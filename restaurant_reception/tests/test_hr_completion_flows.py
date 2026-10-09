from base64 import b64encode
from datetime import date, timedelta

from odoo import Command
from odoo.exceptions import AccessError, ValidationError
from odoo.tests.common import new_test_user

from odoo.addons.base.tests.common import BaseCommon


class TestRestaurantHRCompletionFlows(BaseCommon):
    _test_user_groups = ()

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.branch = cls.env['restaurant.branch'].create({
            'name': 'HR Completion Branch',
            'code': 'HRC-TEST',
            'company_id': cls.company.id,
        })
        cls.other_branch = cls.env['restaurant.branch'].create({
            'name': 'Other HR Completion Branch',
            'code': 'HRC-OTHER',
            'company_id': cls.company.id,
        })
        cls.hr = cls._make_user('completion_hr', 'restaurant_core.group_restaurant_hr')
        cls.manager = cls._make_user(
            'completion_manager', 'restaurant_core.group_restaurant_branch_manager', cls.branch,
        )
        cls.other_manager = cls._make_user(
            'completion_other_manager', 'restaurant_core.group_restaurant_branch_manager', cls.other_branch,
        )
        cls.accountant = cls._make_user(
            'completion_accountant', 'restaurant_core.group_restaurant_accountant',
        )
        cls.job = cls.env['hr.job'].create({
            'name': 'HR Completion Receptionist',
            'company_id': cls.company.id,
        })
        cls.employee = cls.env['hr.employee'].create({
            'name': 'HR Completion Employee',
            'company_id': cls.company.id,
            'restaurant_branch_id': cls.branch.id,
            'job_id': cls.job.id,
            'wage': 3500.0,
        })

    @classmethod
    def _make_user(cls, login, groups, branch=None):
        return new_test_user(
            cls.env,
            login=login,
            groups=groups,
            company_id=cls.company.id,
            company_ids=[Command.set(cls.company.ids)],
            restaurant_branch_ids=[Command.set(branch.ids if branch else [])],
        )

    def _model(self, model, user):
        return self.env[model].with_user(user).with_context(
            allowed_company_ids=[self.company.id],
            tracking_disable=True,
            mail_notrack=True,
        )

    def test_offboarding_checklist_roles_and_employee_archive(self):
        offboarding = self._model('restaurant.employee.offboarding', self.hr).create({
            'employee_id': self.employee.id,
            'reason': 'resignation',
            'notice_date': date(2026, 10, 1),
            'last_working_date': date(2026, 10, 31),
            'separation_document': b64encode(b'resignation').decode(),
            'separation_document_filename': 'resignation.pdf',
        })
        self.assertEqual(len(offboarding.task_ids), 7)
        self.assertEqual(len(offboarding.task_ids.filtered(
            lambda task: task.responsible_role == 'manager'
        )), 2)
        offboarding.action_start()
        with self.assertRaises(ValidationError):
            offboarding.action_complete()

        manager_tasks = offboarding.task_ids.filtered(
            lambda task: task.responsible_role == 'manager'
        )
        with self.assertRaises(AccessError):
            manager_tasks[:1].with_user(self.other_manager).action_mark_done()
        manager_tasks.with_user(self.manager).action_mark_done()
        offboarding.task_ids.filtered(
            lambda task: task.responsible_role == 'hr'
        ).with_user(self.hr).action_mark_done()
        offboarding.with_user(self.hr).write({
            'final_settlement_document': b64encode(b'settlement').decode(),
            'final_settlement_filename': 'settlement.pdf',
            'exit_clearance_document': b64encode(b'clearance').decode(),
            'exit_clearance_filename': 'clearance.pdf',
        })
        offboarding.with_user(self.hr).action_complete()
        self.assertEqual(offboarding.state, 'completed')
        self.assertFalse(self.employee.active)
        self.assertEqual(self.employee.restaurant_offboarding_state, 'completed')

    def test_payroll_inputs_snapshot_and_handoff_to_accounting(self):
        month = self._model('restaurant.attendance.month', self.hr).create({
            'branch_id': self.branch.id,
            'month_start': date(2026, 9, 1),
        })
        self.env['restaurant.attendance.month.line'].sudo().with_context(
            attendance_month_internal=True,
        ).create({
            'month_id': month.id,
            'employee_id': self.employee.id,
            'present_days': 22,
            'late_count': 1,
            'late_minutes': 8,
            'absence_days': 1,
            'annual_leave_days': 2,
            'overtime_hours': 5.5,
        })
        month.sudo()._internal_write({'state': 'closed'})

        batch = self._model('restaurant.payroll.input.batch', self.hr).create({
            'attendance_month_id': month.id,
        })
        self.assertEqual(len(batch.line_ids), 1)
        line = batch.line_ids
        self.assertEqual(line.base_salary, 3500.0)
        self.assertEqual(line.unpaid_absence_days, 1)
        line.with_user(self.hr).write({
            'overtime_amount': 150.0,
            'allowance_amount': 100.0,
            'deduction_amount': 50.0,
            'adjustment_note': 'Reviewed monthly inputs.',
        })
        self.assertEqual(line.input_total, 3700.0)
        batch.with_user(self.hr).action_ready()
        with self.assertRaises(AccessError):
            batch.with_user(self.hr).action_process()
        batch.with_user(self.accountant).action_process()
        self.assertEqual(batch.state, 'processed')
        with self.assertRaises(AccessError):
            line.with_user(self.hr).write({'allowance_amount': 200.0})

    def test_performance_review_manager_and_hr_handoff(self):
        review = self._model('restaurant.employee.performance.review', self.hr).create({
            'employee_id': self.employee.id,
            'review_type': 'quarterly',
            'period_start': date(2026, 7, 1),
            'period_end': date(2026, 9, 30),
        })
        review.action_send_to_manager()
        with self.assertRaises(AccessError):
            review.with_user(self.other_manager).write({'quality_rating': '4'})
        review.with_user(self.manager).write({
            'quality_rating': '4',
            'attendance_rating': '5',
            'teamwork_rating': '4',
            'customer_service_rating': '5',
            'policy_compliance_rating': '4',
            'strengths': 'Strong guest service and reliable teamwork.',
            'goals': 'Develop shift leadership skills.',
            'manager_recommendation': 'continue',
        })
        review.with_user(self.manager).action_submit_to_hr()
        self.assertEqual(review.state, 'hr_review')
        self.assertEqual(review.overall_score, 4.4)
        with self.assertRaises(ValidationError):
            review.with_user(self.hr).action_complete()
        review.with_user(self.hr).write({
            'final_outcome': 'meets',
            'hr_comments': 'Manager assessment reviewed and accepted.',
        })
        review.with_user(self.hr).action_complete()
        self.assertEqual(review.state, 'completed')
        self.assertEqual(self.employee.restaurant_performance_review_count, 1)
        self.assertEqual(self.employee.restaurant_latest_performance_score, 4.4)

    def test_managers_cannot_create_hr_records(self):
        with self.assertRaises(AccessError):
            self._model('restaurant.employee.offboarding', self.manager).create({
                'employee_id': self.employee.id,
                'reason': 'other',
                'notice_date': date.today(),
                'last_working_date': date.today() + timedelta(days=7),
            })
        with self.assertRaises(AccessError):
            self._model('restaurant.employee.performance.review', self.manager).create({
                'employee_id': self.employee.id,
                'review_type': 'ad_hoc',
                'period_start': date.today(),
                'period_end': date.today(),
            })

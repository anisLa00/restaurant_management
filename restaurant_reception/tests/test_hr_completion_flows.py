from base64 import b64encode
from datetime import date, datetime, timedelta

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
        cls.operations = cls._make_user(
            'completion_operations', 'restaurant_core.group_restaurant_operations_manager',
        )
        cls.owner = cls._make_user(
            'completion_owner', 'restaurant_core.group_restaurant_owner',
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

    def _approved_attendance(self, attendance_date, status, employee=None):
        employee = employee or self.employee
        values = {
            'employee_id': employee.id,
            'branch_id': self.branch.id,
            'attendance_date': attendance_date,
            'status': status,
            'state': 'approved',
            'sync_status': 'synced',
        }
        if status == 'overtime_day':
            values.update({
                'scheduled_day_off': True,
                'overtime_measurement': 'approved_day',
                'check_in': datetime.combine(attendance_date, datetime.min.time())
                + timedelta(hours=9),
                'check_out': datetime.combine(attendance_date, datetime.min.time())
                + timedelta(hours=17),
            })
        return self.env['restaurant.attendance.entry'].sudo().with_context(
            restaurant_leave_sync=True,
        ).create(values)

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
        absence = self._approved_attendance(date(2026, 9, 1), 'absent')
        overtime = self._approved_attendance(
            date(2026, 9, 2), 'overtime_day'
        ) | self._approved_attendance(date(2026, 9, 3), 'overtime_day')
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
            'unpaid_absence_days': 1,
            'annual_leave_days': 2,
            'overtime_hours': 5.5,
            'overtime_days': 2,
        })
        month.sudo()._internal_write({'state': 'closed'})

        batch = self._model('restaurant.payroll.input.batch', self.hr).create({
            'attendance_month_id': month.id,
        })
        self.assertEqual(len(batch.line_ids), 1)
        line = batch.line_ids
        self.assertEqual(line.base_salary, 3500.0)
        self.assertAlmostEqual(line.daily_rate, 3500.0 / 30, places=2)
        self.assertEqual(line.gross_unexcused_absence_days, 1)
        self.assertEqual(line.absence_overtime_offset_days, 1)
        self.assertEqual(line.unpaid_absence_days, 0)
        self.assertEqual(line.automatic_absence_deduction, 0)
        self.assertEqual(line.overtime_days, 2)
        self.assertEqual(line.eligible_overtime_days, 2)
        self.assertEqual(line.payable_overtime_days, 1)
        self.assertEqual(line.automatic_overtime_amount, 100.0)
        self.assertEqual(line.offset_absence_source_ids, absence)
        self.assertEqual(line.offset_overtime_source_ids, overtime[:1])
        self.assertEqual(line.payable_overtime_source_ids, overtime[1:])
        line.with_user(self.hr).write({
            'allowance_amount': 100.0,
            'deduction_amount': 50.0,
            'adjustment_note': 'Reviewed monthly inputs.',
        })
        self.assertAlmostEqual(line.input_total, 3650.0, places=2)
        batch.with_user(self.hr).action_ready()
        with self.assertRaises(AccessError):
            batch.with_user(self.hr).action_process()
        batch.with_user(self.accountant).action_process()
        self.assertEqual(batch.state, 'processed')
        with self.assertRaises(AccessError):
            line.with_user(self.hr).write({'allowance_amount': 200.0})

    def _processed_absence_payroll(self, month_start):
        self.employee.wage = 3000.0
        absence = self._approved_attendance(month_start, 'absent')
        month = self._model('restaurant.attendance.month', self.hr).create({
            'branch_id': self.branch.id,
            'month_start': month_start,
        })
        self.env['restaurant.attendance.month.line'].sudo().with_context(
            attendance_month_internal=True,
        ).create({
            'month_id': month.id,
            'employee_id': self.employee.id,
            'absence_days': 1,
            'unpaid_absence_days': 1,
        })
        month.sudo()._internal_write({'state': 'closed'})
        batch = self._model('restaurant.payroll.input.batch', self.hr).create({
            'attendance_month_id': month.id,
        })
        self.assertEqual(batch.line_ids.automatic_absence_deduction, 100.0)
        batch.with_user(self.hr).action_ready()
        batch.with_user(self.accountant).action_process()
        return absence, month, batch

    def _correct_absence_to_present(self, absence, month, method):
        self.company.restaurant_hr_official_sync_enabled = True
        month.correction_reason = 'Correct false absence to confirmed attendance.'
        month.with_user(self.hr).action_open_correction()
        correction_entry = absence.with_user(self.hr)
        correction_entry.action_reopen_for_month_correction()
        correction_entry.write({
            'status': 'present',
            'check_in': datetime.combine(absence.attendance_date, datetime.min.time())
            + timedelta(hours=9),
            'check_out': datetime.combine(absence.attendance_date, datetime.min.time())
            + timedelta(hours=17),
            'correction_settlement_method': method,
            'correction_payroll_note': 'Refund false absence deduction.',
        })
        correction_entry.action_approve()
        return self.env['restaurant.payroll.correction'].search([
            ('attendance_entry_id', '=', absence.id),
        ])

    def test_processed_attendance_correction_carries_to_next_payroll(self):
        absence, month, _batch = self._processed_absence_payroll(date(2028, 1, 1))
        correction = self._correct_absence_to_present(
            absence, month, 'next_payroll',
        )
        self.assertEqual(correction.state, 'pending_carry')
        self.assertEqual(correction.allowance_amount, 100.0)
        self.assertEqual(correction.deduction_amount, 0)

        target_month = self._model('restaurant.attendance.month', self.hr).create({
            'branch_id': self.branch.id,
            'month_start': date(2028, 2, 1),
        })
        self.env['restaurant.attendance.month.line'].sudo().with_context(
            attendance_month_internal=True,
        ).create({
            'month_id': target_month.id,
            'employee_id': self.employee.id,
            'present_days': 20,
        })
        target_month.sudo()._internal_write({'state': 'closed'})
        target_batch = self._model('restaurant.payroll.input.batch', self.hr).create({
            'attendance_month_id': target_month.id,
        })
        target_line = target_batch.line_ids.filtered(
            lambda line: line.employee_id == self.employee
        )
        self.assertEqual(correction.state, 'carried')
        self.assertEqual(correction.target_payroll_batch_id, target_batch)
        self.assertEqual(target_line.correction_allowance_amount, 100.0)
        self.assertEqual(target_line.correction_deduction_amount, 0)
        self.assertEqual(target_line.correction_ids, correction)
        self.assertEqual(target_line.input_total, 3100.0)

    def test_processed_attendance_correction_can_be_cash_settled_once(self):
        absence, month, _batch = self._processed_absence_payroll(date(2028, 3, 1))
        correction = self._correct_absence_to_present(absence, month, 'cash_now')
        self.assertEqual(correction.state, 'awaiting_cash')
        self.assertEqual(correction.allowance_amount, 100.0)
        self.assertTrue(correction.with_user(self.hr).has_access('read'))
        self.assertTrue(correction.with_user(self.accountant).has_access('read'))
        self.assertFalse(correction.with_user(self.manager).has_access('read'))
        self.assertFalse(correction.with_user(self.operations).has_access('read'))
        self.assertFalse(correction.with_user(self.owner).has_access('read'))

        with self.assertRaises(AccessError):
            correction.with_user(self.accountant).write({'allowance_amount': 200.0})
        with self.assertRaises(AccessError):
            correction.with_user(self.accountant).unlink()

        correction.with_user(self.accountant).write({
            'cash_settlement_date': date(2028, 4, 5),
            'cash_settlement_note': 'Cash handed to employee at the office.',
            'cash_receipt': b64encode(b'signed cash receipt').decode(),
            'cash_receipt_filename': 'receipt.pdf',
        })
        with self.assertRaises(AccessError):
            correction.with_user(self.hr).action_mark_cash_settled()
        correction.with_user(self.accountant).action_mark_cash_settled()
        self.assertEqual(correction.state, 'cash_settled')
        self.assertEqual(correction.cash_settled_by, self.accountant)

        target_month = self._model('restaurant.attendance.month', self.hr).create({
            'branch_id': self.branch.id,
            'month_start': date(2028, 4, 1),
        })
        self.env['restaurant.attendance.month.line'].sudo().with_context(
            attendance_month_internal=True,
        ).create({
            'month_id': target_month.id,
            'employee_id': self.employee.id,
            'present_days': 20,
        })
        target_month.sudo()._internal_write({'state': 'closed'})
        target_batch = self._model('restaurant.payroll.input.batch', self.hr).create({
            'attendance_month_id': target_month.id,
        })
        target_line = target_batch.line_ids.filtered(
            lambda line: line.employee_id == self.employee
        )
        self.assertEqual(target_line.correction_allowance_amount, 0)
        self.assertEqual(target_line.correction_deduction_amount, 0)
        self.assertFalse(target_line.correction_ids)

    def test_payroll_overtime_offsets_unexcused_absence_before_payment(self):
        cases = (
            # Five absences and four overtime days leave one deducted absence.
            (date(2027, 1, 1), 5, 4, 1, 0, 3500.0 / 30),
            # One absence consumes one overtime day; the other three are paid.
            (date(2027, 2, 1), 1, 4, 0, 3, 0),
            # Without overtime, all four absences are one automatic deduction.
            (date(2027, 3, 1), 4, 0, 4, 0, (3500.0 / 30) * 4),
        )
        for month_start, absence_count, overtime_count, counted, payable, deduction in cases:
            for offset in range(absence_count):
                self._approved_attendance(
                    month_start + timedelta(days=offset), 'absent'
                )
            for offset in range(overtime_count):
                self._approved_attendance(
                    month_start + timedelta(days=10 + offset), 'overtime_day'
                )
            month = self._model('restaurant.attendance.month', self.hr).create({
                'branch_id': self.branch.id,
                'month_start': month_start,
            })
            self.env['restaurant.attendance.month.line'].sudo().with_context(
                attendance_month_internal=True,
            ).create({
                'month_id': month.id,
                'employee_id': self.employee.id,
                'absence_days': absence_count,
                'unpaid_absence_days': absence_count,
                'overtime_days': overtime_count,
            })
            month.sudo()._internal_write({'state': 'closed'})
            batch = self._model('restaurant.payroll.input.batch', self.hr).create({
                'attendance_month_id': month.id,
            })
            line = batch.line_ids
            self.assertEqual(line.gross_unexcused_absence_days, absence_count)
            self.assertEqual(
                line.absence_overtime_offset_days,
                min(absence_count, overtime_count),
            )
            self.assertEqual(line.unpaid_absence_days, counted)
            self.assertAlmostEqual(
                line.automatic_absence_deduction, deduction, places=2,
            )
            self.assertEqual(line.payable_overtime_days, payable)
            self.assertEqual(line.automatic_overtime_amount, payable * 100)
            self.assertEqual(
                len(line.offset_absence_source_ids),
                min(absence_count, overtime_count),
            )
            self.assertEqual(
                len(line.offset_overtime_source_ids),
                min(absence_count, overtime_count),
            )
            self.assertEqual(len(line.payable_overtime_source_ids), payable)

    def test_fifth_overtime_day_cannot_be_approved(self):
        month_start = date(2027, 4, 1)
        for offset in range(4):
            self._approved_attendance(
                month_start + timedelta(days=offset), 'overtime_day'
            )
        fifth = self.env['restaurant.attendance.entry'].sudo().with_context(
            restaurant_leave_sync=True,
        ).create({
            'employee_id': self.employee.id,
            'branch_id': self.branch.id,
            'attendance_date': month_start + timedelta(days=4),
            'status': 'overtime_day',
            'scheduled_day_off': True,
            'overtime_measurement': 'approved_day',
            'check_in': datetime(2027, 4, 5, 9),
            'check_out': datetime(2027, 4, 5, 17),
            'state': 'submitted_to_hr',
            'sync_status': 'pending',
            'manager_reviewed_by': self.manager.id,
        })
        with self.assertRaisesRegex(ValidationError, 'four approved Overtime Days'):
            fifth.with_user(self.hr).action_hr_approve_selected()

    def test_overtime_does_not_offset_approved_unpaid_leave(self):
        month_start = date(2027, 5, 1)
        unpaid_leave = self.env['restaurant.attendance.entry'].sudo().with_context(
            restaurant_leave_sync=True,
        ).create({
            'employee_id': self.employee.id,
            'branch_id': self.branch.id,
            'attendance_date': month_start,
            'status': 'annual_leave',
            'leave_payroll_treatment': 'unpaid',
            'system_leave_locked': True,
            'state': 'approved',
            'sync_status': 'synced',
        })
        overtime = self._approved_attendance(
            month_start + timedelta(days=1), 'overtime_day'
        )
        month = self._model('restaurant.attendance.month', self.hr).create({
            'branch_id': self.branch.id,
            'month_start': month_start,
        })
        self.env['restaurant.attendance.month.line'].sudo().with_context(
            attendance_month_internal=True,
        ).create({
            'month_id': month.id,
            'employee_id': self.employee.id,
            'unpaid_absence_days': 1,
            'unpaid_leave_days': 1,
            'annual_leave_days': 1,
            'overtime_days': 1,
        })
        month.sudo()._internal_write({'state': 'closed'})
        batch = self._model('restaurant.payroll.input.batch', self.hr).create({
            'attendance_month_id': month.id,
        })
        line = batch.line_ids
        self.assertEqual(line.absence_overtime_offset_days, 0)
        self.assertEqual(line.unpaid_absence_days, 1)
        self.assertAlmostEqual(
            line.automatic_absence_deduction, 3500.0 / 30, places=2,
        )
        self.assertEqual(line.payable_overtime_days, 1)
        self.assertEqual(line.automatic_overtime_amount, 100)
        self.assertEqual(line.unpaid_absence_source_ids, unpaid_leave)
        self.assertEqual(line.payable_overtime_source_ids, overtime)

    def test_payroll_processing_blocks_invalid_salary_and_negative_input(self):
        no_salary = self.env['hr.employee'].create({
            'name': 'Missing Salary Employee',
            'company_id': self.company.id,
            'restaurant_branch_id': self.branch.id,
            'wage': 0,
        })
        month = self._model('restaurant.attendance.month', self.hr).create({
            'branch_id': self.branch.id,
            'month_start': date(2026, 11, 1),
        })
        self.env['restaurant.attendance.month.line'].sudo().with_context(
            attendance_month_internal=True,
        ).create({
            'month_id': month.id,
            'employee_id': no_salary.id,
            'present_days': 20,
        })
        month.sudo()._internal_write({'state': 'closed'})
        batch = self._model('restaurant.payroll.input.batch', self.hr).create({
            'attendance_month_id': month.id,
        })
        batch.with_user(self.hr).action_ready()
        with self.assertRaisesRegex(ValidationError, 'Missing Salary Employee'):
            batch.with_user(self.accountant).action_process()

        month_two = self._model('restaurant.attendance.month', self.hr).create({
            'branch_id': self.branch.id,
            'month_start': date(2026, 12, 1),
        })
        self.env['restaurant.attendance.month.line'].sudo().with_context(
            attendance_month_internal=True,
        ).create({
            'month_id': month_two.id,
            'employee_id': self.employee.id,
            'present_days': 20,
        })
        month_two.sudo()._internal_write({'state': 'closed'})
        negative = self._model('restaurant.payroll.input.batch', self.hr).create({
            'attendance_month_id': month_two.id,
        })
        negative.line_ids.with_user(self.hr).write({
            'deduction_amount': 4000,
            'adjustment_note': 'Audited manual recovery.',
        })
        self.assertLess(negative.line_ids.input_total, 0)
        negative.with_user(self.hr).action_ready()
        with self.assertRaisesRegex(ValidationError, 'negative'):
            negative.with_user(self.accountant).action_process()

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

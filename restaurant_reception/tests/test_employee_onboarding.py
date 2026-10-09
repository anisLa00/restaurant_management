from datetime import timedelta

from odoo import Command, fields
from odoo.exceptions import AccessError, ValidationError
from odoo.tests.common import new_test_user

from odoo.addons.base.tests.common import BaseCommon


class TestRestaurantEmployeeOnboarding(BaseCommon):
    _test_user_groups = ()

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.branch = cls.env['restaurant.branch'].create({
            'name': 'Onboarding Branch',
            'code': 'ONB-TEST',
            'company_id': cls.company.id,
        })
        cls.hr = cls._make_user(
            'onboarding_hr', 'restaurant_core.group_restaurant_hr',
        )
        cls.manager = cls._make_user(
            'onboarding_manager',
            'restaurant_core.group_restaurant_branch_manager',
            cls.branch,
        )
        cls.other_manager = cls._make_user(
            'onboarding_other_manager',
            'restaurant_core.group_restaurant_branch_manager',
        )
        cls.job = cls.env['hr.job'].create({
            'name': 'Onboarding Receptionist',
            'company_id': cls.company.id,
        })
        cls.employee = cls.env['hr.employee'].create({
            'name': 'Onboarding Employee',
            'company_id': cls.company.id,
            'restaurant_branch_id': cls.branch.id,
            'job_id': cls.job.id,
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

    def _onboarding(self):
        return self._model('restaurant.employee.onboarding', self.hr).create({
            'employee_id': self.employee.id,
        })

    def test_seven_day_checklist_and_role_completion(self):
        onboarding = self._onboarding()
        self.assertEqual(onboarding.planned_end_date, onboarding.start_date + timedelta(days=6))
        self.assertEqual(onboarding.duration_days, 7)
        self.assertEqual(len(onboarding.task_ids), 7)
        self.assertEqual(len(onboarding.task_ids.filtered(
            lambda task: task.responsible_role == 'hr'
        )), 3)
        self.assertEqual(len(onboarding.task_ids.filtered(
            lambda task: task.responsible_role == 'manager'
        )), 4)

        onboarding.action_start()
        self.assertEqual(onboarding.state, 'in_progress')
        hr_task = onboarding.task_ids.filtered(
            lambda task: task.responsible_role == 'hr'
        )[:1]
        with self.assertRaises(AccessError):
            hr_task.with_user(self.manager).action_mark_done()

        manager_tasks = onboarding.task_ids.filtered(
            lambda task: task.responsible_role == 'manager'
        )
        with self.assertRaises(AccessError):
            manager_tasks[:1].with_user(self.other_manager).action_mark_done()

        onboarding.task_ids.filtered(
            lambda task: task.responsible_role == 'hr'
        ).with_user(self.hr).action_mark_done()
        manager_tasks.with_user(self.manager).action_mark_done()
        self.assertEqual(onboarding.progress_percent, 100.0)
        self.assertEqual(onboarding.pending_mandatory_count, 0)

        onboarding.with_user(self.hr).action_complete()
        self.assertEqual(onboarding.state, 'completed')
        self.assertEqual(self.employee.restaurant_onboarding_state, 'completed')
        self.assertEqual(onboarding.completed_by, self.hr)

    def test_complete_is_blocked_while_required_tasks_are_pending(self):
        onboarding = self._onboarding()
        onboarding.action_start()
        with self.assertRaises(ValidationError):
            onboarding.action_complete()

    def test_manager_can_report_failed_task_and_hr_reopens_it(self):
        onboarding = self._onboarding()
        onboarding.action_start()
        manager_task = onboarding.task_ids.filtered(
            lambda task: task.responsible_role == 'manager'
        )[:1]

        action = manager_task.with_user(self.manager).action_open_failure_wizard()
        self.assertEqual(
            action['res_model'], 'restaurant.onboarding.task.failure.wizard',
        )
        with self.assertRaises(ValidationError):
            manager_task.with_user(self.manager).action_mark_failed('')
        with self.assertRaises(AccessError):
            manager_task.with_user(self.other_manager).action_mark_failed(
                'Wrong branch manager must not update this task.'
            )

        manager_task.with_user(self.manager).action_mark_failed(
            'The employee did not pass the role training assessment.'
        )
        self.assertEqual(manager_task.state, 'failed')
        self.assertEqual(manager_task.failed_by, self.manager)
        self.assertTrue(manager_task.failed_at)
        self.assertIn('did not pass', manager_task.failure_reason)
        self.assertEqual(onboarding.state, 'in_progress')
        self.assertGreater(onboarding.pending_mandatory_count, 0)
        with self.assertRaises(ValidationError):
            onboarding.with_user(self.hr).action_complete()

        manager_task.with_user(self.hr).action_reset()
        self.assertEqual(manager_task.state, 'pending')
        self.assertFalse(manager_task.failure_reason)
        manager_task.with_user(self.manager).action_mark_done()
        self.assertEqual(manager_task.state, 'done')

    def test_extension_and_failed_outcome_are_audited(self):
        onboarding = self._onboarding()
        onboarding.action_start()
        with self.assertRaises(ValidationError):
            onboarding.action_extend()

        new_deadline = onboarding.planned_end_date + timedelta(days=7)
        onboarding.write({
            'extension_end_date': new_deadline,
            'extension_reason': 'Additional role training required.',
        })
        onboarding.action_extend()
        self.assertEqual(onboarding.state, 'extended')
        self.assertEqual(onboarding.planned_end_date, new_deadline)
        self.assertEqual(onboarding.extended_by, self.hr)

        with self.assertRaises(ValidationError):
            onboarding.action_fail()
        onboarding.failure_reason = 'Documented probation suitability concern.'
        onboarding.action_fail()
        self.assertEqual(onboarding.state, 'failed')
        self.assertEqual(onboarding.failed_by, self.hr)
        self.assertTrue(onboarding.failed_at)

    def test_employee_smart_action_opens_existing_onboarding(self):
        onboarding = self._onboarding()
        action = self.employee.with_user(self.hr).action_open_restaurant_onboarding()
        self.assertEqual(action['res_model'], 'restaurant.employee.onboarding')
        self.assertEqual(action['res_id'], onboarding.id)
        self.assertEqual(self.employee.restaurant_onboarding_count, 1)

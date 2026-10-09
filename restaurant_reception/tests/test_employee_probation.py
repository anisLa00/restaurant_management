from datetime import timedelta

from dateutil.relativedelta import relativedelta

from odoo import Command
from odoo.exceptions import AccessError, ValidationError
from odoo.tests.common import new_test_user

from odoo.addons.base.tests.common import BaseCommon


class TestRestaurantEmployeeProbation(BaseCommon):
    _test_user_groups = ()

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.branch = cls.env['restaurant.branch'].create({
            'name': 'Probation Branch',
            'code': 'PRB-TEST',
            'company_id': cls.company.id,
        })
        cls.other_branch = cls.env['restaurant.branch'].create({
            'name': 'Other Probation Branch',
            'code': 'PRB-OTHER',
            'company_id': cls.company.id,
        })
        cls.hr = cls._make_user(
            'probation_hr', 'restaurant_core.group_restaurant_hr',
        )
        cls.manager = cls._make_user(
            'probation_manager',
            'restaurant_core.group_restaurant_branch_manager',
            cls.branch,
        )
        cls.other_manager = cls._make_user(
            'probation_other_manager',
            'restaurant_core.group_restaurant_branch_manager',
            cls.other_branch,
        )
        cls.job = cls.env['hr.job'].create({
            'name': 'Probation Receptionist',
            'company_id': cls.company.id,
        })
        cls.employee = cls.env['hr.employee'].create({
            'name': 'Probation Employee',
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

    def _complete_onboarding(self):
        onboarding = self._model('restaurant.employee.onboarding', self.hr).create({
            'employee_id': self.employee.id,
        })
        onboarding.action_start()
        onboarding.task_ids.filtered(
            lambda task: task.responsible_role == 'hr'
        ).with_user(self.hr).action_mark_done()
        onboarding.task_ids.filtered(
            lambda task: task.responsible_role == 'manager'
        ).with_user(self.manager).action_mark_done()
        onboarding.with_user(self.hr).action_complete()
        return onboarding

    def test_completion_starts_three_month_probation_and_review_schedule(self):
        onboarding = self._complete_onboarding()
        probation = self.env['restaurant.employee.probation'].search([
            ('onboarding_id', '=', onboarding.id),
        ])
        self.assertEqual(len(probation), 1)
        self.assertEqual(probation.state, 'active')
        self.assertEqual(probation.start_date, onboarding.start_date)
        self.assertEqual(
            probation.planned_end_date,
            probation.start_date + relativedelta(months=3, days=-1),
        )
        self.assertEqual(
            probation.legal_max_end_date,
            probation.start_date + relativedelta(months=6, days=-1),
        )
        self.assertEqual(
            set(probation.review_ids.mapped('review_type')),
            {'first_month', 'midterm', 'final'},
        )
        self.assertEqual(self.employee.restaurant_probation_state, 'active')

    def test_manager_submits_final_review_and_hr_confirms(self):
        probation = self._complete_onboarding().employee_id.restaurant_probation_ids
        final_review = probation.review_ids.filtered(
            lambda review: review.review_type == 'final'
        )
        with self.assertRaises(AccessError):
            final_review.with_user(self.other_manager).write({'rating': '4'})
        final_review.with_user(self.manager).write({
            'rating': '4',
            'recommendation': 'confirm',
            'strengths': 'Reliable service and good guest communication.',
        })
        final_review.with_user(self.manager).action_submit()
        self.assertEqual(final_review.state, 'submitted')
        self.assertEqual(probation.state, 'hr_review')

        with self.assertRaises(ValidationError):
            probation.with_user(self.hr).action_confirm_employment()
        probation.with_user(self.hr).hr_decision_note = 'Final review approved.'
        probation.with_user(self.hr).action_confirm_employment()
        self.assertEqual(probation.state, 'confirmed')
        self.assertEqual(probation.confirmed_by, self.hr)

    def test_extension_cannot_exceed_six_month_legal_maximum(self):
        probation = self._complete_onboarding().employee_id.restaurant_probation_ids
        probation.with_user(self.hr).write({
            'planned_end_date': probation.planned_end_date - timedelta(days=30),
            'extension_end_date': probation.legal_max_end_date,
            'extension_reason': 'Additional monitored service training.',
        })
        probation.with_user(self.hr).action_extend()
        self.assertEqual(probation.state, 'extended')
        self.assertEqual(probation.planned_end_date, probation.legal_max_end_date)

        probation.with_context(restaurant_probation_internal=True).write({
            'state': 'active',
            'planned_end_date': probation.legal_max_end_date - timedelta(days=1),
        })
        probation.with_user(self.hr).write({
            'extension_end_date': probation.legal_max_end_date + timedelta(days=1),
        })
        with self.assertRaises(ValidationError):
            probation.with_user(self.hr).action_extend()

    def test_end_during_probation_requires_final_review_and_fourteen_day_notice(self):
        probation = self._complete_onboarding().employee_id.restaurant_probation_ids
        final_review = probation.review_ids.filtered(
            lambda review: review.review_type == 'final'
        )
        final_review.with_user(self.manager).write({
            'rating': '1',
            'recommendation': 'end',
            'concerns': 'Documented role suitability concerns.',
        })
        final_review.with_user(self.manager).action_submit()
        notice_date = probation.start_date + timedelta(days=30)
        probation.with_user(self.hr).write({
            'termination_reason': 'Final manager review recommends ending probation.',
            'notice_date': notice_date,
            'effective_end_date': notice_date + timedelta(days=13),
        })
        with self.assertRaises(ValidationError):
            probation.with_user(self.hr).action_end_during_probation()

        probation.with_user(self.hr).effective_end_date = notice_date + timedelta(days=14)
        probation.with_user(self.hr).action_end_during_probation()
        self.assertEqual(probation.state, 'ended')
        self.assertEqual(probation.ended_by, self.hr)

    def test_probation_cannot_be_created_by_manager_or_deleted(self):
        onboarding = self._complete_onboarding()
        with self.assertRaises(AccessError):
            self._model('restaurant.employee.probation', self.manager).create({
                'employee_id': self.employee.id,
                'onboarding_id': onboarding.id,
                'start_date': onboarding.start_date,
            })
        with self.assertRaises(AccessError):
            onboarding.employee_id.restaurant_probation_ids.with_user(self.hr).unlink()

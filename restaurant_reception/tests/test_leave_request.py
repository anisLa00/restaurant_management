import base64
from datetime import date

from odoo import Command
from odoo.exceptions import AccessError, ValidationError
from odoo.tests.common import new_test_user

from odoo.addons.base.tests.common import BaseCommon


class TestRestaurantLeaveRequest(BaseCommon):
    _test_user_groups = ()

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.branch = cls.env['restaurant.branch'].create({
            'name': 'Leave Workflow Branch',
            'code': 'LV-WF',
            'company_id': cls.company.id,
        })
        cls.other_branch = cls.env['restaurant.branch'].create({
            'name': 'Other Leave Branch',
            'code': 'LV-OTHER',
            'company_id': cls.company.id,
        })
        cls.manager = cls._make_user(
            'leave_workflow_manager',
            'restaurant_core.group_restaurant_branch_manager',
            cls.branch,
        )
        cls.other_manager = cls._make_user(
            'leave_other_manager',
            'restaurant_core.group_restaurant_branch_manager',
            cls.other_branch,
        )
        cls.hr = cls._make_user(
            'leave_workflow_hr', 'restaurant_core.group_restaurant_hr',
        )
        cls.reception = cls._make_user(
            'leave_workflow_reception',
            'restaurant_core.group_restaurant_reception',
            cls.branch,
        )
        cls.dual_role = cls._make_user(
            'leave_workflow_dual',
            'restaurant_core.group_restaurant_branch_manager,restaurant_core.group_restaurant_hr',
            cls.branch,
        )
        cls.employee = cls.env['hr.employee'].create({
            'name': 'Paper Leave Employee',
            'company_id': cls.company.id,
        })
        cls.leave_type = cls.env['hr.work.entry.type'].create({
            'name': 'Paper Leave Type',
            'code': 'PAPERLEAVE',
            'count_as': 'absence',
            'country_id': cls.company.country_id.id,
            'time_off_selectable': True,
            'requires_allocation': False,
            'leave_validation_type': 'hr',
            'request_unit': 'day',
        })
        cls.company.write({
            'restaurant_hr_official_sync_enabled': True,
            'restaurant_annual_leave_type_id': cls.leave_type.id,
            'restaurant_sick_leave_type_id': cls.leave_type.id,
            'restaurant_emergency_leave_type_id': cls.leave_type.id,
        })
        cls.leave_date = date(2099, 6, 15)

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

    def _model(self, user):
        return self.env['restaurant.leave.request'].with_user(user).with_context(
            allowed_company_ids=[self.company.id],
            tracking_disable=True,
            mail_notrack=True,
        )

    def _request(self, user=None, with_document=True, employee=None):
        values = {
            'employee_id': (employee or self.employee).id,
            'branch_id': self.branch.id,
            'leave_kind': 'annual_leave',
            'request_date_from': self.leave_date,
            'request_date_to': self.leave_date,
            'reason': 'Signed annual leave request.',
            'manager_note': 'Branch coverage is confirmed.',
        }
        if with_document:
            values.update({
                'request_document': base64.b64encode(
                    b'signed leave form'
                ).decode(),
                'request_document_name': 'signed-leave.pdf',
            })
        return self._model(user or self.manager).create(values)

    def test_manager_paper_request_to_final_hr_approval(self):
        request = self._request()
        self.assertEqual(request.state, 'draft')
        self.assertEqual(request.entered_by, self.manager)
        self.assertNotEqual(request.reference, 'New')

        request.with_user(self.manager).action_submit_to_hr()
        self.assertEqual(request.state, 'submitted_to_hr')
        self.assertEqual(request.manager_approved_by, self.manager)
        self.assertTrue(request.manager_approved_at)

        result = request.with_user(self.hr).action_approve()
        self.assertEqual(result['params']['type'], 'success')
        self.assertEqual(request.state, 'approved')
        self.assertEqual(request.sync_status, 'synced')
        self.assertEqual(request.hr_reviewed_by, self.hr)
        self.assertTrue(request.official_leave_id)
        self.assertEqual(request.official_leave_id.state, 'validate')
        self.assertEqual(request.official_leave_id.employee_id, self.employee)
        self.assertEqual(
            request.official_leave_id.restaurant_leave_request_id,
            request,
        )
        self.assertEqual(self.env['hr.leave'].search_count([
            ('restaurant_leave_request_id', '=', request.id),
        ]), 1)

    def test_signed_form_is_required_and_reception_cannot_create(self):
        request = self._request(with_document=False)
        with self.assertRaises(ValidationError):
            request.with_user(self.manager).action_submit_to_hr()
        request.with_user(self.manager).write({
            'request_document': base64.b64encode(b'signed later').decode(),
            'request_document_name': 'signed-later.jpg',
        })
        request.with_user(self.manager).action_submit_to_hr()
        self.assertEqual(request.state, 'submitted_to_hr')

        with self.assertRaises(AccessError):
            self._request(user=self.reception)

    def test_hr_return_and_rejection_require_reason(self):
        returned = self._request()
        returned.with_user(self.manager).action_submit_to_hr()
        with self.assertRaises(ValidationError):
            returned.with_user(self.hr).action_return_to_manager()
        returned_hr = returned.with_user(self.hr)
        returned_hr.decision_reason = 'Please attach a clearer signed form.'
        returned_hr.action_return_to_manager()
        self.assertEqual(returned.state, 'draft')
        self.assertEqual(returned.hr_return_count, 1)

        returned_manager = returned.with_user(self.manager)
        returned_manager.request_document = base64.b64encode(
            b'clear signed form'
        ).decode()
        returned_manager.action_submit_to_hr()
        returned_hr = returned.with_user(self.hr)
        returned_hr.decision_reason = 'Dates conflict with a mandatory closure.'
        returned_hr.action_reject()
        self.assertEqual(returned.state, 'rejected')
        self.assertFalse(returned.official_leave_id)

    def test_retry_is_idempotent_and_self_approval_is_blocked(self):
        self.company.restaurant_annual_leave_type_id = False
        request = self._request()
        request.with_user(self.manager).action_submit_to_hr()
        result = request.with_user(self.hr).action_approve()
        self.assertEqual(result['params']['type'], 'danger')
        self.assertEqual(request.state, 'submitted_to_hr')
        self.assertEqual(request.sync_status, 'error')
        self.assertFalse(request.official_leave_id)

        self.company.restaurant_annual_leave_type_id = self.leave_type
        request.with_user(self.hr).action_retry_sync()
        self.assertEqual(request.state, 'approved')
        self.assertEqual(request.sync_attempt_count, 2)
        self.assertEqual(self.env['hr.leave'].search_count([
            ('restaurant_leave_request_id', '=', request.id),
        ]), 1)

        own = self._request(user=self.dual_role)
        own.with_user(self.dual_role).action_submit_to_hr()
        with self.assertRaises(AccessError):
            own.with_user(self.dual_role).action_approve()

    def test_branch_manager_isolation(self):
        request = self._request()
        self.assertFalse(self._model(self.other_manager).search([
            ('id', '=', request.id),
        ]))
        with self.assertRaises(AccessError):
            request.with_user(self.other_manager).read(['reference'])

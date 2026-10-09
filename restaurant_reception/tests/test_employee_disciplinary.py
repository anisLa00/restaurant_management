import base64
from datetime import date, timedelta

from odoo import Command
from odoo.exceptions import AccessError, ValidationError
from odoo.tests.common import new_test_user

from odoo.addons.base.tests.common import BaseCommon


class TestRestaurantEmployeeDisciplinary(BaseCommon):
    _test_user_groups = ()

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.branch = cls.env['restaurant.branch'].create({
            'name': 'Disciplinary Branch',
            'code': 'DSC-TEST',
            'company_id': cls.company.id,
        })
        cls.other_branch = cls.env['restaurant.branch'].create({
            'name': 'Other Disciplinary Branch',
            'code': 'DSC-OTHER',
            'company_id': cls.company.id,
        })
        cls.manager = cls._make_user(
            'disciplinary_manager',
            'restaurant_core.group_restaurant_branch_manager',
            cls.branch,
        )
        cls.other_manager = cls._make_user(
            'disciplinary_other_manager',
            'restaurant_core.group_restaurant_branch_manager',
            cls.other_branch,
        )
        cls.hr = cls._make_user(
            'disciplinary_hr', 'restaurant_core.group_restaurant_hr',
        )
        cls.job = cls.env['hr.job'].create({
            'name': 'Disciplinary Test Position',
            'company_id': cls.company.id,
        })
        cls.employee = cls.env['hr.employee'].create({
            'name': 'Disciplinary Employee',
            'company_id': cls.company.id,
            'restaurant_branch_id': cls.branch.id,
            'job_id': cls.job.id,
        })
        cls.incident_date = date(2099, 1, 10)

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
        return self.env['restaurant.employee.disciplinary.case'].with_user(user).with_context(
            allowed_company_ids=[self.company.id],
            tracking_disable=True,
            mail_notrack=True,
        )

    def _case(self):
        return self._model(self.manager).create({
            'employee_id': self.employee.id,
            'branch_id': self.branch.id,
            'incident_date': self.incident_date,
            'discovered_date': self.incident_date,
            'incident_category': 'conduct',
            'severity': 'medium',
            'allegation_summary': 'Documented workplace conduct incident.',
            'incident_details': 'Manager recorded the facts, time and location.',
            'evidence_file': base64.b64encode(b'incident evidence').decode(),
            'evidence_filename': 'incident-evidence.pdf',
        })

    def _start_investigation(self, case):
        case.with_user(self.manager).action_submit_to_hr()
        hr_case = case.with_user(self.hr)
        hr_case.write({
            'charge_notice_date': self.incident_date + timedelta(days=1),
            'charge_notice_file': base64.b64encode(b'written charge').decode(),
            'charge_notice_filename': 'written-charge.pdf',
        })
        hr_case.action_start_investigation()
        return hr_case

    def _complete_established_investigation(self, case):
        hr_case = self._start_investigation(case)
        hr_case.write({
            'employee_response_status': 'provided',
            'employee_statement': 'The employee statement was heard and recorded.',
            'investigation_summary': 'Evidence and the employee response were reviewed.',
            'investigation_completed_date': self.incident_date + timedelta(days=2),
            'investigation_report_file': base64.b64encode(b'investigation report').decode(),
            'investigation_report_filename': 'investigation-report.pdf',
            'violation_established': 'yes',
        })
        hr_case.action_complete_investigation()
        return hr_case

    def test_manager_submits_one_employee_case_and_cannot_edit_afterward(self):
        case = self._case()
        self.assertNotEqual(case.reference, 'New')
        self.assertEqual(case.job_id, self.job)
        with self.assertRaises(AccessError):
            self._model(self.other_manager).create({
                'employee_id': self.employee.id,
                'branch_id': self.branch.id,
                'incident_date': self.incident_date,
                'discovered_date': self.incident_date,
                'allegation_summary': 'Wrong branch report.',
                'incident_details': 'Should be blocked.',
            })
        case.with_user(self.manager).action_submit_to_hr()
        self.assertEqual(case.state, 'submitted')
        with self.assertRaises(AccessError):
            case.with_user(self.manager).write({'incident_details': 'Changed later.'})
        with self.assertRaises(AccessError):
            case.with_user(self.manager).unlink()

    def test_written_investigation_decision_acknowledgement_and_grievance(self):
        hr_case = self._complete_established_investigation(self._case())
        self.assertEqual(hr_case.state, 'decision_pending')
        hr_case.write({
            'decision_type': 'written_warning',
            'decision_reason': 'The documented violation was established.',
            'repeat_consequence': 'A repeated case may lead to a higher lawful sanction.',
            'decision_notice_date': self.incident_date + timedelta(days=3),
            'decision_notice_file': base64.b64encode(b'written decision').decode(),
            'decision_notice_filename': 'written-warning.pdf',
        })
        hr_case.action_issue_decision()
        self.assertEqual(hr_case.state, 'issued')
        hr_case.write({
            'acknowledgement_status': 'acknowledged',
            'acknowledgement_date': self.incident_date + timedelta(days=3),
            'acknowledgement_file': base64.b64encode(b'signed receipt').decode(),
            'acknowledgement_filename': 'signed-receipt.pdf',
        })
        hr_case.action_close_after_notification()
        self.assertEqual(hr_case.state, 'closed')

        hr_case.write({
            'grievance_date': self.incident_date + timedelta(days=4),
            'grievance_text': 'Employee requested a written review of the decision.',
            'grievance_file': base64.b64encode(b'signed grievance').decode(),
            'grievance_filename': 'employee-grievance.pdf',
        })
        hr_case.action_register_grievance()
        hr_case.write({
            'grievance_outcome': 'upheld',
            'grievance_outcome_note': 'HR reviewed and answered the grievance in writing.',
            'grievance_outcome_date': self.incident_date + timedelta(days=5),
            'grievance_outcome_file': base64.b64encode(b'grievance outcome').decode(),
            'grievance_outcome_filename': 'grievance-outcome.pdf',
        })
        hr_case.action_resolve_grievance()
        self.assertEqual(hr_case.state, 'closed')
        self.assertEqual(hr_case.grievance_resolved_by, self.hr)

    def test_legal_deadlines_and_sanction_limits_are_enforced(self):
        late_case = self._case()
        late_case.with_user(self.manager).action_submit_to_hr()
        late_hr_case = late_case.with_user(self.hr)
        late_hr_case.write({
            'charge_notice_date': self.incident_date + timedelta(days=31),
            'charge_notice_file': base64.b64encode(b'late charge').decode(),
        })
        with self.assertRaises(ValidationError):
            late_hr_case.action_start_investigation()

        hr_case = self._complete_established_investigation(self._case())
        hr_case.write({
            'decision_type': 'unpaid_suspension',
            'sanction_days': 15,
            'decision_reason': 'Established serious violation.',
            'repeat_consequence': 'Further lawful action may follow a repeated violation.',
            'decision_notice_date': self.incident_date + timedelta(days=3),
            'decision_notice_file': base64.b64encode(b'decision notice').decode(),
        })
        with self.assertRaises(ValidationError):
            hr_case.action_issue_decision()

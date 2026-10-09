from datetime import date, datetime, time, timedelta

from psycopg2 import IntegrityError

from odoo import Command
from odoo.exceptions import AccessError, ValidationError
from odoo.tests.common import new_test_user
from odoo.tools import mute_logger
from odoo.tools.safe_eval import safe_eval

from odoo.addons.base.tests.common import BaseCommon


class TestRestaurantHrWorkflow(BaseCommon):
    _test_user_groups = ()

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.branch = cls.env['restaurant.branch'].create({
            'name': 'HR Workflow Branch',
            'code': 'HR-WF',
            'company_id': cls.company.id,
        })
        cls.reception = cls._make_user(
            'hr_workflow_reception',
            'restaurant_core.group_restaurant_reception',
            cls.branch,
        )
        cls.manager = cls._make_user(
            'hr_workflow_manager',
            'restaurant_core.group_restaurant_branch_manager',
            cls.branch,
        )
        cls.hr = cls._make_user(
            'hr_workflow_hr', 'restaurant_core.group_restaurant_hr',
        )
        cls.dual_role = cls._make_user(
            'hr_workflow_dual',
            'restaurant_core.group_restaurant_branch_manager,restaurant_core.group_restaurant_hr',
            cls.branch,
        )
        cls.leave_type = cls.env['hr.work.entry.type'].create({
            'name': 'Restaurant HR Test Leave',
            'code': 'RHTESTLEAVE',
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
        cls.base_date = date(2099, 6, 15)
        cls.employee_sequence = 0

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

    def _employee(self, suffix=None, company=None):
        type(self).employee_sequence += 1
        return self.env['hr.employee'].create({
            'name': suffix or f'HR Workflow Employee {self.employee_sequence}',
            'company_id': (company or self.company).id,
            'restaurant_immigration_status': False,
            'restaurant_work_authorized': True,
        })

    def _entry(self, status='present', employee=None, day=0, with_times=True, **values):
        attendance_date = self.base_date + timedelta(days=day)
        entry_values = {
            'branch_id': self.branch.id,
            'employee_id': (employee or self._employee()).id,
            'attendance_date': attendance_date,
            'status': status,
        }
        if with_times:
            entry_values.update({
                'check_in': datetime.combine(attendance_date, time(8, 0)),
                'check_out': datetime.combine(attendance_date, time(16, 0)),
            })
        entry_values.update(values)
        model = self._model('restaurant.attendance.entry', self.reception)
        if status in ('annual_leave', 'sick_leave', 'emergency_leave'):
            model = model.sudo().with_context(restaurant_legacy_leave_import=True)
        return model.create(entry_values)

    def _submit(self, entry, manager=None):
        if entry.status in ('annual_leave', 'sick_leave', 'emergency_leave'):
            entry.sudo().with_context(restaurant_legacy_leave_import=True).action_submit()
            entry.sudo().with_context(
                restaurant_legacy_leave_import=True,
            ).action_submit_to_hr()
        else:
            entry.with_user(self.reception).action_submit()
            entry.with_user(manager or self.manager).action_submit_to_hr()
        return entry.with_user(self.hr)

    def test_01_hr_queue_action_domains(self):
        expected_domains = {
            'restaurant_hr_awaiting_action': [('state', '=', 'submitted_to_hr')],
            'restaurant_hr_sync_errors_action': [
                ('state', '=', 'submitted_to_hr'), ('sync_status', '=', 'error'),
            ],
            'restaurant_hr_approved_attendance_action': [
                ('state', '=', 'approved'),
                ('status', 'in', ['present', 'late', 'overtime_day']),
            ],
            'restaurant_hr_exceptions_action': [
                ('status', 'in', ['absent', 'late']),
                ('state', 'in', ['submitted_to_hr', 'approved', 'rejected']),
            ],
            'restaurant_hr_leave_action': [
                ('status', 'in', ['annual_leave', 'sick_leave', 'emergency_leave']),
                ('state', 'in', ['submitted_to_hr', 'approved', 'rejected']),
            ],
            'restaurant_hr_all_decisions_action': [
                '|',
                ('state', 'in', ['approved', 'rejected']),
                ('hr_return_count', '>', 0),
            ],
        }
        for xmlid, expected in expected_domains.items():
            action = self.env.ref(f'restaurant_reception.{xmlid}')
            self.assertEqual(safe_eval(action.domain), expected)
            self.assertFalse(safe_eval(action.context).get('create', True))

        settings_action = self.env.ref(
            'restaurant_reception.restaurant_hr_settings_action'
        )
        self.assertEqual(
            safe_eval(settings_action.context).get('module'), 'restaurant_hr'
        )

    def test_02_hr_can_only_edit_hr_fields(self):
        hr_entry = self._submit(self._entry(status='annual_leave'))
        with self.assertRaises(AccessError):
            hr_entry.write({'attendance_date': self.base_date + timedelta(days=1)})
        with self.assertRaises(AccessError):
            hr_entry.write({'overtime_hours': 9})
        hr_entry.write({
            'hr_note': 'Checked by HR.',
            'decision_reason': 'Document verified.',
            'official_leave_type_id': self.leave_type.id,
        })
        self.assertEqual(hr_entry.official_leave_type_id, self.leave_type)

    def test_03_reject_and_hr_return_require_reason(self):
        rejected = self._submit(self._entry(status='absent', with_times=False))
        with self.assertRaises(ValidationError):
            rejected.action_reject()
        rejected.decision_reason = 'Unexcused absence after HR review.'
        rejected.action_reject()
        self.assertEqual(rejected.state, 'rejected')
        self.assertEqual(rejected.sync_status, 'not_required')

        returned = self._submit(self._entry(day=1))
        with self.assertRaises(ValidationError):
            returned.action_return_to_reception()
        returned.decision_reason = 'Reception must correct the check-out.'
        returned.action_return_to_reception()
        source = returned.sudo()
        self.assertEqual(source.state, 'draft')
        self.assertEqual(source.hr_return_count, 1)
        self.assertEqual(
            source.last_hr_return_reason,
            'Reception must correct the check-out.',
        )

    def test_04_return_requires_reception_edit_and_manager_rereview(self):
        entry = self._entry()
        hr_entry = self._submit(entry)
        hr_entry.decision_reason = 'Correct the recorded check-in.'
        hr_entry.action_return_to_reception()

        reception_entry = entry.with_user(self.reception)
        reception_entry.check_in = datetime.combine(self.base_date, time(7, 45))
        reception_entry.action_submit()
        self.assertEqual(entry.state, 'manager_review')
        with self.assertRaises(AccessError):
            entry.with_user(self.hr).action_approve()
        entry.with_user(self.manager).action_submit_to_hr()
        entry.with_user(self.hr).action_approve()
        self.assertEqual(entry.state, 'approved')
        self.assertEqual(entry.official_attendance_id.check_in.time(), time(7, 45))

    def test_05_present_and_overnight_attendance_sync(self):
        normal = self._entry()
        self._submit(normal).action_approve()
        self.assertEqual(normal.state, 'approved')
        self.assertEqual(normal.sync_status, 'synced')
        self.assertEqual(normal.official_attendance_id.employee_id, normal.employee_id)
        self.assertEqual(normal.official_attendance_id.check_in, normal.check_in)
        self.assertEqual(normal.official_attendance_id.check_out, normal.check_out)

        overnight_date = self.base_date + timedelta(days=1)
        overnight = self._entry(
            status='late',
            day=1,
            check_in=datetime.combine(overnight_date, time(20, 0)),
            check_out=datetime.combine(overnight_date + timedelta(days=1), time(2, 0)),
        )
        self._submit(overnight).action_approve()
        self.assertGreater(
            overnight.official_attendance_id.check_out,
            overnight.official_attendance_id.check_in,
        )

    def test_06_missing_times_are_blocked_before_manager_review(self):
        entry = self._entry(with_times=False)
        with self.assertRaises(ValidationError):
            entry.with_user(self.reception).action_submit()
        self.assertEqual(entry.state, 'draft')
        self.assertEqual(entry.sync_status, 'pending')
        self.assertFalse(entry.official_attendance_id)

    def test_reception_cannot_create_leave_and_nonworking_entries_have_no_times(self):
        operational_statuses = dict(
            self.env['restaurant.attendance.entry']
            ._fields['operational_status']
            ._description_selection(self.env)
        )
        self.assertEqual(
            set(operational_statuses),
            {'pending', 'present', 'absent', 'late', 'day_off', 'overtime_day'},
        )
        for legacy_status in ('annual_leave', 'sick_leave', 'emergency_leave'):
            self.assertNotIn(legacy_status, operational_statuses)

        reception_action = self.env.ref(
            'restaurant_reception.restaurant_attendance_entry_action'
        )
        self.assertEqual(safe_eval(reception_action.domain), [
            ('status', 'in', [
                'present', 'absent', 'late', 'day_off', 'overtime_day',
            ]),
        ])
        with self.assertRaises(ValidationError):
            self._model('restaurant.attendance.entry', self.reception).create({
                'branch_id': self.branch.id,
                'employee_id': self._employee().id,
                'attendance_date': self.base_date,
                'status': 'annual_leave',
            })
        with self.assertRaises(ValidationError):
            self._entry(status='day_off', check_in=datetime.combine(
                self.base_date, time(8, 0),
            ))

    def test_07_leave_mapping_creates_submitted_time_off(self):
        entry = self._entry(status='annual_leave', with_times=False)
        self._submit(entry).action_approve()
        leave = entry.official_leave_id
        self.assertTrue(leave)
        self.assertEqual(leave.restaurant_attendance_entry_id, entry)
        self.assertEqual(leave.employee_id, entry.employee_id)
        self.assertEqual(leave.work_entry_type_id, self.leave_type)
        self.assertEqual(leave.request_date_from, entry.attendance_date)
        self.assertEqual(leave.request_date_to, entry.attendance_date)
        self.assertEqual(leave.state, 'confirm')
        self.assertFalse(entry.official_attendance_id)

    def test_08_missing_leave_mapping_is_retryable(self):
        self.company.restaurant_sick_leave_type_id = False
        entry = self._entry(status='sick_leave', with_times=False)
        hr_entry = self._submit(entry)
        hr_entry.action_approve()
        self.assertEqual(entry.state, 'submitted_to_hr')
        self.assertEqual(entry.sync_status, 'error')
        self.assertFalse(entry.official_leave_id)

        self.company.restaurant_sick_leave_type_id = self.leave_type
        hr_entry.action_retry_sync()
        self.assertEqual(entry.state, 'approved')
        self.assertEqual(entry.sync_attempt_count, 2)
        self.assertEqual(entry.official_leave_id.state, 'confirm')

    def test_09_absent_and_day_off_create_no_fake_attendance(self):
        before = self.env['hr.attendance'].search_count([
            ('restaurant_attendance_entry_id', '!=', False),
        ])
        for day, status in enumerate(('absent', 'day_off')):
            entry = self._entry(status=status, day=day, with_times=False)
            self._submit(entry).action_approve()
            self.assertEqual(entry.state, 'approved')
            self.assertEqual(entry.sync_status, 'not_required')
            self.assertFalse(entry.official_attendance_id)
            self.assertFalse(entry.official_leave_id)
        self.assertEqual(
            self.env['hr.attendance'].search_count([
                ('restaurant_attendance_entry_id', '!=', False),
            ]),
            before,
        )

    def test_10_overtime_ledger_is_linked_and_immutable(self):
        entry = self._entry(overtime_hours=2.5)
        self._submit(entry).action_approve()
        ledger = entry.overtime_ledger_id
        self.assertTrue(ledger)
        self.assertEqual(ledger.source_entry_id, entry)
        self.assertEqual(ledger.employee_id, entry.employee_id)
        self.assertEqual(ledger.company_id, entry.company_id)
        self.assertEqual(ledger.branch_id, entry.branch_id)
        self.assertEqual(ledger.attendance_date, entry.attendance_date)
        self.assertEqual(ledger.hours, 2.5)
        self.assertEqual(ledger.approved_by, self.hr)
        self.assertTrue(ledger.approved_at)
        with self.assertRaises(AccessError):
            ledger.with_user(self.hr).write({'hours': 10})
        with self.assertRaises(AccessError):
            ledger.sudo().unlink()

    def test_overtime_day_calculates_hours_and_syncs_attendance(self):
        entry = self._entry(status='overtime_day')
        entry.with_user(self.reception).action_submit()
        self.assertEqual(entry.overtime_hours, 8)
        entry.with_user(self.manager).action_submit_to_hr()
        entry.with_user(self.hr).action_approve()

        self.assertEqual(entry.state, 'approved')
        self.assertEqual(entry.sync_status, 'synced')
        self.assertTrue(entry.official_attendance_id)
        self.assertTrue(entry.overtime_ledger_id)
        self.assertEqual(entry.overtime_ledger_id.hours, 8)

    def test_11_dual_role_cannot_approve_personally_reviewed_entry(self):
        own = self._entry()
        own.action_submit()
        own.with_user(self.dual_role).action_submit_to_hr()
        with self.assertRaises(AccessError):
            own.with_user(self.dual_role).action_approve()

        other = self._entry(day=1)
        other.action_submit()
        other.with_user(self.manager).action_submit_to_hr()
        other.with_user(self.dual_role).action_approve()
        self.assertEqual(other.state, 'approved')
        self.assertEqual(other.hr_reviewed_by, self.dual_role)

    def test_12_disabled_sync_and_retry_are_idempotent(self):
        self.company.restaurant_hr_official_sync_enabled = False
        entry = self._entry()
        hr_entry = self._submit(entry)
        hr_entry.action_approve()
        self.assertEqual(entry.state, 'submitted_to_hr')
        self.assertEqual(entry.sync_status, 'error')
        self.assertFalse(entry.official_attendance_id)

        self.company.restaurant_hr_official_sync_enabled = True
        hr_entry.action_retry_sync()
        target = entry.official_attendance_id
        self.assertEqual(entry.state, 'approved')
        self.assertEqual(entry.sync_attempt_count, 2)
        self.assertEqual(self.env['hr.attendance'].search_count([
            ('restaurant_attendance_entry_id', '=', entry.id),
        ]), 1)
        self.assertEqual(target.restaurant_attendance_entry_id, entry)

    def test_13_existing_source_link_is_updated_without_duplicate(self):
        entry = self._entry()
        hr_entry = self._submit(entry)
        target = self.env['hr.attendance'].sudo().with_context(
            restaurant_hr_sync=True,
        ).create({
            'employee_id': entry.employee_id.id,
            'check_in': entry.check_in - timedelta(minutes=15),
            'check_out': entry.check_out,
            'restaurant_attendance_entry_id': entry.id,
        })
        hr_entry.action_approve()
        self.assertEqual(entry.official_attendance_id, target)
        self.assertEqual(target.check_in, entry.check_in)
        self.assertEqual(self.env['hr.attendance'].search_count([
            ('restaurant_attendance_entry_id', '=', entry.id),
        ]), 1)

    @mute_logger('odoo.sql_db')
    def test_14_unique_source_constraints_prevent_duplicates(self):
        entry = self._entry()
        target_values = {
            'employee_id': entry.employee_id.id,
            'check_in': entry.check_in,
            'check_out': entry.check_out,
            'restaurant_attendance_entry_id': entry.id,
        }
        attendance_model = self.env['hr.attendance'].sudo().with_context(
            restaurant_hr_sync=True,
        )
        attendance_model.create(target_values)
        with self.assertRaises(IntegrityError), self.env.cr.savepoint():
            attendance_model.create({
                **target_values,
                'check_in': entry.check_in + timedelta(days=1),
                'check_out': entry.check_out + timedelta(days=1),
            })

        leave_entry = self._entry(
            status='annual_leave', day=1, with_times=False,
        )
        leave_model = self.env['hr.leave'].sudo().with_context(
            restaurant_hr_sync=True,
            leave_fast_create=True,
        )
        leave_values = {
            'employee_id': leave_entry.employee_id.id,
            'work_entry_type_id': self.leave_type.id,
            'request_date_from': leave_entry.attendance_date,
            'request_date_to': leave_entry.attendance_date,
            'restaurant_attendance_entry_id': leave_entry.id,
        }
        leave_model.create(leave_values)
        with self.assertRaises(IntegrityError), self.env.cr.savepoint():
            leave_model.create({
                **leave_values,
                'request_date_from': leave_entry.attendance_date + timedelta(days=1),
                'request_date_to': leave_entry.attendance_date + timedelta(days=1),
            })

        overtime_entry = self._entry(day=2, overtime_hours=1)
        ledger_model = self.env['restaurant.overtime.ledger'].sudo().with_context(
            restaurant_hr_sync=True,
        )
        ledger_values = {
            'source_entry_id': overtime_entry.id,
            'employee_id': overtime_entry.employee_id.id,
            'company_id': overtime_entry.company_id.id,
            'branch_id': overtime_entry.branch_id.id,
            'attendance_date': overtime_entry.attendance_date,
            'hours': 1,
            'approved_by': self.hr.id,
            'approved_at': datetime.combine(overtime_entry.attendance_date, time(17, 0)),
        }
        ledger_model.create(ledger_values)
        with self.assertRaises(IntegrityError), self.env.cr.savepoint():
            ledger_model.create({**ledger_values, 'hours': 2})

    def test_15_sync_failure_rolls_back_partial_official_records(self):
        employee = self._employee()
        entry = self._entry(employee=employee, overtime_hours=3)
        self.env['hr.attendance'].sudo().create({
            'employee_id': employee.id,
            'check_in': entry.check_in,
            'check_out': entry.check_out,
        })
        self._submit(entry).action_approve()
        self.assertEqual(entry.state, 'submitted_to_hr')
        self.assertEqual(entry.sync_status, 'error')
        self.assertFalse(entry.official_attendance_id)
        self.assertFalse(entry.overtime_ledger_id)
        self.assertFalse(self.env['restaurant.overtime.ledger'].sudo().search([
            ('source_entry_id', '=', entry.id),
        ]))

    def test_16_company_consistency_and_approved_lock(self):
        other_company = self.env['res.company'].create({'name': 'HR Other Company'})
        employee = self._employee(company=other_company)
        self.reception.write({'company_ids': [Command.link(other_company.id)]})
        with self.assertRaises(ValidationError):
            self._entry(employee=employee)

        entry = self._entry(day=1)
        self._submit(entry).action_approve()
        for user in (self.reception, self.manager, self.hr):
            with self.assertRaises(AccessError):
                entry.with_user(user).write({'reception_note': 'Tamper attempt'})

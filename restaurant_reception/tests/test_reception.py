from datetime import date

from psycopg2 import IntegrityError

from odoo import Command
from odoo.exceptions import AccessError, UserError, ValidationError
from odoo.tests import Form
from odoo.tests.common import new_test_user
from odoo.tools import mute_logger

from odoo.addons.base.tests.common import BaseCommon


class TestRestaurantReception(BaseCommon):
    _test_user_groups = ()

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.branch, cls.other_branch = cls.env['restaurant.branch'].create([
            {'name': 'Reception Test Downtown', 'code': 'REC-DT', 'company_id': cls.company.id},
            {'name': 'Reception Test Marina', 'code': 'REC-MA', 'company_id': cls.company.id},
        ])
        cls.employee, cls.other_employee = cls.env['hr.employee'].create([
            {'name': 'Reception Test Waiter One', 'company_id': cls.company.id},
            {'name': 'Reception Test Waiter Two', 'company_id': cls.company.id},
        ])
        cls.reception = cls._make_user('reception_operator', 'restaurant_core.group_restaurant_reception', cls.branch)
        cls.manager = cls._make_user('reception_manager', 'restaurant_core.group_restaurant_branch_manager', cls.branch)
        cls.hr = cls._make_user('reception_hr', 'restaurant_core.group_restaurant_hr')
        cls.owner = cls._make_user('reception_owner', 'restaurant_core.group_restaurant_owner')
        cls.operations = cls._make_user('reception_operations', 'restaurant_core.group_restaurant_operations_manager')
        cls.outsider = cls._make_user('reception_outsider', 'base.group_user')
        cls.portal = cls._make_user('reception_portal', 'base.group_portal')
        cls.today = date(2026, 9, 29)

    @classmethod
    def _make_user(cls, login, groups, branch=None):
        return new_test_user(
            cls.env, login=login, groups=groups, company_id=cls.company.id,
            company_ids=[Command.set(cls.company.ids)],
            restaurant_branch_ids=[Command.set(branch.ids if branch else [])],
        )

    def _model(self, name, user=None):
        return self.env[name].with_user(user or self.reception).with_context(
            allowed_company_ids=[self.company.id], tracking_disable=False, mail_notrack=False,
        )

    def _closing(self, user=None, **values):
        return self._model('restaurant.daily.closing', user).create({
            'branch_id': self.branch.id, 'closing_date': self.today, 'shift': 'full_day', **values,
        })

    def _attendance(self, user=None, **values):
        return self._model('restaurant.attendance.entry', user).create({
            'branch_id': self.branch.id, 'employee_id': self.employee.id,
            'attendance_date': self.today, **values,
        })

    def _line(self, closing, **values):
        return self._model('restaurant.waiter.daily.line').create({
            'closing_id': closing.id, 'employee_id': self.employee.id,
            'sales_amount': 100, 'tips_amount': 10, 'order_count': 5, 'table_count': 3, **values,
        })

    def _submitted_attendance(self):
        entry = self._attendance()
        entry.action_submit()
        entry.with_user(self.manager).action_submit_to_hr()
        return entry

    def test_01_reception_creates_assigned_closing(self):
        closing = self._closing()
        self.assertEqual(closing.state, 'draft')
        self.assertEqual(closing.branch_id, self.branch)
        self.assertEqual(closing.reception_user_id, self.reception)
        self.assertTrue(closing.name.startswith('RDC/'))
        self.assertEqual(closing.company_id, self.company)
        self.assertEqual(closing.currency_id, self.company.currency_id)

    def test_02_unassigned_branch_isolation(self):
        closing = self._closing(user=self.env.user, branch_id=self.other_branch.id)
        entry = self._attendance(user=self.env.user, branch_id=self.other_branch.id)
        line = self.env['restaurant.waiter.daily.line'].create({
            'closing_id': closing.id, 'employee_id': self.employee.id,
        })
        for user in (self.reception, self.manager):
            for record in (closing, entry, line):
                with self.subTest(user=user.login, model=record._name):
                    other = record.with_user(user)
                    self.assertFalse(other.search([('id', '=', other.id)]))
                    with self.assertRaises(AccessError):
                        other.read(['branch_id'])
            with self.assertRaises(AccessError), self.env.cr.savepoint():
                self._closing(user=user, branch_id=self.other_branch.id, shift='morning')
            with self.assertRaises(AccessError), self.env.cr.savepoint():
                self._attendance(user=user, branch_id=self.other_branch.id, employee_id=self.other_employee.id)

    def test_03_sales_totals_recompute(self):
        closing = self._closing()
        line = self._line(closing)
        second = self._line(closing, employee_id=self.other_employee.id, sales_amount=250)
        self.assertEqual(closing.total_waiter_sales, 350)
        self.assertEqual(closing.total_orders, 10)
        self.assertEqual(closing.total_tables, 6)
        line.write({'sales_amount': 200, 'order_count': 8})
        self.assertEqual(closing.total_waiter_sales, 450)
        self.assertEqual(closing.total_orders, 13)
        second.unlink()
        self.assertEqual(closing.total_waiter_sales, 200)

    def test_04_tips_totals_and_percentage(self):
        closing = self._closing()
        line = self._line(closing)
        self._line(closing, employee_id=self.other_employee.id, tips_amount=15)
        self.assertEqual(closing.total_tips, 25)
        self.assertEqual(line.tip_percentage, 10)
        line.tips_amount = 20
        self.assertEqual(closing.total_tips, 35)
        line.sales_amount = 0
        self.assertEqual(line.tip_percentage, 0)

    @mute_logger('odoo.sql_db')
    def test_05_negative_waiter_values_rejected(self):
        closing = self._closing()
        for field in ('sales_amount', 'tips_amount', 'order_count', 'table_count'):
            with self.subTest(field=field):
                with self.assertRaises(IntegrityError), self.env.cr.savepoint():
                    self._line(closing, **{field: -1})
        line = self._line(closing)
        with self.assertRaises(IntegrityError), self.env.cr.savepoint():
            line.write({'tips_amount': -2})
            self.env.flush_all()

    def test_06_reception_submits_closing(self):
        closing = self._closing()
        closing.action_submit()
        self.assertEqual(closing.state, 'manager_review')
        self.assertTrue(closing.message_ids.filtered(lambda message: 'Workflow changed' in str(message.body)))

    def test_07_reception_cannot_confirm_closing(self):
        closing = self._closing()
        closing.action_submit()
        with self.assertRaises(AccessError):
            closing.action_confirm()
        with self.assertRaises(AccessError):
            closing.write({'state': 'confirmed'})

    def test_08_manager_confirms_assigned_closing(self):
        closing = self._closing()
        closing.action_submit()
        closing.with_user(self.manager).action_confirm()
        self.assertEqual(closing.state, 'confirmed')
        self.assertEqual(closing.branch_manager_id, self.manager)
        self.assertTrue(closing.manager_reviewed_at)

    def test_09_reception_creates_attendance_draft(self):
        entry = self._attendance()
        self.assertEqual(entry.state, 'draft')
        self.assertEqual(entry.entered_by, self.reception)
        self.assertEqual(entry.overtime_hours, 0)

    def test_10_reception_submits_attendance_to_manager(self):
        entry = self._attendance()
        entry.action_submit()
        self.assertEqual(entry.state, 'manager_review')

    def test_11_reception_cannot_submit_to_hr(self):
        entry = self._attendance()
        with self.assertRaises(UserError):
            entry.action_submit_to_hr()
        entry.action_submit()
        with self.assertRaises(AccessError):
            entry.action_submit_to_hr()

    def test_12_manager_submits_to_hr(self):
        entry = self._attendance()
        entry.action_submit()
        manager_entry = entry.with_user(self.manager)
        manager_entry.manager_note = 'Reviewed against the shift roster.'
        manager_entry.action_submit_to_hr()
        self.assertEqual(entry.state, 'submitted_to_hr')
        self.assertEqual(entry.manager_reviewed_by, self.manager)
        self.assertTrue(entry.manager_reviewed_at)

    def test_13_manager_cannot_approve_as_hr(self):
        entry = self._submitted_attendance()
        with self.assertRaises(AccessError):
            entry.with_user(self.manager).action_approve()
        self.manager.write({'group_ids': [Command.link(self.env.ref('restaurant_core.group_restaurant_hr').id)]})
        with self.assertRaises(AccessError):
            entry.with_user(self.manager).action_approve()
        with self.assertRaises(AccessError):
            entry.with_user(self.manager).action_reject()

    def test_14_hr_approves_submitted_attendance_only(self):
        entry = self._attendance()
        self.assertFalse(self._model(entry._name, self.hr).search([('id', '=', entry.id)]))
        entry.action_submit()
        self.assertFalse(self._model(entry._name, self.hr).search([('id', '=', entry.id)]))
        entry.with_user(self.manager).action_submit_to_hr()
        hr_entry = entry.with_user(self.hr)
        self.assertTrue(hr_entry.branch_id.display_name)
        hr_entry.hr_note = 'Approved for the next HR processing step.'
        before = self.env['hr.attendance'].search_count([])
        hr_entry.action_approve()
        self.assertEqual(entry.state, 'approved')
        self.assertEqual(entry.hr_reviewed_by, self.hr)
        self.assertTrue(entry.hr_reviewed_at)
        self.assertEqual(self.env['hr.attendance'].search_count([]), before)

    @mute_logger('odoo.sql_db')
    def test_15_duplicate_attendance_rejected(self):
        self._attendance()
        with self.assertRaises(IntegrityError), self.env.cr.savepoint():
            self._attendance()

    @mute_logger('odoo.sql_db')
    def test_16_invalid_time_and_overtime_rejected(self):
        with self.assertRaises(IntegrityError), self.env.cr.savepoint():
            self._attendance(check_in='2026-09-29 12:00:00', check_out='2026-09-29 11:00:00')
        with self.assertRaises(IntegrityError), self.env.cr.savepoint():
            self._attendance(overtime_hours=-1)
        entry = self._attendance(check_in='2026-09-29 20:00:00', check_out='2026-09-30 02:00:00')
        self.assertTrue(entry.check_out > entry.check_in)

    def test_17_owner_operations_cross_branch_visibility(self):
        closings = self._closing() | self._closing(user=self.env.user, branch_id=self.other_branch.id)
        entries = self._attendance() | self._attendance(user=self.env.user, branch_id=self.other_branch.id)
        for user in (self.owner, self.operations):
            for records in (closings, entries):
                found = self._model(records._name, user).search([('id', 'in', records.ids)])
                self.assertEqual(set(found.ids), set(records.ids))

    def test_18_terminal_records_and_lines_are_locked(self):
        closing = self._closing()
        line = self._line(closing)
        closing.action_submit()
        closing.with_user(self.manager).action_confirm()
        for user in (self.reception, self.manager):
            for operation in (
                lambda: closing.with_user(user).write({'notes': 'tamper'}),
                lambda: line.with_user(user).write({'sales_amount': 999}),
                lambda: line.with_user(user).unlink(),
                lambda: closing.with_user(user).unlink(),
                lambda: self._line(closing),
                lambda: closing.with_user(user).write({'state': 'draft'}),
            ):
                with self.assertRaises((AccessError, UserError)), self.env.cr.savepoint():
                    operation()
        entry = self._submitted_attendance()
        entry.with_user(self.hr).action_approve()
        for user in (self.reception, self.manager, self.hr):
            with self.assertRaises(AccessError):
                entry.with_user(user).write({'overtime_hours': 10})
            with self.assertRaises(AccessError):
                entry.with_user(user).write({'hr_note': 'tampered'})

    def test_19_return_and_rejection_paths(self):
        closing = self._closing()
        closing.action_submit()
        closing.with_user(self.manager).action_return_to_draft()
        closing.notes = 'Corrected'
        entry = self._attendance()
        entry.action_submit()
        entry.with_user(self.manager).action_return_to_draft()
        entry.reception_note = 'Corrected time'
        entry.action_submit()
        entry.with_user(self.manager).action_submit_to_hr()
        entry.with_user(self.hr).action_reject()
        self.assertEqual(entry.state, 'rejected')
        self.assertEqual(entry.hr_reviewed_by, self.hr)

    @mute_logger('odoo.sql_db')
    def test_20_active_closing_uniqueness_and_cancellation(self):
        closing = self._closing()
        with self.assertRaises(IntegrityError), self.env.cr.savepoint():
            self._closing()
        self._closing(shift='morning')
        closing.action_cancel()
        replacement = self._closing()
        self.assertNotEqual(closing.name, replacement.name)
        self.assertEqual(replacement.state, 'draft')

    def test_21_rpc_state_and_review_spoofing(self):
        for model, values in (
            ('restaurant.daily.closing', {'branch_id': self.branch.id, 'state': 'confirmed'}),
            ('restaurant.attendance.entry', {'branch_id': self.branch.id, 'employee_id': self.employee.id, 'state': 'approved'}),
        ):
            with self.assertRaises(AccessError):
                self._model(model).create(values)
        closing = self._closing()
        entry = self._attendance()
        for record, vals in (
            (closing, {'branch_manager_id': self.manager.id}),
            (closing, {'total_waiter_sales': 1000}),
            (entry, {'manager_reviewed_by': self.manager.id}),
            (entry, {'hr_reviewed_by': self.hr.id}),
            (entry, {'manager_note': 'fake approval'}),
            (entry, {'hr_note': 'fake approval'}),
        ):
            with self.assertRaises(AccessError):
                record.write(vals)
        entry.action_submit()
        entry.with_user(self.manager).action_submit_to_hr()
        for action in ('action_approve', 'action_reject'):
            with self.assertRaises(AccessError):
                getattr(entry, action)()

    def test_22_branch_transfer_and_line_move_denied(self):
        closing = self._closing()
        entry = self._attendance()
        for record in (closing, entry):
            with self.assertRaises(AccessError):
                record.write({'branch_id': self.other_branch.id})
        line = self._line(closing)
        other = self._closing(shift='morning')
        with self.assertRaises(AccessError):
            line.write({'closing_id': other.id})

    def test_23_outsiders_have_no_access(self):
        for user in (self.outsider, self.portal, self.env.ref('base.public_user')):
            for model in ('restaurant.daily.closing', 'restaurant.waiter.daily.line', 'restaurant.attendance.entry'):
                with self.assertRaises(AccessError):
                    self._model(model, user).search([])

    def test_24_public_employee_lookup_without_hr_access(self):
        employees = self._model('hr.employee').search([('id', '=', self.employee.id)])
        self.assertEqual(employees.ids, self.employee.ids)
        self.assertEqual(employees.display_name, self.employee.name)
        self.assertFalse(self._model('hr.employee').has_access('write'))
        self.assertFalse(self._model('hr.attendance').has_access('create'))

    def test_25_forms_support_draft_entry(self):
        with Form(self._model('restaurant.daily.closing')) as form:
            form.branch_id = self.branch
            form.closing_date = self.today
            with form.waiter_line_ids.new() as line:
                line.employee_id = self.employee
                line.sales_amount = 120
                line.tips_amount = 12
        self.assertEqual(form.record.total_waiter_sales, 120)
        with Form(self._model('restaurant.attendance.entry')) as form:
            form.branch_id = self.branch
            form.employee_id = self.employee
            form.attendance_date = self.today
        self.assertEqual(form.record.state, 'draft')

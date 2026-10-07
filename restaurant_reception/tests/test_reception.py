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
        cls.company.restaurant_hr_official_sync_enabled = True

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

    def _closing(self, user=None, legacy=True, **values):
        closing = self._model('restaurant.daily.closing', user).create({
            'branch_id': self.branch.id, 'closing_date': self.today, 'shift': 'full_day', **values,
        })
        closing.sudo().with_context(service_tracking_migration=True).write({
            'service_tracking_mode': 'legacy' if legacy else 'service_entries',
        })
        return closing

    def _attendance(self, user=None, **values):
        return self._model('restaurant.attendance.entry', user).create({
            'branch_id': self.branch.id, 'employee_id': self.employee.id,
            'attendance_date': self.today,
            'check_in': '2026-09-29 08:00:00',
            'check_out': '2026-09-29 16:00:00',
            **values,
        })

    def _line(self, closing, **values):
        return self._model('restaurant.waiter.daily.line').create({
            'closing_id': closing.id, 'employee_id': self.employee.id,
            'sales_amount': 100, 'tips_amount': 10, 'order_count': 5, 'table_count': 3, **values,
        })

    def _service_entry(self, closing, **values):
        sequence = len(closing.service_entry_ids) + 1
        return self._model('restaurant.waiter.service.entry').create({
            'closing_id': closing.id,
            'service_type': 'dine_in',
            'employee_id': self.employee.id,
            'bill_reference': f'BILL-{sequence}',
            'table_reference': f'T-{sequence}',
            'guest_count': 1,
            'bill_amount': 100,
            'cash_tip_amount': 0,
            'card_tip_amount': 10,
            'review_count': 0,
            **values,
        })

    def _payment(self, closing, **values):
        return self._model('restaurant.reception.payment.line').create({
            'closing_id': closing.id,
            'payment_type': 'cash',
            'amount': 100,
            **values,
        })

    def _tip(self, closing, **values):
        return self._model('restaurant.reception.tip.line').create({
            'closing_id': closing.id,
            'tip_source': 'cash',
            'amount': 10,
            **values,
        })

    def _discount(self, closing, **values):
        return self._model('restaurant.reception.discount').create({
            'closing_id': closing.id,
            'order_reference': 'CHK-100',
            'item_description': 'Synthetic Discount Item',
            'quantity': 1,
            'amount': 5,
            'reason': 'Synthetic test discount',
            **values,
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
                self._closing(
                    user=user,
                    branch_id=self.other_branch.id,
                    closing_date=date(2026, 9, 30),
                )
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
        self.assertEqual(closing.submitted_by_id, self.reception)
        self.assertTrue(closing.submitted_at)
        self.assertEqual(closing.submission_count, 1)
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
        self.assertEqual(self.env['hr.attendance'].search_count([]), before + 1)
        self.assertEqual(entry.official_attendance_id.employee_id, self.employee)

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
        manager_closing = closing.with_user(self.manager)
        with self.assertRaises(ValidationError):
            manager_closing.action_return_to_draft()
        manager_closing.correction_reason = 'Correct the card settlement.'
        manager_closing.action_return_to_draft()
        self.assertEqual(closing.reopen_count, 1)
        self.assertEqual(closing.last_reopened_by_id, self.manager)
        self.assertTrue(closing.last_reopened_at)
        closing.notes = 'Corrected'
        entry = self._attendance()
        entry.action_submit()
        entry.with_user(self.manager).action_return_to_draft()
        entry.reception_note = 'Corrected time'
        entry.action_submit()
        entry.with_user(self.manager).action_submit_to_hr()
        hr_entry = entry.with_user(self.hr)
        hr_entry.decision_reason = 'The manager review does not support this entry.'
        hr_entry.action_reject()
        self.assertEqual(entry.state, 'rejected')
        self.assertEqual(entry.hr_reviewed_by, self.hr)

    @mute_logger('odoo.sql_db')
    def test_20_active_closing_uniqueness_and_cancellation(self):
        closing = self._closing()
        with self.assertRaises(IntegrityError), self.env.cr.savepoint():
            self._closing()
        with self.assertRaises(IntegrityError), self.env.cr.savepoint():
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
        other = self._closing(closing_date=date(2026, 9, 30))
        with self.assertRaises(AccessError):
            line.write({'closing_id': other.id})

    def test_23_outsiders_have_no_access(self):
        for user in (self.outsider, self.portal, self.env.ref('base.public_user')):
            for model in (
                'restaurant.daily.closing',
                'restaurant.waiter.daily.line',
                'restaurant.waiter.service.entry',
                'restaurant.reception.payment.line',
                'restaurant.reception.tip.line',
                'restaurant.reception.discount',
                'restaurant.attendance.entry',
            ):
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
            form.reported_total_sales = 120
            form.guest_count = 4
            with form.payment_line_ids.new() as payment:
                payment.payment_type = 'cash'
                payment.amount = 120
        self.assertEqual(form.record.payment_total, 120)

        with Form(self._model('restaurant.waiter.sales.closing')) as waiter_form:
            waiter_form.branch_id = self.branch
            waiter_form.business_date = self.today
            with waiter_form.bill_ids.new() as entry:
                entry.service_type = 'dine_in'
                entry.bill_reference = 'FORM-1'
                entry.employee_id = self.employee
                entry.table_reference = 'FORM-T1'
                entry.guest_count = 4
                entry.bill_amount = 120
                entry.card_tip_amount = 12
        self.assertEqual(waiter_form.record.total_sales, 120)
        self.assertEqual(waiter_form.record.total_tips, 12)
        self.assertEqual(form.record.waiter_sales_closing_id, waiter_form.record)
        with Form(self._model('restaurant.attendance.entry')) as form:
            form.branch_id = self.branch
            form.employee_id = self.employee
            form.attendance_date = self.today
        self.assertEqual(form.record.state, 'draft')

    def test_26_financial_breakdowns_totals_and_summary(self):
        closing = self._closing(
            reported_total_sales=105,
            reported_vat_amount=5,
            guest_count=42,
            opening_cash_float=200,
            closing_cash_float=260,
        )
        self._payment(closing, amount=65)
        self._payment(
            closing,
            payment_type='card',
            method_name='Visa terminal',
            amount=35,
        )
        self._payment(
            closing,
            payment_type='delivery',
            method_name='Synthetic Delivery',
            amount=5,
        )
        self._line(closing, tips_amount=30)
        self._tip(closing, amount=25)
        self._tip(
            closing,
            tip_source='card',
            source_name='Visa terminal',
            amount=5,
        )
        self._discount(closing, amount=12)
        self._discount(
            closing,
            order_reference='CHK-101',
            item_description='Second Synthetic Item',
            amount=8,
        )

        self.assertEqual(closing.cash_payment_total, 65)
        self.assertEqual(closing.card_payment_total, 35)
        self.assertEqual(closing.delivery_payment_total, 5)
        self.assertEqual(closing.payment_total, 105)
        self.assertEqual(closing.payment_difference, 0)
        self.assertEqual(closing.calculated_vat_amount, 5)
        self.assertEqual(closing.net_sales_excluding_vat, 100)
        self.assertEqual(closing.reported_vat_amount, 5)
        self.assertEqual(closing.vat_difference, 0)
        self.assertEqual(closing.reported_total_tips, 30)
        self.assertEqual(closing.tip_allocation_difference, 0)
        self.assertEqual(closing.total_discounts, 20)
        summary = closing.get_whatsapp_summary()
        self.assertIn('Reception Daily Summary', summary)
        self.assertIn('Guests: 42', summary)
        self.assertIn('Total Sales:', summary)
        self.assertIn('Net Sales (Excl. VAT):', summary)
        self.assertIn('VAT (Calculated 5%):', summary)
        self.assertIn('VAT Comparison:', summary)
        self.assertIn('Discounts:', summary)

        image_reference = self._closing(
            closing_date=date(2026, 9, 30),
            reported_total_sales=1735,
            reported_vat_amount=82.62,
        )
        self.assertEqual(image_reference.calculated_vat_amount, 82.62)
        self.assertEqual(image_reference.net_sales_excluding_vat, 1652.38)
        self.assertEqual(image_reference.vat_difference, 0)

    @mute_logger('odoo.sql_db')
    def test_27_financial_values_are_validated(self):
        with self.assertRaises(ValidationError):
            self._closing(reported_total_sales=-1)
        with self.assertRaises(ValidationError):
            self._closing(reported_vat_amount=-1)
        closing = self._closing()
        with self.assertRaises(IntegrityError), self.env.cr.savepoint():
            self._payment(closing, amount=-1)
        with self.assertRaises(ValidationError):
            self._payment(
                closing,
                payment_type='delivery',
                method_name='',
            )
        with self.assertRaises(IntegrityError), self.env.cr.savepoint():
            self._discount(closing, quantity=0)

    def test_28_financial_entries_lock_after_submission(self):
        closing = self._closing(reported_total_sales=100)
        payment = self._payment(closing)
        tip = self._tip(closing)
        discount = self._discount(closing)
        closing.action_submit()

        for operation in (
            lambda: payment.write({'amount': 90}),
            lambda: tip.unlink(),
            lambda: discount.write({'reason': 'Tampered'}),
            lambda: self._payment(closing),
        ):
            with self.assertRaises(AccessError), self.env.cr.savepoint():
                operation()

    def test_29_financial_entries_are_branch_isolated(self):
        closing = self._closing()
        self._payment(closing)
        self._tip(closing)
        self._discount(closing)
        other_closing = self._closing(
            user=self.env.user,
            branch_id=self.other_branch.id,
        )
        self.env['restaurant.reception.payment.line'].create({
            'closing_id': other_closing.id,
            'payment_type': 'cash',
            'amount': 10,
        })
        self.env['restaurant.reception.tip.line'].create({
            'closing_id': other_closing.id,
            'tip_source': 'cash',
            'amount': 2,
        })
        self.env['restaurant.reception.discount'].create({
            'closing_id': other_closing.id,
            'order_reference': 'OTHER-1',
            'item_description': 'Other Branch Item',
            'quantity': 1,
            'amount': 1,
            'reason': 'Synthetic',
        })
        for model_name in (
            'restaurant.reception.payment.line',
            'restaurant.reception.tip.line',
            'restaurant.reception.discount',
        ):
            model = self._model(model_name)
            self.assertFalse(model.search([('branch_id', '=', self.other_branch.id)]))

    def test_30_service_entries_reconcile_without_double_counting(self):
        closing = self._closing(
            legacy=False,
            reported_total_sales=300,
            guest_count=6,
        )
        self._service_entry(
            closing,
            bill_reference='DIN-1',
            table_reference='T-1',
            guest_count=2,
            bill_amount=100,
            cash_tip_amount=5,
            card_tip_amount=2,
            review_count=1,
        )
        self._service_entry(
            closing,
            bill_reference='DIN-2',
            table_reference='T-2',
            guest_count=3,
            bill_amount=80,
            cash_tip_amount=0,
            card_tip_amount=8,
            review_count=2,
        )
        self._service_entry(
            closing,
            employee_id=self.other_employee.id,
            bill_reference='DIN-3',
            table_reference='T-3',
            guest_count=1,
            bill_amount=50,
            cash_tip_amount=1,
            card_tip_amount=4,
        )
        self._service_entry(
            closing,
            service_type='pickup',
            employee_id=False,
            bill_reference='PICK-1',
            table_reference=False,
            guest_count=0,
            bill_amount=40,
            cash_tip_amount=3,
            card_tip_amount=0,
        )
        self._service_entry(
            closing,
            service_type='delivery',
            employee_id=False,
            bill_reference='DEL-1',
            table_reference=False,
            guest_count=0,
            bill_amount=30,
            cash_tip_amount=0,
            card_tip_amount=2,
        )

        self.assertEqual(closing.reported_total_sales, 300)
        self.assertEqual(closing.tracked_dine_in_sales, 230)
        self.assertEqual(closing.tracked_pickup_sales, 40)
        self.assertEqual(closing.tracked_delivery_sales, 30)
        self.assertEqual(closing.tracked_service_sales, 300)
        self.assertEqual(closing.service_sales_difference, 0)
        self.assertEqual(closing.tracked_guest_count, 6)
        self.assertEqual(closing.guest_count_difference, 0)
        self.assertEqual(closing.tracked_cash_tips, 9)
        self.assertEqual(closing.tracked_card_tips, 16)
        self.assertEqual(closing.reported_total_tips, 25)
        self.assertEqual(closing.service_bill_count, 5)
        self.assertEqual(closing.dine_in_bill_count, 3)
        self.assertEqual(closing.pickup_bill_count, 1)
        self.assertEqual(closing.delivery_bill_count, 1)
        self.assertEqual(closing.total_reviews, 3)

        self.assertEqual(len(closing.waiter_line_ids), 2)
        first = closing.waiter_line_ids.filtered(
            lambda line: line.employee_id == self.employee
        )
        second = closing.waiter_line_ids.filtered(
            lambda line: line.employee_id == self.other_employee
        )
        self.assertEqual(first.sales_amount, 180)
        self.assertEqual(first.order_count, 2)
        self.assertEqual(first.table_count, 2)
        self.assertEqual(first.guest_count, 5)
        self.assertEqual(first.cash_tip_amount, 5)
        self.assertEqual(first.card_tip_amount, 10)
        self.assertEqual(first.tips_amount, 15)
        self.assertEqual(first.review_count, 3)
        self.assertEqual(second.sales_amount, 50)
        self.assertEqual(second.tips_amount, 5)
        self.assertEqual(closing.total_waiter_sales, 230)
        self.assertEqual(closing.total_tips, 20)
        self.assertEqual(closing.total_orders, 3)
        self.assertEqual(closing.total_tables, 3)
        self.assertEqual(closing.tip_allocation_difference, 5)
        summary = closing.get_whatsapp_summary()
        self.assertIn('Tracked Bills: 5', summary)
        self.assertIn('Tracked Guests: 6', summary)
        self.assertIn('Reviews: 3', summary)

    def test_31_service_summary_resyncs_on_update_and_delete(self):
        closing = self._closing(legacy=False)
        entry = self._service_entry(
            closing,
            bill_reference='MOVE-1',
            cash_tip_amount=4,
            card_tip_amount=6,
        )
        self.assertEqual(closing.waiter_line_ids.employee_id, self.employee)
        entry.write({
            'employee_id': self.other_employee.id,
            'bill_amount': 140,
            'guest_count': 3,
            'cash_tip_amount': 5,
            'card_tip_amount': 7,
            'review_count': 2,
        })
        self.assertEqual(len(closing.waiter_line_ids), 1)
        line = closing.waiter_line_ids
        self.assertEqual(line.employee_id, self.other_employee)
        self.assertEqual(line.sales_amount, 140)
        self.assertEqual(line.guest_count, 3)
        self.assertEqual(line.tips_amount, 12)
        self.assertEqual(line.review_count, 2)
        entry.unlink()
        self.assertFalse(closing.waiter_line_ids)
        self.assertEqual(closing.tracked_service_sales, 0)
        self.assertEqual(closing.reported_total_tips, 0)

    @mute_logger('odoo.sql_db')
    def test_32_service_entry_validation_and_single_tip_source(self):
        closing = self._closing(legacy=False)
        invalid_values = (
            {'employee_id': False},
            {'table_reference': False},
            {
                'service_type': 'pickup',
                'employee_id': self.employee.id,
                'table_reference': False,
                'guest_count': 0,
            },
            {
                'service_type': 'delivery',
                'employee_id': False,
                'table_reference': False,
                'guest_count': 1,
            },
        )
        for index, values in enumerate(invalid_values, start=1):
            with self.subTest(values=values), self.assertRaises(ValidationError), self.env.cr.savepoint():
                self._service_entry(
                    closing,
                    bill_reference=f'INVALID-{index}',
                    **values,
                )

        self._service_entry(closing, bill_reference='DUPLICATE')
        with self.assertRaises(IntegrityError), self.env.cr.savepoint():
            self._service_entry(closing, bill_reference='DUPLICATE')
        with self.assertRaises(IntegrityError), self.env.cr.savepoint():
            self._service_entry(closing, bill_reference='NEGATIVE', bill_amount=-1)
        with self.assertRaises(ValidationError):
            self._line(closing)
        with self.assertRaises(ValidationError):
            self._tip(closing)

    def test_33_service_entries_lock_after_submission(self):
        closing = self._closing(legacy=False)
        entry = self._service_entry(closing, bill_reference='LOCK-1')
        closing.action_submit()
        for operation in (
            lambda: entry.write({'bill_amount': 90}),
            lambda: entry.unlink(),
            lambda: self._service_entry(closing, bill_reference='LOCK-2'),
        ):
            with self.assertRaises(AccessError), self.env.cr.savepoint():
                operation()

    def test_34_service_entries_follow_branch_security(self):
        local_closing = self._closing(legacy=False)
        local_entry = self._service_entry(local_closing, bill_reference='LOCAL-1')
        other_closing = self._closing(
            user=self.env.user,
            legacy=False,
            branch_id=self.other_branch.id,
        )
        foreign_entry = self.env['restaurant.waiter.service.entry'].sudo().create({
            'closing_id': other_closing.id,
            'service_type': 'dine_in',
            'employee_id': self.employee.id,
            'bill_reference': 'FOREIGN-1',
            'table_reference': 'F-1',
            'guest_count': 1,
            'bill_amount': 10,
        })
        reception_model = self._model('restaurant.waiter.service.entry')
        self.assertEqual(
            reception_model.search([('id', 'in', (local_entry | foreign_entry).ids)]).ids,
            local_entry.ids,
        )
        with self.assertRaises(AccessError):
            foreign_entry.with_user(self.reception).read(['bill_reference'])
        self.assertEqual(
            local_entry.with_user(self.manager).read(['bill_reference'])[0]['bill_reference'],
            'LOCAL-1',
        )
        for user in (self.operations, self.owner):
            visible = self._model('restaurant.waiter.service.entry', user).search([
                ('id', 'in', (local_entry | foreign_entry).ids),
            ])
            self.assertEqual(set(visible.ids), set((local_entry | foreign_entry).ids))

    def test_35_service_tracking_never_touches_attendance(self):
        attendance_before = self.env['restaurant.attendance.entry'].search_count([])
        hr_attendance_before = self.env['hr.attendance'].search_count([])
        closing = self._closing(legacy=False)
        self._service_entry(closing, bill_reference='NO-ATTENDANCE-1')
        self._service_entry(
            closing,
            service_type='pickup',
            employee_id=False,
            bill_reference='NO-ATTENDANCE-2',
            table_reference=False,
            guest_count=0,
        )
        self.assertEqual(
            self.env['restaurant.attendance.entry'].search_count([]),
            attendance_before,
        )
        self.assertEqual(
            self.env['hr.attendance'].search_count([]),
            hr_attendance_before,
        )
        for field_name in (
            'work_status', 'availability_status', 'attendance_status',
            'attendance_id',
        ):
            self.assertNotIn(
                field_name,
                self.env['restaurant.waiter.service.entry']._fields,
            )
            self.assertNotIn(
                field_name,
                self.env['restaurant.waiter.daily.line']._fields,
            )

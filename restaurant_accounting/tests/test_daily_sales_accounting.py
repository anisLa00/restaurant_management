from datetime import date

from odoo import Command
from odoo.exceptions import AccessError, ValidationError
from odoo.tests.common import new_test_user

from odoo.addons.account.tests.common import AccountTestInvoicingCommon


class TestDailySalesAccounting(AccountTestInvoicingCommon):
    _test_user_groups = ()

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.company = cls.env.company
        cls.branch = cls.env["restaurant.branch"].create({
            "name": "Daily Accounting Test Branch",
            "code": "DAT",
            "company_id": cls.company.id,
        })
        cls.reception = cls._make_user(
            "daily_accounting_reception",
            "restaurant_core.group_restaurant_reception",
            cls.branch,
        )
        cls.manager = cls._make_user(
            "daily_accounting_manager",
            "restaurant_core.group_restaurant_branch_manager",
            cls.branch,
        )
        cls.accountant = cls._make_user(
            "daily_accounting_accountant",
            "restaurant_core.group_restaurant_accountant",
        )
        cls.owner = cls._make_user(
            "daily_accounting_owner",
            "restaurant_core.group_restaurant_owner",
            cls.branch,
        )
        cls.waiter_job = cls.env["hr.job"].sudo().create({
            "name": "Waiter",
            "company_id": cls.company.id,
        })
        cls.waiter = cls.env["hr.employee"].sudo().create({
            "name": "Daily Accounting Waiter",
            "company_id": cls.company.id,
            "job_id": cls.waiter_job.id,
            "restaurant_branch_id": cls.branch.id,
        })
        cls.cash_account = cls._account("DA.CASH", "Daily Cash", "asset_cash")
        cls.card_account = cls._account("DA.CARD", "Card Clearing", "asset_current")
        cls.delivery_account = cls._account("DA.DEL", "Delivery Clearing", "asset_current")
        cls.other_account = cls._account("DA.OTH", "Other Clearing", "asset_current")
        cls.vat_account = cls._account("DA.VAT", "Output VAT", "liability_current")
        cls.tips_account = cls._account("DA.TIPS", "Tips Payable", "liability_current")
        cls.company.write({
            "restaurant_sales_journal_id": cls.company_data["default_journal_misc"].id,
            "restaurant_sales_income_account_id": cls.company_data["default_account_revenue"].id,
            "restaurant_vat_output_account_id": cls.vat_account.id,
            "restaurant_cash_account_id": cls.cash_account.id,
            "restaurant_card_clearing_account_id": cls.card_account.id,
            "restaurant_delivery_clearing_account_id": cls.delivery_account.id,
            "restaurant_other_clearing_account_id": cls.other_account.id,
            "restaurant_tips_payable_account_id": cls.tips_account.id,
        })

    @classmethod
    def _account(cls, code, name, account_type):
        return cls.env["account.account"].create({
            "code": code,
            "name": name,
            "account_type": account_type,
        })

    @classmethod
    def _make_user(cls, login, group, branch=None):
        return new_test_user(
            cls.env,
            login=login,
            groups=group,
            company_id=cls.company.id,
            company_ids=[Command.set(cls.company.ids)],
            restaurant_branch_ids=[Command.set(branch.ids if branch else [])],
        )

    def _reviewed_closing(self, sales=105, payments=105, tips=10, closing_date=None):
        closing = self.env["restaurant.daily.closing"].with_user(self.reception).create({
            "branch_id": self.branch.id,
            "closing_date": closing_date or date(2026, 10, 9),
            "shift": "full_day",
            "reported_total_sales": sales,
            "reported_vat_amount": sales * 5 / 105,
            "guest_count": 10,
        })
        closing.sudo().with_context(service_tracking_migration=True).write({
            "service_tracking_mode": "legacy",
        })
        self.env["restaurant.reception.payment.line"].with_user(self.reception).create({
            "closing_id": closing.id,
            "payment_type": "cash",
            "amount": payments,
        })
        if tips:
            self.env["restaurant.reception.tip.line"].with_user(self.reception).create({
                "closing_id": closing.id,
                "tip_source": "cash",
                "amount": tips,
            })
        closing.with_user(self.reception).action_submit()
        closing.with_user(self.manager).action_confirm()
        return closing.sudo()

    def test_reviewed_closing_creates_balanced_posted_entry(self):
        closing = self._reviewed_closing()
        daily = closing.accounting_daily_sales_id

        self.assertEqual(len(daily), 1)
        self.assertEqual(daily.state, "pending")
        self.assertEqual(daily.total_sales, 105)
        self.assertEqual(daily.net_sales, 100)
        self.assertEqual(daily.vat_amount, 5)
        self.assertEqual(daily.cash_tips, 10)

        daily.with_user(self.accountant).action_review()
        action = daily.with_user(self.accountant).action_create_journal_entry()
        move = daily.journal_entry_id.sudo()
        self.assertEqual(action["res_id"], move.id)
        self.assertEqual(daily.state, "draft_entry")
        self.assertEqual(sum(move.line_ids.mapped("debit")), 115)
        self.assertEqual(sum(move.line_ids.mapped("credit")), 115)

        daily.with_user(self.accountant).action_post_journal_entry()
        self.assertEqual(daily.state, "posted")
        self.assertEqual(move.state, "posted")
        self.assertEqual(daily.posted_by_id, self.accountant)

    def test_native_journal_post_cancel_and_reset_automatically_project_state(self):
        daily = self._reviewed_closing(
            closing_date=date(2026, 10, 11)
        ).accounting_daily_sales_id
        daily.with_user(self.accountant).action_review()
        daily.with_user(self.accountant).action_create_journal_entry()
        move = daily.journal_entry_id.sudo()

        move.action_post()
        daily.invalidate_recordset()
        self.assertEqual(daily.state, "posted")
        self.assertFalse(daily.posted_by_id)
        self.assertFalse(daily.posted_at)

        move.button_cancel()
        daily.invalidate_recordset()
        self.assertEqual(daily.state, "cancelled")

        move.button_draft()
        daily.invalidate_recordset()
        self.assertEqual(daily.state, "draft_entry")
        self.assertEqual(daily.workflow_state, "draft_entry")

    def test_accountant_return_correction_reuses_and_refreshes_snapshot(self):
        closing = self._reviewed_closing(
            sales=105,
            payments=105,
            tips=0,
            closing_date=date(2026, 10, 12),
        )
        daily = closing.accounting_daily_sales_id
        daily.with_user(self.accountant).write({
            "review_note": "Correct the POS VAT source value.",
        })
        daily.with_user(self.accountant).action_return_for_correction()

        self.assertEqual(daily.state, "correction")
        self.assertEqual(closing.state, "draft")
        self.assertEqual(closing.correction_reason, "Correct the POS VAT source value.")
        self.assertEqual(daily.returned_by_id, self.accountant)
        self.assertEqual(daily.correction_count, 1)

        closing.with_user(self.reception).write({"reported_vat_amount": 4.75})
        closing.with_user(self.reception).action_submit()
        closing.with_user(self.manager).action_confirm()
        daily.invalidate_recordset()

        self.assertEqual(closing.state, "confirmed")
        self.assertEqual(daily.state, "pending")
        self.assertEqual(daily.workflow_state, "pending")
        self.assertEqual(daily.pos_reported_vat, 4.75)
        self.assertEqual(len(closing.accounting_daily_sales_id), 1)

    def test_return_for_correction_is_blocked_after_entry_creation(self):
        daily = self._reviewed_closing(
            closing_date=date(2026, 10, 13)
        ).accounting_daily_sales_id
        daily.with_user(self.accountant).action_review()
        daily.with_user(self.accountant).action_create_journal_entry()
        daily.with_user(self.accountant).write({
            "review_note": "Attempted late source correction.",
        })

        with self.assertRaises(ValidationError):
            daily.with_user(self.accountant).action_return_for_correction()

        with self.assertRaises(AccessError):
            daily.with_user(self.accountant).with_context(
                restaurant_daily_sales_accounting_workflow=True,
            ).write({"workflow_state": "posted"})

    def test_waiter_sheet_uses_same_record_through_accounting_correction(self):
        business_date = date(2026, 10, 14)
        waiter_closing = self.env[
            "restaurant.waiter.sales.closing"
        ].with_user(self.reception).create({
            "branch_id": self.branch.id,
            "business_date": business_date,
        })
        bill = self.env["restaurant.waiter.sales.bill"].with_user(
            self.reception
        ).create({
            "waiter_closing_id": waiter_closing.id,
            "service_type": "dine_in",
            "employee_id": self.waiter.id,
            "bill_reference": "CORR-BILL-1",
            "table_reference": "7",
            "guest_count": 2,
            "bill_amount": 105,
            "card_tip_amount": 5,
        })
        closing = self.env["restaurant.daily.closing"].with_user(
            self.reception
        ).create({
            "branch_id": self.branch.id,
            "closing_date": business_date,
            "shift": "full_day",
            "reported_total_sales": 105,
            "reported_vat_amount": 5,
            "guest_count": 2,
        })
        self.env["restaurant.reception.payment.line"].with_user(
            self.reception
        ).create({
            "closing_id": closing.id,
            "payment_type": "cash",
            "amount": 105,
        })
        waiter_closing.with_user(self.reception).action_submit()
        waiter_closing.with_user(self.manager).action_close()
        closing.with_user(self.reception).action_submit()
        closing.with_user(self.manager).action_confirm()
        accounting = closing.sudo().accounting_daily_sales_id
        accounting_id = accounting.id
        waiter_id = waiter_closing.id

        accounting.with_user(self.accountant).write({
            "review_note": "Correct the waiter card tip.",
        })
        accounting.with_user(self.accountant).action_return_for_correction()
        self.assertEqual(closing.state, "draft")
        self.assertEqual(waiter_closing.state, "correction")
        self.assertEqual(
            waiter_closing.last_correction_reason,
            "Correct the waiter card tip.",
        )

        bill.with_user(self.reception).write({"card_tip_amount": 8})
        waiter_closing.with_user(self.reception).action_submit()
        waiter_closing.with_user(self.manager).action_close()
        closing.with_user(self.reception).action_submit()
        closing.with_user(self.manager).action_confirm()
        accounting.invalidate_recordset()

        self.assertEqual(waiter_closing.id, waiter_id)
        self.assertEqual(
            closing.sudo().accounting_daily_sales_id.id,
            accounting_id,
        )
        self.assertEqual(accounting.state, "pending")
        self.assertEqual(accounting.correction_count, 1)
        self.assertEqual(
            accounting.last_correction_reason,
            "Correct the waiter card tip.",
        )
        self.assertEqual(accounting.last_correction_applied_by_id, self.manager)
        self.assertFalse(accounting.reviewed_by_id)

    def test_payment_difference_blocks_accounting_review(self):
        daily = self._reviewed_closing(
            sales=105,
            payments=100,
            tips=0,
            closing_date=date(2026, 10, 10),
        ).accounting_daily_sales_id

        with self.assertRaises(ValidationError):
            daily.with_user(self.accountant).action_review()

    def test_month_close_requires_posted_sales_and_processed_payroll(self):
        daily = self._reviewed_closing(tips=0).accounting_daily_sales_id
        daily.with_user(self.accountant).action_review()
        daily.with_user(self.accountant).action_create_journal_entry()
        daily.with_user(self.accountant).action_post_journal_entry()

        employee = self.env["hr.employee"].sudo().create({
            "name": "Monthly Close Employee",
            "company_id": self.company.id,
            "restaurant_immigration_status": False,
            "restaurant_work_authorized": True,
            "wage": 3000,
        })
        attendance_month = self.env["restaurant.attendance.month"].sudo().create({
            "branch_id": self.branch.id,
            "month_start": date(2026, 10, 1),
        })
        self.env["restaurant.attendance.month.line"].sudo().with_context(
            attendance_month_internal=True,
        ).create({
            "month_id": attendance_month.id,
            "employee_id": employee.id,
            "present_days": 22,
        })
        attendance_month.sudo()._internal_write({"state": "closed"})
        payroll = self.env["restaurant.payroll.input.batch"].sudo().create({
            "attendance_month_id": attendance_month.id,
        })
        payroll.sudo().action_ready()
        self.assertFalse(
            self.env["restaurant.payroll.input.batch"].with_user(
                self.owner
            ).search([("id", "=", payroll.id)])
        )
        payroll.with_user(self.accountant).action_process()

        self.owner.sudo().write({"restaurant_branch_ids": [Command.clear()]})
        self.assertEqual(
            self.env["restaurant.payroll.input.batch"].with_user(
                self.owner
            ).search([("id", "=", payroll.id)]),
            payroll,
        )
        self.assertEqual(
            self.env["restaurant.payroll.input.line"].with_user(
                self.owner
            ).search([("batch_id", "=", payroll.id)]),
            payroll.line_ids,
        )
        self.owner.sudo().write({
            "restaurant_branch_ids": [Command.set(self.branch.ids)],
        })

        owner_payroll = self.env["restaurant.payroll.input.batch"].with_user(
            self.owner
        ).search([("id", "=", payroll.id)])
        self.assertEqual(owner_payroll, payroll)
        self.assertEqual(owner_payroll.line_ids, payroll.line_ids)
        self.assertEqual(owner_payroll.line_ids.input_total, 3000)
        with self.assertRaises(AccessError):
            owner_payroll.write({"payroll_note": "Owner must remain read-only."})
        with self.assertRaises(AccessError):
            owner_payroll.line_ids.write({"adjustment_note": "No owner edits."})
        with self.assertRaises(AccessError):
            owner_payroll.action_process()

        period = self.env["restaurant.accounting.period.close"].with_user(
            self.accountant
        ).create({
            "company_id": self.company.id,
            "month_start": date(2026, 10, 1),
            "bank_reconciled": True,
            "cash_reconciled": True,
            "inventory_reviewed": True,
            "tax_reviewed": True,
        })
        self.assertTrue(period.daily_sales_complete)
        self.assertTrue(period.supplier_invoices_complete)
        self.assertTrue(period.payroll_complete)

        period.with_user(self.accountant).action_submit_owner()
        self.assertEqual(period.state, "awaiting_owner")

        owner_period = self.env["restaurant.accounting.period.close"].with_user(
            self.owner
        ).search_read(
            [("id", "=", period.id)],
            [
                "name",
                "payroll_complete",
                "payroll_batch_count",
                "payroll_batch_pending_count",
            ],
        )
        self.assertEqual(owner_period, [{
            "id": period.id,
            "name": period.name,
            "payroll_complete": True,
            "payroll_batch_count": 1,
            "payroll_batch_pending_count": 0,
        }])

        payroll_action = period.with_user(self.owner).action_open_payroll_inputs()
        self.assertEqual(payroll_action["res_model"], "restaurant.payroll.input.batch")
        self.assertIn(("month_start", "=", date(2026, 10, 1)), payroll_action["domain"])
        self.assertIn(("state", "=", "processed"), payroll_action["domain"])

        period.with_user(self.owner).action_approve_close()
        self.assertEqual(period.state, "closed")
        self.assertEqual(period.approved_by_id, self.owner)

    def test_management_control_is_shared_by_operations_and_owner_only(self):
        self._reviewed_closing(sales=210, payments=210, tips=15)
        Dashboard = self.env["restaurant.management.dashboard"]

        action = Dashboard.with_user(self.owner).action_open_dashboard()
        dashboard = Dashboard.with_user(self.owner).browse(action["res_id"])
        dashboard.write({
            "date_from": date(2026, 10, 1),
            "date_to": date(2026, 10, 31),
        })
        self.assertEqual(dashboard.total_sales, 210)
        self.assertEqual(dashboard.total_tips, 15)
        self.assertEqual(dashboard.daily_sales_pending_count, 1)
        self.assertIn(
            f"{self.branch.name}: 1",
            dashboard.daily_sales_pending_by_branch,
        )

        second_action = Dashboard.with_user(self.owner).action_open_dashboard()
        self.assertEqual(second_action["res_id"], dashboard.id)
        with self.assertRaises(AccessError):
            Dashboard.with_user(self.reception).action_open_dashboard()

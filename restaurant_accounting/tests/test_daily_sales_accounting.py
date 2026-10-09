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
        )
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
        payroll.with_user(self.accountant).action_process()

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

        second_action = Dashboard.with_user(self.owner).action_open_dashboard()
        self.assertEqual(second_action["res_id"], dashboard.id)
        with self.assertRaises(AccessError):
            Dashboard.with_user(self.reception).action_open_dashboard()

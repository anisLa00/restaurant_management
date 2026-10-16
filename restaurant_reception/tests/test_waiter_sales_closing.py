from datetime import date
from urllib.parse import urlparse

from lxml import etree
from psycopg2 import IntegrityError

from odoo import Command
from odoo.exceptions import AccessError, ValidationError
from odoo.tests.common import new_test_user
from odoo.tools import mute_logger

from odoo.addons.base.tests.common import BaseCommon


class TestWaiterSalesClosing(BaseCommon):
    _test_user_groups = ()

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.branch, cls.other_branch = cls.env["restaurant.branch"].create([
            {
                "name": "Waiter Closing Test Branch",
                "code": "WST-A",
                "company_id": cls.company.id,
            },
            {
                "name": "Waiter Closing Other Branch",
                "code": "WST-B",
                "company_id": cls.company.id,
            },
        ])
        cls.waiter_job = cls.env["hr.job"].create({
            "name": "Waiter",
            "company_id": cls.company.id,
        })
        cls.chef_job = cls.env["hr.job"].create({
            "name": "Chef",
            "company_id": cls.company.id,
        })
        cls.waiter_one, cls.waiter_two = cls.env["hr.employee"].create([
            {
                "name": "Waiter Closing One", "company_id": cls.company.id,
                "job_id": cls.waiter_job.id,
                "restaurant_branch_id": cls.branch.id,
            },
            {
                "name": "Waiter Closing Two", "company_id": cls.company.id,
                "job_id": cls.waiter_job.id,
                "restaurant_branch_id": cls.branch.id,
            },
        ])
        cls.waiter_zero = cls.env["hr.employee"].create({
            "name": "Waiter Closing Zero", "company_id": cls.company.id,
            "job_id": cls.waiter_job.id,
            "restaurant_branch_id": cls.branch.id,
        })
        cls.chef = cls.env["hr.employee"].create({
            "name": "Waiter Closing Chef", "company_id": cls.company.id,
            "job_id": cls.chef_job.id,
            "restaurant_branch_id": cls.branch.id,
        })
        cls.reception = cls._make_user(
            "waiter_closing_reception",
            "restaurant_core.group_restaurant_reception",
            cls.branch,
        )
        cls.other_reception = cls._make_user(
            "waiter_closing_other_reception",
            "restaurant_core.group_restaurant_reception",
            cls.other_branch,
        )
        cls.manager = cls._make_user(
            "waiter_closing_manager",
            "restaurant_core.group_restaurant_branch_manager",
            cls.branch,
        )
        cls.business_date = date(2098, 10, 4)

        if cls.env.registry.get("restaurant.stock.section"):
            cls.env["restaurant.stock.section"].sudo().with_context(
                restaurant_system_section_sync=True,
            ).search([
                ("branch_id", "in", (cls.branch | cls.other_branch).ids),
            ]).write({"required_for_daily_close": False})

    @classmethod
    def _make_user(cls, login, groups, branch):
        return new_test_user(
            cls.env,
            login=login,
            groups=groups,
            company_id=cls.company.id,
            company_ids=[Command.set(cls.company.ids)],
            restaurant_branch_ids=[Command.set(branch.ids)],
        )

    def _model(self, model_name, user=None):
        return self.env[model_name].with_user(user or self.reception).with_context(
            allowed_company_ids=[self.company.id],
            tracking_disable=True,
            mail_notrack=True,
        )

    def _closing(self, user=None, **values):
        return self._model("restaurant.waiter.sales.closing", user).create({
            "branch_id": self.branch.id,
            "business_date": self.business_date,
            **values,
        })

    def _bill(self, closing, **values):
        sequence = len(closing.bill_ids) + 1
        return self._model("restaurant.waiter.sales.bill").create({
            "waiter_closing_id": closing.id,
            "service_type": "dine_in",
            "employee_id": self.waiter_one.id,
            "bill_reference": f"WST-BILL-{sequence}",
            "table_reference": "9",
            "guest_count": 1,
            "bill_amount": 100,
            "cash_tip_amount": 0,
            "card_tip_amount": 5,
            "review_count": 0,
            **values,
        })

    def _order(self, waiter_line, **values):
        sequence = len(waiter_line.order_ids) + 1
        return self._model("restaurant.waiter.sales.order").create({
            "waiter_line_id": waiter_line.id,
            "sequence": sequence * 10,
            "pos_reference": f"POS-{waiter_line.id}-{sequence}",
            "table_reference": str(sequence),
            "guest_count": 1,
            "bill_amount": 100,
            "cash_tip_amount": 0,
            "card_tip_amount": 5,
            "review_count": 0,
            **values,
        })

    def test_01_bill_rows_generate_waiter_and_pickup_delivery_summary(self):
        attendance_before = self.env["restaurant.attendance.entry"].search_count([])
        hr_attendance_before = self.env["hr.attendance"].search_count([])
        closing = self._closing()
        self._bill(closing, bill_reference="DIN-1", guest_count=1, bill_amount=90)
        self._bill(
            closing,
            bill_reference="DIN-2",
            table_reference="9",
            guest_count=5,
            bill_amount=110,
            cash_tip_amount=4,
            card_tip_amount=6,
            review_count=1,
        )
        self._bill(
            closing,
            employee_id=self.waiter_two.id,
            bill_reference="DIN-3",
            table_reference="14",
            guest_count=2,
            bill_amount=80,
            card_tip_amount=3,
        )
        self._bill(
            closing,
            service_type="pickup",
            employee_id=False,
            bill_reference="PICK-1",
            table_reference=False,
            guest_count=0,
            bill_amount=40,
            cash_tip_amount=0,
            card_tip_amount=0,
        )
        self._bill(
            closing,
            service_type="delivery",
            employee_id=False,
            bill_reference="DEL-1",
            table_reference=False,
            guest_count=0,
            bill_amount=30,
            cash_tip_amount=0,
            card_tip_amount=2,
        )

        self.assertEqual(closing.bill_count, 5)
        self.assertEqual(closing.dine_in_bill_count, 3)
        self.assertEqual(closing.total_sales, 350)
        self.assertEqual(closing.dine_in_sales, 280)
        self.assertEqual(closing.pickup_delivery_sales, 70)
        self.assertEqual(closing.guest_count, 8)
        self.assertEqual(closing.total_tips, 20)
        self.assertEqual(closing.review_count, 1)

        waiter_summary = closing.summary_line_ids.filtered(
            lambda line: line.employee_id == self.waiter_one
        )
        self.assertEqual(waiter_summary.table_references, "9, 9")
        self.assertEqual(waiter_summary.guest_counts, "1, 5")
        self.assertEqual(waiter_summary.bill_count, 2)
        self.assertEqual(waiter_summary.sales_amount, 200)
        self.assertEqual(waiter_summary.tips_amount, 15)
        self.assertEqual(waiter_summary.tip_percentage, 7.5)
        takeaway = closing.summary_line_ids.filtered(
            lambda line: line.summary_type == "pickup_delivery"
        )
        self.assertEqual(takeaway.sales_amount, 70)
        self.assertEqual(takeaway.tips_amount, 2)
        self.assertFalse(closing.summary_line_ids.filtered(
            lambda line: line.employee_id == self.waiter_zero
        ))

        self.assertEqual(
            self.env["restaurant.attendance.entry"].search_count([]),
            attendance_before,
        )
        self.assertEqual(
            self.env["hr.attendance"].search_count([]),
            hr_attendance_before,
        )
        for field_name in (
            "work_status", "availability_status", "attendance_status",
            "attendance_id",
        ):
            self.assertNotIn(
                field_name, self.env["restaurant.waiter.sales.closing"]._fields,
            )
            self.assertNotIn(
                field_name, self.env["restaurant.waiter.sales.bill"]._fields,
            )
            self.assertNotIn(
                field_name, self.env["restaurant.waiter.sales.summary"]._fields,
            )

    def test_01b_waiters_are_ready_and_service_totals_use_add_line(self):
        closing = self._closing()
        waiter_rows = closing.bill_ids.filtered("is_auto_waiter")
        self.assertEqual(
            set(waiter_rows.mapped("employee_id").ids),
            set((self.waiter_one | self.waiter_two | self.waiter_zero).ids),
        )
        self.assertTrue(all(waiter_rows.mapped("is_daily_total")))
        self.assertEqual(set(waiter_rows.mapped("entry_mode")), {"order_details"})
        self.assertFalse(closing.bill_ids.filtered(
            lambda line: line.service_type in ("pickup", "delivery")
        ))

        first_waiter = waiter_rows.filtered(
            lambda line: line.employee_id == self.waiter_one
        )
        self._order(
            first_waiter,
            pos_reference="ANIS-1", table_reference="9", guest_count=1,
            bill_amount=90, cash_tip_amount=10, card_tip_amount=0,
            review_count=1,
        )
        self._order(
            first_waiter,
            pos_reference="ANIS-2", table_reference="14", guest_count=5,
            bill_amount=65, cash_tip_amount=0, card_tip_amount=6.5,
        )
        self._order(
            first_waiter,
            pos_reference="ANIS-3", table_reference="4", guest_count=2,
            bill_amount=719, cash_tip_amount=35.95, card_tip_amount=0,
        )
        self._order(
            first_waiter,
            pos_reference="ANIS-4", table_reference="11", guest_count=4,
            bill_amount=90, cash_tip_amount=0, card_tip_amount=0,
        )
        self.assertEqual(first_waiter.bill_count, 4)
        self.assertEqual(first_waiter.table_count, 4)
        self.assertEqual(first_waiter.guest_count, 12)
        self.assertEqual(first_waiter.table_references, "9, 14, 4, 11")
        self.assertEqual(first_waiter.guest_counts, "1, 5, 2, 4")
        self.assertEqual(
            first_waiter.order_breakdown,
            "90 / 10 | 65 / 6.5 | 719 / 35.95 | 90 / 0",
        )
        self.assertEqual(first_waiter.bill_amount, 964)
        self.assertEqual(
            self.env["restaurant.waiter.sales.order"]._fields["bill_amount"].string,
            "Bill Total (VAT Included)",
        )
        self.assertAlmostEqual(first_waiter.tip_total, 52.45, places=2)
        self.assertEqual(first_waiter.review_count, 1)
        pickup = self._model("restaurant.waiter.sales.bill").with_context(
            default_is_daily_total=True,
        ).create({
            "waiter_closing_id": closing.id,
            "is_daily_total": True,
            "service_type": "pickup",
            "bill_count": 3,
            "bill_amount": 90,
            "cash_tip_amount": 5,
        })
        self.assertTrue(pickup.bill_reference.startswith("DAILY-PICKUP-"))
        self.assertFalse(pickup.employee_id)
        self.assertEqual(closing.bill_count, 7)
        self.assertEqual(closing.total_sales, 1054)
        self.assertAlmostEqual(closing.total_tips, 57.45, places=2)
        waiter_summary = closing.summary_line_ids.filtered(
            lambda line: line.employee_id == self.waiter_one
        )
        self.assertEqual(waiter_summary.bill_count, 4)
        self.assertEqual(waiter_summary.table_count, 4)
        self.assertEqual(waiter_summary.table_references, "9, 14, 4, 11")
        self.assertEqual(waiter_summary.guest_counts, "1, 5, 2, 4")
        self.assertEqual(waiter_summary.order_breakdown, first_waiter.order_breakdown)
        self.assertFalse(closing.summary_line_ids.filtered(
            lambda line: line.employee_id == self.waiter_zero
        ))
        with self.assertRaises(AccessError):
            first_waiter.unlink()

    def test_01c_absent_waiter_is_removed_and_present_correction_restores_row(self):
        closing = self._closing()
        self.assertTrue(closing.bill_ids.filtered(
            lambda line: line.employee_id == self.waiter_two
        ))

    def test_01d_waiter_picker_and_backend_reject_non_waiter_staff(self):
        closing = self._closing()
        with self.assertRaisesRegex(ValidationError, "not assigned to the Waiter job"):
            self._bill(closing, employee_id=self.chef.id)

        view = etree.fromstring(self.env.ref(
            "restaurant_reception.restaurant_waiter_sales_closing_view_form"
        ).arch_db)
        waiter_fields = view.xpath("//field[@name='bill_ids']//field[@name='employee_id']")
        self.assertEqual(len(waiter_fields), 2)
        for field in waiter_fields:
            domain = field.get("domain") or ""
            self.assertIn("('active', '=', True)", domain)
            self.assertIn("('job_id.name', 'ilike', 'waiter')", domain)
        self.assertTrue(view.xpath("//button[@name='action_open_orders']"))
        self.assertTrue(view.xpath("//field[@name='order_ids']"))
        orders_view = etree.fromstring(self.env.ref(
            "restaurant_reception.restaurant_waiter_sales_bill_orders_view_form"
        ).arch_db)
        save_buttons = orders_view.xpath("//footer/button[@special='save']")
        discard_buttons = orders_view.xpath("//footer/button[@special='cancel']")
        self.assertEqual(len(save_buttons), 1)
        self.assertEqual(save_buttons[0].get("string"), "Save & Close")
        self.assertEqual(len(discard_buttons), 1)
        self.assertEqual(discard_buttons[0].get("string"), "Discard")
        attendance = self._model("restaurant.attendance.entry").create({
            "employee_id": self.waiter_two.id,
            "branch_id": self.branch.id,
            "attendance_date": self.business_date,
            "status": "absent",
        })
        self.assertFalse(closing.bill_ids.filtered(
            lambda line: line.employee_id == self.waiter_two
        ))
        self.assertFalse(closing.summary_line_ids.filtered(
            lambda line: line.employee_id == self.waiter_two
        ))
        attendance.write({"status": "present"})
        self.assertTrue(closing.bill_ids.filtered(
            lambda line: line.employee_id == self.waiter_two
        ))

    def test_01e_summary_ranks_waiters_by_vat_inclusive_sales(self):
        closing = self._closing()
        waiter_rows = closing.bill_ids.filtered("is_auto_waiter")
        sales_by_waiter = {
            self.waiter_one: 300,
            self.waiter_two: 300,
            self.waiter_zero: 100,
        }
        for waiter, sales in sales_by_waiter.items():
            self._order(
                waiter_rows.filtered(lambda line: line.employee_id == waiter),
                bill_amount=sales,
            )
        self._model("restaurant.waiter.sales.bill").with_context(
            default_is_daily_total=True,
        ).create({
            "waiter_closing_id": closing.id,
            "is_daily_total": True,
            "service_type": "pickup",
            "bill_count": 1,
            "bill_amount": 1_000,
        })

        summaries = closing.summary_line_ids.sorted("sort_sequence")
        waiter_summaries = summaries.filtered(
            lambda line: line.summary_type == "waiter"
        )
        self.assertEqual(
            waiter_summaries.mapped("employee_id").ids,
            (self.waiter_one | self.waiter_two | self.waiter_zero).ids,
        )
        self.assertEqual(waiter_summaries.mapped("sales_rank"), [1, 1, 3])
        self.assertEqual(waiter_summaries.mapped("sales_amount"), [300, 300, 100])
        takeaway = summaries.filtered(
            lambda line: line.summary_type == "pickup_delivery"
        )
        self.assertEqual(takeaway.sales_rank, 0)
        self.assertEqual(summaries[-1], takeaway)

        view = etree.fromstring(self.env.ref(
            "restaurant_reception.restaurant_waiter_sales_closing_view_form"
        ).arch_db)
        self.assertTrue(view.xpath(
            "//field[@name='summary_line_ids']//field[@name='sales_rank']"
        ))
        self.assertTrue(closing._render_waiter_table_pdf().startswith(b"%PDF"))

    def test_02_submit_close_and_draft_lock(self):
        closing = self._closing()
        with self.assertRaises(ValidationError):
            closing.action_submit()
        waiter_line = closing.bill_ids.filtered(
            lambda line: line.employee_id == self.waiter_one
        )
        order = self._order(waiter_line, pos_reference="LOCK-ORDER")
        bill = self._bill(closing)
        closing.action_submit()
        self.assertEqual(closing.state, "submitted")
        self.assertEqual(closing.submitted_by_id, self.reception)
        for operation in (
            lambda: bill.write({"bill_amount": 90}),
            lambda: bill.unlink(),
            lambda: self._bill(closing, bill_reference="LOCKED"),
            lambda: order.write({"bill_amount": 90}),
            lambda: order.unlink(),
            lambda: self._order(waiter_line, pos_reference="LOCK-NEW"),
        ):
            with self.assertRaises(AccessError), self.env.cr.savepoint():
                operation()
        closing.with_user(self.manager).action_close()
        self.assertEqual(closing.state, "closed")
        self.assertEqual(closing.closed_by_id, self.manager)

    @mute_logger("odoo.sql_db")
    def test_03_unique_day_and_branch_isolation(self):
        local = self._closing()
        with self.assertRaises(IntegrityError), self.env.cr.savepoint():
            self._closing()
        foreign = self._model(
            "restaurant.waiter.sales.closing", self.other_reception,
        ).create({
            "branch_id": self.other_branch.id,
            "business_date": self.business_date,
        })
        visible = self._model("restaurant.waiter.sales.closing").search([
            ("id", "in", (local | foreign).ids),
        ])
        self.assertEqual(visible.ids, local.ids)
        with self.assertRaises(AccessError):
            foreign.with_user(self.reception).read(["name"])

    def test_04_daily_closing_links_and_requires_waiter_workflow(self):
        waiter_closing = self._closing()
        self._bill(
            waiter_closing,
            bill_amount=105,
            guest_count=2,
            cash_tip_amount=5,
            card_tip_amount=5,
        )
        daily = self._model("restaurant.daily.closing").create({
            "branch_id": self.branch.id,
            "closing_date": self.business_date,
            "reported_total_sales": 105,
            "guest_count": 2,
        })
        self.assertEqual(daily.service_tracking_mode, "standalone")
        self.assertEqual(daily.waiter_sales_closing_id, waiter_closing)
        self.assertEqual(daily.tracked_service_sales, 105)
        self.assertEqual(daily.reported_total_tips, 10)
        daily.action_submit()
        self.assertEqual(daily.state, "manager_review")
        with self.assertRaises(ValidationError):
            daily.with_user(self.manager).action_confirm()

        waiter_closing.action_submit()
        waiter_closing.with_user(self.manager).action_close()
        daily.with_user(self.manager).action_confirm()
        self.assertEqual(daily.state, "confirmed")

    def test_05_manager_reopen_requires_reason(self):
        closing = self._closing()
        self._bill(closing)
        closing.action_submit()
        manager_closing = closing.with_user(self.manager)
        with self.assertRaises(ValidationError):
            manager_closing.action_return_to_draft()
        manager_closing.correction_reason = "Correct one bill."
        manager_closing.action_return_to_draft()
        self.assertEqual(closing.state, "draft")
        self.assertEqual(closing.reopen_count, 1)

    def test_06_cancelled_documents_release_the_active_day_link(self):
        waiter_closing = self._closing()
        daily = self._model("restaurant.daily.closing").create({
            "branch_id": self.branch.id,
            "closing_date": self.business_date,
        })
        self.assertEqual(daily.waiter_sales_closing_id, waiter_closing)

        waiter_closing.action_cancel()
        self.assertFalse(daily.waiter_sales_closing_id)
        replacement = self._closing()
        self.assertEqual(daily.waiter_sales_closing_id, replacement)

        daily.action_cancel()
        self.assertFalse(daily.waiter_sales_closing_id)

    def test_07_closed_sheet_correction_reuses_record_and_marks_updated_pdf(self):
        closing = self._closing()
        bill = self._bill(closing, card_tip_amount=5)
        closing.action_submit()
        manager_closing = closing.with_user(self.manager)
        manager_closing.action_close()
        first_closed_at = closing.first_closed_at

        manager_closing.correction_reason = "Correct the card tip."
        manager_closing.action_open_correction()
        self.assertEqual(closing.state, "correction")
        self.assertEqual(closing.reopen_count, 1)
        self.assertEqual(closing.last_correction_reason, "Correct the card tip.")
        self.assertEqual(closing.first_closed_at, first_closed_at)

        bill.with_user(self.reception).write({"card_tip_amount": 8})
        closing.with_user(self.reception).action_submit()
        closing.with_user(self.manager).action_close()
        self.assertEqual(closing.state, "closed")
        self.assertEqual(closing.total_tips, 8)
        self.assertEqual(closing.first_closed_at, first_closed_at)

        action = closing.with_user(self.reception).action_download_waiter_table_pdf()
        attachment_id = int(urlparse(action["url"]).path.split("/")[-1])
        attachment = self.env["ir.attachment"].sudo().browse(attachment_id)
        self.assertIn("Updated", attachment.name)
        self.assertTrue(attachment.raw.content.startswith(b"%PDF"))

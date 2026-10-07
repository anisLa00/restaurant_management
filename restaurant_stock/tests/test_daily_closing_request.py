from datetime import datetime, timedelta
from unittest.mock import patch

from odoo import Command, fields
from odoo.exceptions import AccessError, UserError, ValidationError
from odoo.tests.common import new_test_user

from odoo.addons.base.tests.common import BaseCommon
from odoo.addons.restaurant_stock.models import restaurant_stock_daily


class TestDailyClosingRequest(BaseCommon):
    _test_user_groups = ()

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.company = cls.env.company
        cls.warehouse, cls.other_warehouse = cls.env["stock.warehouse"].create([
            {
                "name": "Closing Request Branch Warehouse",
                "code": "CRB",
                "company_id": cls.company.id,
            },
            {
                "name": "Closing Request Other Warehouse",
                "code": "CRO",
                "company_id": cls.company.id,
            },
        ])
        cls.branch, cls.other_branch = cls.env["restaurant.branch"].create([
            {
                "name": "Closing Request Branch",
                "code": "CRB",
                "company_id": cls.company.id,
                "warehouse_id": cls.warehouse.id,
            },
            {
                "name": "Closing Request Other Branch",
                "code": "CRO",
                "company_id": cls.company.id,
                "warehouse_id": cls.other_warehouse.id,
            },
        ])
        cls.stockkeeper = cls._make_user(
            "closing_request_stockkeeper",
            "restaurant_core.group_restaurant_stockkeeper",
            cls.branch,
        )
        cls.manager = cls._make_user(
            "closing_request_manager",
            "restaurant_core.group_restaurant_branch_manager",
            cls.branch,
        )
        cls.other_manager = cls._make_user(
            "closing_request_other_manager",
            "restaurant_core.group_restaurant_branch_manager",
            cls.other_branch,
        )
        cls.other_stockkeeper = cls._make_user(
            "closing_request_other_stockkeeper",
            "restaurant_core.group_restaurant_stockkeeper",
            cls.other_branch,
        )
        cls.product = cls.env["product.product"].create({
            "name": "Closing Request Product",
            "is_storable": True,
        })

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

    def _opened_daily(self, stock_date=None, request_qty=20, closing_qty=8):
        values = {
            "branch_id": self.branch.id,
            "stock_date": stock_date or fields.Date.context_today(self.env.user),
            "line_ids": [Command.create({
                "product_id": self.product.id,
                "actual_closing_qty": closing_qty,
                "actual_closing_state": "counted",
                "request_tomorrow_qty": request_qty,
            })],
        }
        if stock_date:
            daily = (
                self.env["restaurant.stock.daily"]
                .with_user(self.stockkeeper)
                .sudo()
                .with_context(**{
                    restaurant_stock_daily.ALLOW_NONCURRENT_DAILY_DATE_CONTEXT:
                        True,
                })
                .create(values)
            )
            daily.action_confirm_opening()
            return daily.with_user(self.stockkeeper)

        daily = self.env["restaurant.stock.daily"].with_user(
            self.stockkeeper
        ).create(values)
        daily.action_confirm_opening()
        return daily

    def test_closing_creates_one_submitted_linked_request(self):
        daily = self._opened_daily(request_qty=20)

        daily.action_submit_closing()

        request = daily.generated_request_id
        self.assertTrue(request)
        self.assertEqual(request.daily_stock_id, daily)
        self.assertEqual(request.branch_id, self.branch)
        self.assertEqual(request.request_date, daily.stock_date + timedelta(days=1))
        self.assertEqual(request.state, "submitted")
        self.assertEqual(request.branch_status, "requested")
        self.assertEqual(request.line_ids.requested_qty, 20)

    def test_uncounted_zero_stays_pending_and_blocks_submission(self):
        self.env["stock.quant"]._update_available_quantity(
            self.product,
            self.warehouse.lot_stock_id,
            10,
        )
        daily = self.env["restaurant.stock.daily"].with_user(
            self.stockkeeper
        ).create({"branch_id": self.branch.id})
        line = daily.line_ids.filtered(
            lambda item: item.product_id == self.product
        )

        daily.action_confirm_opening()
        self.assertEqual(line.expected_closing_qty, 10)
        self.assertEqual(line.actual_closing_qty, 0)
        self.assertEqual(line.actual_closing_state, "not_counted")
        self.assertEqual(line.difference_qty, 0)
        self.assertEqual(line.missing_qty, 0)
        self.assertEqual(line.surplus_qty, 0)

        with self.assertRaisesRegex(ValidationError, "Not Counted"):
            daily.action_submit_closing()

        self.assertEqual(daily.state, "opened")
        self.assertFalse(daily.generated_request_id)
        self.assertFalse(line.missing_move_id)

    def test_explicit_actual_write_marks_counted_and_shows_variance(self):
        self.env["stock.quant"]._update_available_quantity(
            self.product,
            self.warehouse.lot_stock_id,
            20,
        )
        daily = self.env["restaurant.stock.daily"].with_user(
            self.stockkeeper
        ).create({"branch_id": self.branch.id})
        daily.action_confirm_opening()
        line = daily.line_ids.filtered(
            lambda item: item.product_id == self.product
        )

        self.assertEqual(line.actual_closing_state, "not_counted")
        line.write({"actual_closing_qty": 16})

        self.assertEqual(line.actual_closing_state, "counted")
        self.assertEqual(line.actual_closing_qty, 16)
        self.assertEqual(line.missing_qty, 4)
        self.assertEqual(line.surplus_qty, 0)

    def test_confirm_count_supports_explicit_physical_zero(self):
        self.env["stock.quant"]._update_available_quantity(
            self.product,
            self.warehouse.lot_stock_id,
            10,
        )
        daily = self.env["restaurant.stock.daily"].with_user(
            self.stockkeeper
        ).create({"branch_id": self.branch.id})
        daily.action_confirm_opening()
        line = daily.line_ids.filtered(
            lambda item: item.product_id == self.product
        )

        self.assertEqual(line.actual_closing_qty, 0)
        self.assertEqual(line.actual_closing_state, "not_counted")
        line.action_mark_counted()

        self.assertEqual(line.actual_closing_qty, 0)
        self.assertEqual(line.actual_closing_state, "counted")
        self.assertEqual(line.missing_qty, 10)
        self.assertEqual(line.surplus_qty, 0)

    def test_inline_count_status_is_editable_and_has_zero_confirmation(self):
        arch = self.env.ref(
            "restaurant_stock.restaurant_stock_daily_view_form"
        ).arch_db
        normalized_arch = " ".join(arch.split())

        self.assertIn('<field name="actual_closing_state"/>', normalized_arch)
        self.assertNotIn(
            '<field name="actual_closing_state" widget="badge"/>',
            normalized_arch,
        )
        self.assertIn('name="action_mark_counted"', normalized_arch)
        self.assertIn('string="Confirm Count"', normalized_arch)

    def test_counted_zero_posts_one_real_shortage(self):
        self.env["stock.quant"]._update_available_quantity(
            self.product,
            self.warehouse.lot_stock_id,
            10,
        )
        daily = self.env["restaurant.stock.daily"].with_user(
            self.stockkeeper
        ).create({"branch_id": self.branch.id})
        daily.action_confirm_opening()
        line = daily.line_ids.filtered(
            lambda item: item.product_id == self.product
        )
        line.write({
            "actual_closing_qty": 0,
            "actual_closing_state": "counted",
        })

        self.assertEqual(line.missing_qty, 10)
        self.assertEqual(line.surplus_qty, 0)
        daily.action_submit_closing()
        daily.with_user(self.manager).action_close()
        move = line.missing_move_id

        self.assertEqual(move.state, "done")
        self.assertEqual(move.quantity, 10)
        self.assertEqual(
            self.env["stock.quant"]._get_available_quantity(
                self.product,
                self.warehouse.lot_stock_id,
            ),
            0,
        )
        with self.assertRaises(UserError):
            daily.with_user(self.manager).action_close()
        self.assertEqual(line.missing_move_id, move)

    def test_variance_appears_only_after_explicit_count(self):
        self.env["stock.quant"]._update_available_quantity(
            self.product,
            self.warehouse.lot_stock_id,
            10,
        )
        daily = self.env["restaurant.stock.daily"].with_user(
            self.stockkeeper
        ).create({"branch_id": self.branch.id})
        daily.action_confirm_opening()
        line = daily.line_ids.filtered(
            lambda item: item.product_id == self.product
        )
        self.assertEqual(line.missing_qty, 0)
        self.assertEqual(line.surplus_qty, 0)
        self.assertEqual(line.difference_qty, 0)

        line.write({
            "actual_closing_qty": line.expected_closing_qty,
            "actual_closing_state": "counted",
        })
        self.assertEqual(line.missing_qty, 0)
        self.assertEqual(line.surplus_qty, 0)
        self.assertEqual(line.difference_qty, 0)

        line.actual_closing_qty = line.expected_closing_qty - 2
        self.assertEqual(line.missing_qty, 2)
        self.assertEqual(line.surplus_qty, 0)
        self.assertEqual(line.difference_qty, -2)

        line.actual_closing_qty = line.expected_closing_qty + 3
        self.assertEqual(line.missing_qty, 0)
        self.assertEqual(line.surplus_qty, 3)
        self.assertEqual(line.difference_qty, 3)

    def test_final_close_rechecks_count_status_without_posting(self):
        self.env["stock.quant"]._update_available_quantity(
            self.product,
            self.warehouse.lot_stock_id,
            10,
        )
        daily = self.env["restaurant.stock.daily"].with_user(
            self.stockkeeper
        ).create({"branch_id": self.branch.id})
        daily.action_confirm_opening()
        line = daily.line_ids.filtered(
            lambda item: item.product_id == self.product
        )
        line.write({
            "actual_closing_qty": 8,
            "actual_closing_state": "counted",
        })
        daily.action_submit_closing()
        line.with_user(self.manager).write({
            "actual_closing_state": "not_counted",
        })

        with self.env.cr.savepoint(), self.assertRaisesRegex(
            ValidationError,
            "Not Counted",
        ):
            daily.with_user(self.manager).action_close()

        self.assertEqual(daily.state, "closing_review")
        self.assertFalse(line.missing_move_id)
        self.assertEqual(
            self.env["stock.quant"]._get_available_quantity(
                self.product,
                self.warehouse.lot_stock_id,
            ),
            10,
        )

    def test_resubmit_updates_same_request_without_duplicate(self):
        daily = self._opened_daily(request_qty=20)
        daily.action_submit_closing()
        request = daily.generated_request_id
        daily.with_user(self.manager).action_return_to_open()
        daily.line_ids.write({"request_tomorrow_qty": 12})

        daily.action_submit_closing()

        self.assertEqual(daily.generated_request_id, request)
        self.assertEqual(request.line_ids.requested_qty, 12)
        self.assertEqual(
            self.env["restaurant.stock.request"].sudo().search_count([
                ("daily_stock_id", "=", daily.id),
            ]),
            1,
        )

    def test_manager_edits_closing_review_before_final_close(self):
        self.env["stock.quant"]._update_available_quantity(
            self.product,
            self.warehouse.lot_stock_id,
            20,
        )
        daily = self.env["restaurant.stock.daily"].with_user(
            self.stockkeeper
        ).create({"branch_id": self.branch.id})
        line = daily.line_ids.filtered(
            lambda item: item.product_id == self.product
        )
        self.assertEqual(line.opening_qty, 20)

        daily.action_confirm_opening()
        line.write({
            "consumption_qty": 5,
            "actual_closing_qty": 15,
            "actual_closing_state": "counted",
            "request_tomorrow_qty": 10,
        })
        daily.action_submit_closing()
        request = daily.generated_request_id

        self.assertEqual(daily.state, "closing_review")
        self.assertEqual(request.line_ids.requested_qty, 10)
        self.assertFalse(line.consumption_move_id)
        self.assertEqual(
            self.env["stock.quant"]._get_available_quantity(
                self.product,
                self.warehouse.lot_stock_id,
            ),
            20,
        )
        self.assertFalse(
            daily.with_user(self.stockkeeper).can_edit_stock_lines
        )
        self.assertTrue(daily.with_user(self.manager).can_edit_stock_lines)

        with self.assertRaises(AccessError):
            line.with_user(self.stockkeeper).write({"consumption_qty": 6})
        with self.assertRaises(AccessError):
            line.with_user(self.other_manager).write({"consumption_qty": 6})
        with self.assertRaises(AccessError):
            line.with_user(self.manager).write({"opening_qty": 19})
        with self.assertRaises(AccessError):
            line.with_user(self.manager).write({"received_qty": 1})

        manager_line = line.with_user(self.manager)
        with self.env.cr.savepoint(), self.assertRaisesRegex(
            ValidationError,
            r"Sold \+ Complimentary \+ legacy consumption.*"
            r"cannot exceed Current",
        ):
            manager_line.write({"consumption_qty": 21})
        with self.env.cr.savepoint(), self.assertRaisesRegex(
            ValidationError,
            r"Sold \+ Complimentary \+ legacy consumption \+ Waste \+ Damaged"
            r".*cannot exceed Current",
        ):
            manager_line.write({
                "consumption_qty": 18,
                "waste_qty": 3,
                "damaged_qty": 0,
            })

        manager_line.write({
            "consumption_qty": 7,
            "waste_qty": 2,
            "damaged_qty": 1,
            "actual_closing_qty": 10,
            "actual_closing_state": "counted",
            "request_tomorrow_qty": 8,
            "note": "Reviewed by branch manager",
        })
        daily.with_user(self.manager).write({
            "note": "Manager closing review note",
        })
        self.assertFalse(manager_line.consumption_move_id)
        self.assertEqual(
            self.env["stock.quant"]._get_available_quantity(
                self.product,
                self.warehouse.lot_stock_id,
            ),
            20,
        )

        daily.with_user(self.manager).action_close()
        manager_line.invalidate_recordset()
        request.invalidate_recordset(["line_ids"])
        self.assertEqual(daily.state, "closed")
        self.assertEqual(request.line_ids.requested_qty, 8)
        self.assertEqual(manager_line.consumption_move_id.state, "done")
        self.assertEqual(manager_line.consumption_move_id.quantity, 7)
        self.assertEqual(
            self.env["stock.quant"]._get_available_quantity(
                self.product,
                self.warehouse.lot_stock_id,
            ),
            10,
        )
        move = manager_line.consumption_move_id

        with self.assertRaises(UserError):
            daily.with_user(self.manager).action_close()
        self.assertEqual(manager_line.consumption_move_id, move)
        self.assertEqual(
            self.env["stock.quant"]._get_available_quantity(
                self.product,
                self.warehouse.lot_stock_id,
            ),
            10,
        )
        with self.assertRaises(AccessError):
            manager_line.write({"actual_closing_qty": 9})
        with self.assertRaises(AccessError):
            daily.with_user(self.manager).write({"note": "Too late"})
        self.assertFalse(daily.with_user(self.manager).can_edit_stock_lines)

        form_arch = self.env.ref(
            "restaurant_stock.restaurant_stock_daily_view_form"
        ).arch_db
        normalized_arch = " ".join(form_arch.split())
        self.assertIn(
            'name="can_edit_stock_lines" invisible="1"',
            normalized_arch,
        )
        self.assertIn(
            'name="line_ids" readonly="not can_edit_stock_lines"',
            normalized_arch,
        )
        self.assertIn(
            'name="note" readonly="not can_edit_stock_lines"',
            normalized_arch,
        )

    def test_daily_opening_generation_carries_previous_close(self):
        yesterday = fields.Date.context_today(self.env.user) - timedelta(days=1)
        previous = self._opened_daily(
            stock_date=yesterday,
            request_qty=0,
            closing_qty=8,
        )
        previous.action_submit_closing()
        previous.with_user(self.manager).action_close()

        # The approved prior close is the next service day's opening source.
        self.env["stock.quant"]._update_available_quantity(
            self.product,
            self.warehouse.lot_stock_id,
            8,
        )

        today = self.env["restaurant.stock.daily"].with_user(
            self.stockkeeper
        ).create({
            "branch_id": self.branch.id,
            "stock_date": yesterday + timedelta(days=1),
        })

        self.assertEqual(today.line_ids.product_id, self.product)
        self.assertEqual(today.line_ids.opening_qty, 8)
        self.assertEqual(today.line_ids.received_qty, 0)

    def test_overnight_close_keeps_sheet_date_and_carries_final_close(self):
        self.company.tz = "Asia/Dubai"
        service_date = fields.Date.to_date("2026-10-03")
        next_service_date = fields.Date.to_date("2026-10-04")

        self.env["stock.quant"]._update_available_quantity(
            self.product,
            self.warehouse.lot_stock_id,
            20,
        )
        service_sheet = (
            self.env["restaurant.stock.daily"]
            .with_user(self.stockkeeper)
            .sudo()
            .with_context(**{
                restaurant_stock_daily.ALLOW_NONCURRENT_DAILY_DATE_CONTEXT:
                    True,
            })
            .create({
            "branch_id": self.branch.id,
            "stock_date": service_date,
            "line_ids": [Command.create({
                "product_id": self.product.id,
                "opening_qty": 20,
            })],
            })
        )
        service_sheet.action_confirm_opening()
        service_sheet = service_sheet.with_user(self.stockkeeper)
        service_sheet.line_ids.write({
            "consumption_qty": 10,
            "actual_closing_qty": 9,
            "actual_closing_state": "counted",
        })
        service_sheet.action_submit_closing()

        # 2026-10-03 21:00 UTC is 2026-10-04 01:00 in Dubai.
        close_timestamp = datetime(2026, 10, 3, 21, 0, 0)
        with patch.object(
            fields.Datetime,
            "now",
            return_value=close_timestamp,
        ):
            service_sheet.with_user(self.manager).action_close()

        self.assertEqual(service_sheet.stock_date, service_date)
        self.assertEqual(service_sheet.reviewed_at, close_timestamp)
        self.assertEqual(service_sheet.line_ids.actual_closing_qty, 9)
        self.assertEqual(
            self.env["stock.quant"]._get_available_quantity(
                self.product,
                self.warehouse.lot_stock_id,
            ),
            9,
        )

        dashboard_fields = [
            "restaurant_stock_opening",
            "restaurant_stock_received_today",
            "restaurant_stock_consumption",
            "restaurant_stock_on_hand",
            "restaurant_stock_available",
        ]

        def dashboard_values(user):
            template = self.product.product_tmpl_id.with_user(
                user
            ).with_context(allowed_company_ids=[self.company.id])
            template.invalidate_recordset(dashboard_fields)
            return template.read(dashboard_fields)[0]

        # A completed service remains the Current Stock snapshot even after
        # local midnight and until the next service confirms Opening.
        values = dashboard_values(self.stockkeeper)
        self.assertEqual(values["restaurant_stock_opening"], 20)
        self.assertEqual(values["restaurant_stock_received_today"], 0)
        self.assertEqual(values["restaurant_stock_consumption"], 10)
        self.assertEqual(values["restaurant_stock_on_hand"], 9)
        self.assertEqual(values["restaurant_stock_available"], 9)

        with patch.object(
            fields.Datetime,
            "now",
            return_value=datetime(2026, 10, 4, 8, 0, 0),
        ):
            next_sheet = self.env["restaurant.stock.daily"].with_user(
                self.stockkeeper
            ).create({
                "branch_id": self.branch.id,
                "stock_date": next_service_date,
            })
        next_line = next_sheet.line_ids.filtered(
            lambda line: line.product_id == self.product
        )

        self.assertEqual(next_line.opening_qty, 9)
        self.assertEqual(next_line.received_qty, 0)

        # Merely preparing the next sheet must not switch the dashboard.
        values = dashboard_values(self.stockkeeper)
        self.assertEqual(values["restaurant_stock_opening"], 20)
        self.assertEqual(values["restaurant_stock_consumption"], 10)

        with patch.object(
            fields.Datetime,
            "now",
            return_value=datetime(2026, 10, 4, 8, 0, 0),
        ):
            next_sheet.action_confirm_opening()
        values = dashboard_values(self.stockkeeper)
        self.assertEqual(values["restaurant_stock_opening"], 9)
        self.assertEqual(values["restaurant_stock_received_today"], 0)
        self.assertEqual(values["restaurant_stock_consumption"], 0)
        self.assertEqual(values["restaurant_stock_on_hand"], 9)
        self.assertEqual(values["restaurant_stock_available"], 9)

        transit_location = self.company.internal_transit_location_id
        self.env["stock.quant"]._update_available_quantity(
            self.product,
            transit_location,
            3,
        )
        receipt = self.env["stock.picking"].sudo().create({
            "picking_type_id": self.warehouse.int_type_id.id,
            "location_id": transit_location.id,
            "location_dest_id": self.warehouse.lot_stock_id.id,
            "company_id": self.company.id,
            "origin": "Overnight service-day regression",
            "move_ids": [Command.create({
                "product_id": self.product.id,
                "product_uom_qty": 3,
                "uom_id": self.product.uom_id.id,
                "location_id": transit_location.id,
                "location_dest_id": self.warehouse.lot_stock_id.id,
                "company_id": self.company.id,
            })],
        })
        receipt.action_confirm()
        receipt.action_assign()
        for move in receipt.move_ids:
            move.quantity = move.product_uom_qty
        self.assertTrue(receipt.button_validate())
        receipt.write({
            "date_done": datetime(2026, 10, 4, 8, 30, 0),
        })
        next_sheet.action_refresh_movements()
        next_line.invalidate_recordset(["received_qty"])
        self.assertEqual(next_line.received_qty, 3)

        values = dashboard_values(self.stockkeeper)

        self.assertEqual(values["restaurant_stock_opening"], 9)
        self.assertEqual(values["restaurant_stock_received_today"], 3)
        self.assertEqual(values["restaurant_stock_consumption"], 0)
        self.assertEqual(values["restaurant_stock_on_hand"], 12)
        self.assertEqual(values["restaurant_stock_available"], 12)
        manager_values = dashboard_values(self.manager)
        self.assertEqual(
            {field: manager_values[field] for field in dashboard_fields},
            {field: values[field] for field in dashboard_fields},
        )

        next_line.write({
            "consumption_qty": 2,
            "actual_closing_qty": 10,
            "actual_closing_state": "counted",
        })
        next_sheet.action_submit_closing()
        next_sheet.with_user(self.manager).action_close()

        values = dashboard_values(self.stockkeeper)

        self.assertEqual(values["restaurant_stock_consumption"], 2)
        self.assertEqual(values["restaurant_stock_on_hand"], 10)
        self.assertEqual(values["restaurant_stock_available"], 10)

    def test_other_branch_cannot_read_incoming_request(self):
        daily = self._opened_daily()
        daily.action_submit_closing()

        with self.assertRaises(AccessError):
            daily.generated_request_id.with_user(
                self.other_stockkeeper
            ).check_access("read")

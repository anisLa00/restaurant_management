from datetime import datetime, timedelta
from unittest.mock import patch

from odoo import Command, fields
from odoo.exceptions import AccessError, UserError, ValidationError
from odoo.tests import Form
from odoo.tests.common import new_test_user

from odoo.addons.base.tests.common import BaseCommon
from odoo.addons.restaurant_stock.models import (
    restaurant_stock_daily,
    stock_dashboard,
)


class TestBranchReceipt(BaseCommon):
    _test_user_groups = ()

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.company = cls.env.company
        cls.transit_location = cls.company.internal_transit_location_id
        cls.branch_warehouse, cls.other_warehouse = cls.env["stock.warehouse"].create([
            {
                "name": "Receipt Test Branch Warehouse",
                "code": "RTB",
                "company_id": cls.company.id,
            },
            {
                "name": "Receipt Test Other Warehouse",
                "code": "RTO",
                "company_id": cls.company.id,
            },
        ])
        cls.branch, cls.other_branch = cls.env["restaurant.branch"].create([
            {
                "name": "Receipt Test Branch",
                "code": "RTB",
                "company_id": cls.company.id,
                "warehouse_id": cls.branch_warehouse.id,
            },
            {
                "name": "Receipt Test Other Branch",
                "code": "RTO",
                "company_id": cls.company.id,
                "warehouse_id": cls.other_warehouse.id,
            },
        ])
        cls.stockkeeper = cls._make_user(
            "receipt_stockkeeper",
            "restaurant_core.group_restaurant_stockkeeper",
            cls.branch,
        )
        cls.manager = cls._make_user(
            "receipt_manager",
            "restaurant_core.group_restaurant_branch_manager",
            cls.branch,
        )
        cls.wrong_branch_stockkeeper = cls._make_user(
            "receipt_wrong_branch_stockkeeper",
            "restaurant_core.group_restaurant_stockkeeper",
            cls.other_branch,
        )
        cls.no_role_user = cls._make_user(
            "receipt_no_role_user",
            "base.group_user",
            cls.branch,
        )
        cls.central_storekeeper = cls._make_user(
            "receipt_central_storekeeper",
            "restaurant_core.group_restaurant_central_storekeeper",
            cls.branch,
        )
        cls.central_branch_manager = cls._make_user(
            "receipt_central_branch_manager",
            (
                "restaurant_core.group_restaurant_central_storekeeper,"
                "restaurant_core.group_restaurant_branch_manager"
            ),
            cls.branch,
        )
        cls.central_warehouse = cls.env["stock.warehouse"].search([
            ("company_id", "=", cls.company.id),
            ("code", "=", "WH"),
        ], limit=1)
        cls.product = cls.env["product.product"].create({
            "name": "Branch Receipt Test Product",
            "is_storable": True,
        })
        cls.mirdif = cls.env.ref("restaurant_core.branch_mirdif")
        cls.jafilya = cls.env.ref("restaurant_core.branch_jafilya")
        cls.global_village = cls.env.ref(
            "restaurant_core.branch_global_village"
        )
        cls.mirdif_stockkeeper = cls._make_user(
            "receipt_mirdif_stockkeeper",
            "restaurant_core.group_restaurant_stockkeeper",
            cls.mirdif,
        )
        cls.jafilya_stockkeeper = cls._make_user(
            "receipt_jafilya_stockkeeper",
            "restaurant_core.group_restaurant_stockkeeper",
            cls.jafilya,
        )

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

    def _request(
        self,
        dispatched_qty=10,
        received_qty=10,
        state="dispatched",
        branch=None,
        stockkeeper=None,
    ):
        branch = branch or self.branch
        stockkeeper = stockkeeper or self.stockkeeper
        request = self.env["restaurant.stock.request"].with_user(
            stockkeeper
        ).create({
            "branch_id": branch.id,
            "line_ids": [Command.create({
                "product_id": self.product.id,
                "requested_qty": dispatched_qty,
            })],
        })
        if state == "dispatched":
            self.env["stock.quant"]._update_available_quantity(
                self.product,
                self.central_warehouse.lot_stock_id,
                dispatched_qty,
            )
            request.action_submit()
            central_request = request.with_user(self.central_storekeeper)
            central_request.action_calculate_availability()
            central_request.action_prepare_transfer()
            central_request.action_dispatch()
            receipt_values = {"branch_received_qty": received_qty}
            if received_qty < dispatched_qty:
                receipt_values["shortage_reason"] = "missing"
            request.line_ids.write(receipt_values)
        return request.with_user(stockkeeper).with_context(
            allowed_company_ids=[self.company.id]
        )

    def _daily_sheet(self):
        Daily = self.env["restaurant.stock.daily"].with_user(self.stockkeeper)
        return Daily.create({
            "branch_id": self.branch.id,
            "stock_date": fields.Date.context_today(
                Daily.with_context(tz=self.company.tz or "UTC")
            ),
        })

    def _historical_daily(self, user, values, confirm=False):
        daily = (
            self.env["restaurant.stock.daily"]
            .with_user(user)
            .sudo()
            .with_context(**{
                restaurant_stock_daily.ALLOW_NONCURRENT_DAILY_DATE_CONTEXT:
                    True,
            })
            .create(values)
        )
        if confirm:
            daily.action_confirm_opening()
        return daily.with_user(user).with_context(
            allowed_company_ids=user.company_ids.ids,
        )

    def _closed_daily(self, stock_date, opening_qty, closing_qty):
        daily = self._historical_daily(self.stockkeeper, {
            "branch_id": self.branch.id,
            "stock_date": stock_date,
            "line_ids": [Command.create({
                "product_id": self.product.id,
                "opening_qty": opening_qty,
                "actual_closing_qty": closing_qty,
                "actual_closing_state": "counted",
            })],
        }, confirm=True)
        daily.action_submit_closing()
        daily.with_user(self.manager).action_close()
        return daily

    def _distribution(self, sent_qty, received_qty):
        self.env["stock.quant"]._update_available_quantity(
            self.product,
            self.central_warehouse.lot_stock_id,
            sent_qty,
        )
        distribution = self.env["restaurant.stock.distribution"].with_user(
            self.central_storekeeper
        ).create({
            "branch_id": self.branch.id,
            "line_ids": [Command.create({
                "product_id": self.product.id,
                "sent_qty": sent_qty,
            })],
        })
        distribution.action_dispatch()

        branch_distribution = distribution.with_user(self.stockkeeper)
        receipt_values = {"received_qty": received_qty}
        if received_qty < sent_qty:
            receipt_values["shortage_reason"] = "missing"
        branch_distribution.line_ids.write(receipt_values)
        branch_distribution.action_confirm_receipt()
        return branch_distribution

    def _stock_sections(self, assign_product=True):
        Section = self.env["restaurant.stock.section"].with_user(
            self.stockkeeper
        )
        kitchen, bar = Section.create([
            {
                "name": "Receipt Kitchen",
                "section_type": "kitchen",
                "branch_id": self.branch.id,
            },
            {
                "name": "Receipt Bar",
                "section_type": "bar",
                "branch_id": self.branch.id,
            },
        ])
        if assign_product:
            self.env["restaurant.stock.section.product"].with_user(
                self.stockkeeper
            ).create({
                "section_id": kitchen.id,
                "product_id": self.product.id,
            })
        return kitchen, bar

    def _native_receipt(self, product, warehouse, quantity, uom=None):
        company = warehouse.company_id
        uom = uom or product.uom_id
        quantity_in_product_uom = uom._compute_quantity(
            quantity,
            product.uom_id,
        )
        company_context = {
            "allowed_company_ids": [company.id],
        }
        self.env["stock.quant"].sudo().with_company(company).with_context(
            **company_context
        )._update_available_quantity(
            product,
            company.internal_transit_location_id,
            quantity_in_product_uom,
        )
        picking = self.env["stock.picking"].sudo().with_company(
            company
        ).with_context(**company_context).create({
            "picking_type_id": warehouse.int_type_id.id,
            "location_id": company.internal_transit_location_id.id,
            "location_dest_id": warehouse.lot_stock_id.id,
            "company_id": company.id,
            "origin": "Native receipt isolation test",
            "move_ids": [Command.create({
                "product_id": product.id,
                "product_uom_qty": quantity,
                "uom_id": uom.id,
                "location_id": company.internal_transit_location_id.id,
                "location_dest_id": warehouse.lot_stock_id.id,
                "company_id": company.id,
            })],
        })
        picking.action_confirm()
        picking.action_assign()
        for move in picking.move_ids:
            move.quantity = quantity
        self.assertTrue(picking.with_context(skip_backorder=True).button_validate())
        self.assertEqual(picking.state, "done")
        return picking

    def _closed_daily_after_old_receipt(self, old_received_qty=29):
        old_request = self._request(
            dispatched_qty=old_received_qty,
            received_qty=old_received_qty,
        )
        old_request.action_confirm_branch_receipt()
        old_request.receipt_picking_id.sudo().write({
            "date_done": fields.Datetime.now() - timedelta(minutes=1),
        })

        daily = self._daily_sheet()
        line = daily.line_ids.filtered(
            lambda item: item.product_id == self.product
        )
        daily.action_confirm_opening()
        line.write({
            "consumption_qty": old_received_qty - 8,
            "waste_qty": 1,
            "damaged_qty": 2,
            "actual_closing_qty": 8,
            "actual_closing_state": "counted",
        })
        daily.action_submit_closing()
        daily.with_user(self.manager).action_close()
        return daily

    def _dashboard_values(self):
        template = (
            self.product.product_tmpl_id
            .with_user(self.stockkeeper)
            .with_context(allowed_company_ids=[self.company.id])
        )
        dashboard_fields = [
            "restaurant_stock_opening",
            "restaurant_stock_received_today",
            "restaurant_stock_consumption",
            "restaurant_stock_waste",
            "restaurant_stock_damaged",
            "restaurant_stock_on_hand",
            "restaurant_stock_available",
        ]
        template.invalidate_recordset(dashboard_fields)
        return template.read(dashboard_fields)[0]

    def test_exact_receipt_creates_done_internal_picking(self):
        request = self._request()

        request.action_confirm_branch_receipt()

        picking = request.receipt_picking_id.sudo()
        self.assertEqual(request.state, "received")
        self.assertEqual(request.branch_received_by_id, self.stockkeeper)
        self.assertEqual(picking.state, "done")
        self.assertEqual(picking.location_id, self.transit_location)
        self.assertEqual(picking.location_dest_id, self.branch_warehouse.lot_stock_id)
        self.assertEqual(picking.move_ids.quantity, 10)
        self.assertEqual(
            self.env["stock.quant"]._get_available_quantity(
                self.product, self.branch_warehouse.lot_stock_id
            ),
            10,
        )

    def test_partial_receipt_leaves_difference_in_transit(self):
        request = self._request(dispatched_qty=10, received_qty=7)

        self.assertEqual(request.line_ids.requested_qty, 10)
        self.assertEqual(request.line_ids.confirmed_qty, 10)
        self.assertEqual(request.line_ids.dispatched_qty, 10)
        self.assertEqual(request.line_ids.branch_received_qty, 7)
        self.assertEqual(request.branch_status, "delivered")
        self.assertEqual(request.line_ids.current_on_hand_qty, 0)

        request.action_confirm_branch_receipt()
        request.line_ids.invalidate_recordset(["current_on_hand_qty"])

        self.assertEqual(request.state, "received")
        self.assertEqual(request.branch_status, "received")
        self.assertEqual(request.line_ids.difference_qty, 3)
        self.assertEqual(request.line_ids.current_on_hand_qty, 7)
        self.assertEqual(request.receipt_picking_id.sudo().move_ids.quantity, 7)
        self.assertEqual(
            self.env["stock.quant"]._get_available_quantity(
                self.product, self.transit_location
            ),
            3,
        )

    def test_duplicate_receipt_is_rejected(self):
        request = self._request()
        request.action_confirm_branch_receipt()

        with self.assertRaises(UserError):
            request.sudo().with_user(self.stockkeeper).action_confirm_branch_receipt()

    def test_wrong_branch_stockkeeper_is_rejected(self):
        request = self._request()

        with self.assertRaises(AccessError):
            request.sudo().with_user(
                self.wrong_branch_stockkeeper
            ).action_confirm_branch_receipt()

    def test_user_without_stockkeeper_role_is_rejected(self):
        request = self._request()

        with self.assertRaises(AccessError):
            request.sudo().with_user(
                self.no_role_user
            ).action_confirm_branch_receipt()

    def test_invalid_request_state_is_rejected(self):
        request = self._request(state="draft")

        with self.assertRaises(UserError):
            request.action_confirm_branch_receipt()

    def test_daily_stock_full_central_distribution_receipt(self):
        daily = self._daily_sheet()

        distribution = self._distribution(sent_qty=20, received_qty=20)

        line = daily.line_ids.filtered(lambda item: item.product_id == self.product)
        self.assertEqual(line.received_qty, 20)
        self.assertEqual(line.incoming_transfer_qty, 0)
        self.assertEqual(line.expected_closing_qty, 20)
        self.assertTrue(distribution.activity_ids.filtered(
            lambda activity: activity.user_id == self.stockkeeper
        ))

    def test_daily_stock_partial_central_distribution_receipt(self):
        daily = self._daily_sheet()

        self._distribution(sent_qty=20, received_qty=18)

        line = daily.line_ids.filtered(lambda item: item.product_id == self.product)
        self.assertEqual(line.received_qty, 18)
        self.assertEqual(
            self.env["stock.quant"]._get_available_quantity(
                self.product, self.transit_location
            ),
            2,
        )

    def test_daily_stock_emergency_request_receipt(self):
        daily = self._daily_sheet()
        request = self._request(dispatched_qty=9, received_qty=7)

        request.action_confirm_branch_receipt()

        line = daily.line_ids.filtered(lambda item: item.product_id == self.product)
        self.assertEqual(line.received_qty, 7)

    def test_receipt_confirmation_creates_daily_with_received_quantity(self):
        request = self._request(dispatched_qty=12, received_qty=9)
        request.action_confirm_branch_receipt()

        daily = self.env["restaurant.stock.daily"].search([
            ("branch_id", "=", self.branch.id),
            ("state", "=", "opened"),
        ])

        self.assertEqual(len(daily), 1)
        line = daily.line_ids.filtered(lambda item: item.product_id == self.product)
        self.assertEqual(line.opening_qty, 0)
        self.assertEqual(line.received_qty, 9)
        self.assertEqual(line.current_on_hand_qty, 9)

    def test_first_receipt_reconstructs_zero_opening_without_double_counting(self):
        request = self._request(dispatched_qty=20, received_qty=20)

        request.action_confirm_branch_receipt()

        daily = self.env["restaurant.stock.daily"].search([
            ("branch_id", "=", self.branch.id),
            ("state", "=", "opened"),
        ])
        line = daily.line_ids.filtered(
            lambda item: item.product_id == self.product
        )
        self.assertEqual(line.opening_qty, 0)
        self.assertEqual(line.received_qty, 20)
        self.assertEqual(line.current_on_hand_qty, 20)
        line.write({
            "consumption_qty": 3,
            "waste_qty": 1,
            "damaged_qty": 2,
        })

        daily.action_refresh_movements()
        daily.action_refresh_movements()
        line.invalidate_recordset([
            "opening_qty",
            "received_qty",
            "current_on_hand_qty",
        ])
        self.assertEqual(line.opening_qty, 0)
        self.assertEqual(line.received_qty, 20)
        self.assertEqual(line.current_on_hand_qty, 20)
        self.assertEqual(line.consumption_qty, 3)
        self.assertEqual(line.waste_qty, 1)
        self.assertEqual(line.damaged_qty, 2)
        self.assertEqual(
            self.env["stock.quant"]._get_available_quantity(
                self.product,
                self.branch_warehouse.lot_stock_id,
            ),
            20,
        )

    def test_first_receipt_preserves_preexisting_native_opening(self):
        self.env["stock.quant"]._update_available_quantity(
            self.product,
            self.branch_warehouse.lot_stock_id,
            10,
        )
        request = self._request(dispatched_qty=20, received_qty=20)

        request.action_confirm_branch_receipt()

        daily = self.env["restaurant.stock.daily"].search([
            ("branch_id", "=", self.branch.id),
            ("state", "=", "opened"),
        ])
        line = daily.line_ids.filtered(
            lambda item: item.product_id == self.product
        )
        self.assertEqual(line.opening_qty, 10)
        self.assertEqual(line.received_qty, 20)
        self.assertEqual(line.current_on_hand_qty, 30)

        daily.action_refresh_movements()
        daily.action_refresh_movements()
        line.invalidate_recordset([
            "opening_qty",
            "received_qty",
            "current_on_hand_qty",
        ])
        self.assertEqual(line.opening_qty, 10)
        self.assertEqual(line.received_qty, 20)
        self.assertEqual(line.current_on_hand_qty, 30)

    def test_refresh_uses_actual_receipt_date_and_preserves_opening(self):
        self.company.tz = "Asia/Dubai"
        receipt_date = fields.Date.to_date("2026-10-03")
        selected_date = fields.Date.to_date("2026-10-04")
        selected_daily = self._historical_daily(self.stockkeeper, {
            "branch_id": self.branch.id,
            "stock_date": selected_date,
            "line_ids": [Command.create({
                "product_id": self.product.id,
                "opening_qty": 10,
            })],
        }, confirm=True)
        request = self._request(dispatched_qty=20, received_qty=20)
        receipt_timestamp = datetime(2026, 10, 3, 12, 0, 0)

        with patch.object(fields.Datetime, "now", return_value=receipt_timestamp):
            request.action_confirm_branch_receipt()

        actual_daily = self.env["restaurant.stock.daily"].search([
            ("branch_id", "=", self.branch.id),
            ("stock_date", "=", receipt_date),
            ("state", "!=", "cancelled"),
        ])
        self.assertEqual(actual_daily.line_ids.received_qty, 20)

        selected_daily.action_refresh_movements()
        selected_daily.action_refresh_movements()
        selected_line = selected_daily.line_ids.filtered(
            lambda item: item.product_id == self.product
        )
        selected_line.invalidate_recordset([
            "opening_qty",
            "received_qty",
            "current_on_hand_qty",
        ])
        self.assertEqual(selected_line.opening_qty, 10)
        self.assertEqual(selected_line.received_qty, 0)
        self.assertEqual(selected_line.current_on_hand_qty, 10)

    def test_daily_outgoing_quantities_cannot_exceed_current(self):
        daily = self.env["restaurant.stock.daily"].with_user(
            self.stockkeeper
        ).create({
            "branch_id": self.branch.id,
            "stock_date": fields.Date.context_today(self.stockkeeper),
            "line_ids": [Command.create({
                "product_id": self.product.id,
                "opening_qty": 20,
            })],
        })
        line = daily.line_ids

        with self.assertRaisesRegex(
            ValidationError,
            r"Sold \+ Complimentary \+ legacy consumption 25.0.*"
            r"cannot exceed Current 20.0",
        ):
            line.write({"consumption_qty": 25})

        line.write({
            "consumption_qty": 15,
            "waste_qty": 4,
        })
        with self.assertRaisesRegex(
            ValidationError,
            r"Sold \+ Complimentary \+ legacy consumption \+ Waste \+ Damaged "
            r"\(21.0.*Current \(20.0",
        ):
            line.write({"damaged_qty": 2})

        line.write({"damaged_qty": 1})
        self.assertEqual(line.expected_closing_qty, 0)

        precision_digits = self.env[
            "decimal.precision"
        ].precision_get("Product Unit")
        tolerance = 10 ** (-(precision_digits + 1))
        line.write({
            "consumption_qty": 20 + tolerance,
            "waste_qty": 0,
            "damaged_qty": 0,
        })
        self.assertEqual(
            line.uom_id.compare(line.consumption_qty, line.current_on_hand_qty),
            0,
        )

        with self.assertRaisesRegex(
            ValidationError,
            "stock quantities cannot be negative",
        ):
            line.write({"waste_qty": -(10 ** -precision_digits)})

        line.write({
            "consumption_qty": 20,
            "waste_qty": 0,
            "damaged_qty": 0,
        })
        daily.action_confirm_opening()
        line.flush_recordset(["consumption_qty"])
        self.env.cr.execute(
            """
                UPDATE restaurant_stock_daily_line
                   SET consumption_qty = 25
                 WHERE id = %s
            """,
            [line.id],
        )
        line.invalidate_recordset(["consumption_qty"], flush=False)
        with self.assertRaisesRegex(
            ValidationError,
            r"Sold \+ Complimentary \+ legacy consumption 25.0.*"
            r"cannot exceed Current 20.0",
        ):
            daily.action_submit_closing()
        self.assertEqual(daily.state, "opened")

    def test_multiple_receipts_keep_reconstructed_opening_stable(self):
        self.env["stock.quant"]._update_available_quantity(
            self.product,
            self.branch_warehouse.lot_stock_id,
            10,
        )
        first_request = self._request(dispatched_qty=5, received_qty=5)
        first_request.action_confirm_branch_receipt()
        daily = self.env["restaurant.stock.daily"].search([
            ("branch_id", "=", self.branch.id),
            ("state", "=", "opened"),
        ])
        line = daily.line_ids.filtered(
            lambda item: item.product_id == self.product
        )
        self.assertEqual(line.opening_qty, 10)
        self.assertEqual(line.received_qty, 5)
        self.assertEqual(line.current_on_hand_qty, 15)

        second_request = self._request(dispatched_qty=3, received_qty=3)
        second_request.action_confirm_branch_receipt()

        line.invalidate_recordset([
            "opening_qty",
            "received_qty",
            "current_on_hand_qty",
        ])
        self.assertEqual(line.opening_qty, 10)
        self.assertEqual(line.received_qty, 8)
        self.assertEqual(line.current_on_hand_qty, 18)

    def test_post_close_receipts_carry_once_into_next_opening(self):
        self.company.tz = "Asia/Dubai"
        prior_date = fields.Date.to_date("2026-10-03")
        next_date = fields.Date.to_date("2026-10-04")
        third_date = fields.Date.to_date("2026-10-05")
        prior_close_at = datetime(2026, 10, 3, 12, 0, 0)
        late_receipt_at = datetime(2026, 10, 3, 15, 0, 0)
        next_close_at = datetime(2026, 10, 4, 12, 0, 0)
        lamb = self.env["product.product"].create({
            "name": "Post-close Lamb",
            "is_storable": True,
        })

        for product, quantity in ((self.product, 10), (lamb, 31)):
            self.env["stock.quant"]._update_available_quantity(
                product,
                self.branch_warehouse.lot_stock_id,
                quantity,
            )

        prior = self._historical_daily(self.stockkeeper, {
            "branch_id": self.branch.id,
            "stock_date": prior_date,
            "line_ids": [
                Command.create({
                    "product_id": self.product.id,
                    "opening_qty": 10,
                    "actual_closing_qty": 10,
                    "actual_closing_state": "counted",
                }),
                Command.create({
                    "product_id": lamb.id,
                    "opening_qty": 31,
                    "actual_closing_qty": 31,
                    "actual_closing_state": "counted",
                }),
            ],
        }, confirm=True)
        prior.action_submit_closing()
        with patch.object(
            fields.Datetime,
            "now",
            return_value=prior_close_at,
        ):
            prior.with_user(self.manager).action_close()

        chicken_receipt = self._native_receipt(
            self.product,
            self.branch_warehouse,
            40,
        )
        lamb_receipt = self._native_receipt(
            lamb,
            self.branch_warehouse,
            10,
        )
        (chicken_receipt | lamb_receipt).write({
            "date_done": late_receipt_at,
        })

        next_daily = self._historical_daily(self.stockkeeper, {
            "branch_id": self.branch.id,
            "stock_date": next_date,
        }, confirm=True)
        chicken_line = next_daily.line_ids.filtered(
            lambda line: line.product_id == self.product
        )
        lamb_line = next_daily.line_ids.filtered(
            lambda line: line.product_id == lamb
        )
        self.assertEqual(chicken_line.opening_qty, 50)
        self.assertEqual(lamb_line.opening_qty, 41)
        self.assertEqual(chicken_line.received_qty, 0)
        self.assertEqual(lamb_line.received_qty, 0)

        next_daily.action_refresh_movements()
        next_daily.action_refresh_movements()
        chicken_line.invalidate_recordset([
            "opening_qty",
            "received_qty",
        ])
        lamb_line.invalidate_recordset([
            "opening_qty",
            "received_qty",
        ])
        self.assertEqual(chicken_line.opening_qty, 50)
        self.assertEqual(lamb_line.opening_qty, 41)
        self.assertEqual(chicken_line.received_qty, 0)
        self.assertEqual(lamb_line.received_qty, 0)

        chicken_line.write({
            "consumption_qty": 5,
            "actual_closing_qty": 45,
            "actual_closing_state": "counted",
        })
        lamb_line.write({
            "consumption_qty": 20,
            "actual_closing_qty": 21,
            "actual_closing_state": "counted",
        })
        next_daily.action_submit_closing()
        with patch.object(
            fields.Datetime,
            "now",
            return_value=next_close_at,
        ):
            next_daily.with_user(self.manager).action_close()

        self.assertEqual(
            self.env["stock.quant"]._get_available_quantity(
                self.product,
                self.branch_warehouse.lot_stock_id,
            ),
            45,
        )
        self.assertEqual(
            self.env["stock.quant"]._get_available_quantity(
                lamb,
                self.branch_warehouse.lot_stock_id,
            ),
            21,
        )

        third_daily = self._historical_daily(self.stockkeeper, {
            "branch_id": self.branch.id,
            "stock_date": third_date,
        })
        self.assertEqual(
            third_daily.line_ids.filtered(
                lambda line: line.product_id == self.product
            ).opening_qty,
            45,
        )
        self.assertEqual(
            third_daily.line_ids.filtered(
                lambda line: line.product_id == lamb
            ).opening_qty,
            21,
        )

    def test_pre_close_receipt_is_owned_and_day_boundary_stays_received(self):
        self.company.tz = "Asia/Dubai"
        prior_date = fields.Date.to_date("2026-10-03")
        next_date = fields.Date.to_date("2026-10-04")
        receipt_before_close_at = datetime(2026, 10, 3, 11, 0, 0)
        prior_close_at = datetime(2026, 10, 3, 12, 0, 0)
        next_day_start = datetime(2026, 10, 3, 20, 0, 0)

        self.env["stock.quant"]._update_available_quantity(
            self.product,
            self.branch_warehouse.lot_stock_id,
            10,
        )
        prior = self._historical_daily(self.stockkeeper, {
            "branch_id": self.branch.id,
            "stock_date": prior_date,
            "line_ids": [Command.create({
                "product_id": self.product.id,
                "opening_qty": 10,
            })],
        }, confirm=True)

        owned_receipt = self._native_receipt(
            self.product,
            self.branch_warehouse,
            5,
        )
        owned_receipt.write({"date_done": receipt_before_close_at})
        prior.action_refresh_movements()
        prior_line = prior.line_ids.filtered(
            lambda line: line.product_id == self.product
        )
        self.assertEqual(prior_line.received_qty, 5)
        prior_line.write({
            "actual_closing_qty": 15,
            "actual_closing_state": "counted",
        })
        prior.action_submit_closing()
        with patch.object(
            fields.Datetime,
            "now",
            return_value=prior_close_at,
        ):
            prior.with_user(self.manager).action_close()

        boundary_receipt = self._native_receipt(
            self.product,
            self.branch_warehouse,
            3,
        )
        boundary_receipt.write({"date_done": next_day_start})

        next_daily = self._historical_daily(self.stockkeeper, {
            "branch_id": self.branch.id,
            "stock_date": next_date,
        }, confirm=True)
        next_line = next_daily.line_ids.filtered(
            lambda line: line.product_id == self.product
        )
        self.assertEqual(next_line.opening_qty, 15)
        self.assertEqual(next_line.received_qty, 3)
        self.assertEqual(next_line.current_on_hand_qty, 18)

        next_daily.action_refresh_movements()
        next_daily.action_refresh_movements()
        next_line.invalidate_recordset([
            "opening_qty",
            "received_qty",
            "current_on_hand_qty",
        ])
        self.assertEqual(next_line.opening_qty, 15)
        self.assertEqual(next_line.received_qty, 3)
        self.assertEqual(next_line.current_on_hand_qty, 18)

    def test_closed_same_date_new_form_hides_recalculated_opening(self):
        self.company.tz = "Asia/Dubai"
        prior_date = fields.Date.to_date("2026-10-03")
        closed_date = fields.Date.to_date("2026-10-04")
        next_date = fields.Date.to_date("2026-10-05")
        prior_close_at = datetime(2026, 10, 3, 12, 0, 0)
        late_receipt_at = datetime(2026, 10, 3, 15, 0, 0)
        final_close_at = datetime(2026, 10, 4, 0, 50, 4)
        lamb = self.env["product.product"].create({
            "name": "Same-date Form Lamb",
            "is_storable": True,
        })

        for product, quantity in ((self.product, 10), (lamb, 31)):
            self.env["stock.quant"]._update_available_quantity(
                product,
                self.branch_warehouse.lot_stock_id,
                quantity,
            )

        prior = self._historical_daily(self.stockkeeper, {
            "branch_id": self.branch.id,
            "stock_date": prior_date,
            "line_ids": [
                Command.create({
                    "product_id": self.product.id,
                    "opening_qty": 10,
                    "actual_closing_qty": 10,
                    "actual_closing_state": "counted",
                }),
                Command.create({
                    "product_id": lamb.id,
                    "opening_qty": 31,
                    "actual_closing_qty": 31,
                    "actual_closing_state": "counted",
                }),
            ],
        }, confirm=True)
        prior.action_submit_closing()
        with patch.object(
            fields.Datetime,
            "now",
            return_value=prior_close_at,
        ):
            prior.with_user(self.manager).action_close()

        chicken_receipt = self._native_receipt(
            self.product,
            self.branch_warehouse,
            40,
        )
        lamb_receipt = self._native_receipt(
            lamb,
            self.branch_warehouse,
            10,
        )
        (chicken_receipt | lamb_receipt).write({
            "date_done": late_receipt_at,
        })

        final_daily = self._historical_daily(self.stockkeeper, {
            "branch_id": self.branch.id,
            "stock_date": closed_date,
            "line_ids": [
                Command.create({
                    "product_id": self.product.id,
                    "opening_qty": 10,
                    "consumption_qty": 5,
                    "actual_closing_qty": 5,
                    "actual_closing_state": "counted",
                }),
                Command.create({
                    "product_id": lamb.id,
                    "opening_qty": 31,
                    "consumption_qty": 20,
                    "actual_closing_qty": 11,
                    "actual_closing_state": "counted",
                }),
            ],
        }, confirm=True)
        final_daily.action_submit_closing()
        with patch.object(
            fields.Datetime,
            "now",
            return_value=final_close_at,
        ):
            final_daily.with_user(self.manager).action_close()

        Daily = self.env["restaurant.stock.daily"].with_user(
            self.stockkeeper
        )
        raw_duplicate = Daily.new({
            "branch_id": self.branch.id,
            "stock_date": closed_date,
        })
        raw_opening = {
            values["product_id"]: values["opening_qty"]
            for values in raw_duplicate._prepare_automatic_opening_lines()
        }
        self.assertEqual(raw_opening[self.product.id], 50)
        self.assertEqual(raw_opening[lamb.id], 41)

        raw_duplicate._onchange_opening_context()
        self.assertEqual(raw_duplicate.existing_daily_id, final_daily)
        self.assertFalse(raw_duplicate.line_ids)

        with patch.object(
            fields.Datetime,
            "now",
            return_value=datetime(2026, 10, 4, 2, 0, 0),
        ):
            defaults = Daily.default_get([
                "branch_id",
                "stock_date",
                "line_ids",
            ])
        self.assertEqual(defaults["stock_date"], closed_date)
        self.assertEqual(defaults["branch_id"], self.branch.id)
        self.assertEqual(defaults["line_ids"], [])

        next_draft = Daily.new({
            "branch_id": self.branch.id,
            "stock_date": next_date,
        })
        next_draft._onchange_opening_context()
        next_opening = {
            line.product_id.id: line.opening_qty
            for line in next_draft.line_ids
        }
        self.assertEqual(next_opening[self.product.id], 5)
        self.assertEqual(next_opening[lamb.id], 11)

        final_lines = {
            line.product_id.id: line.actual_closing_qty
            for line in final_daily.line_ids
        }
        self.assertEqual(final_lines[self.product.id], 5)
        self.assertEqual(final_lines[lamb.id], 11)
        self.assertEqual(
            self.env["stock.quant"]._get_available_quantity(
                self.product,
                self.branch_warehouse.lot_stock_id,
            ),
            45,
        )
        self.assertEqual(
            self.env["stock.quant"]._get_available_quantity(
                lamb,
                self.branch_warehouse.lot_stock_id,
            ),
            21,
        )
        daily_form_arch = self.env.ref(
            "restaurant_stock.restaurant_stock_daily_view_form"
        ).arch_db
        self.assertIn('name="existing_daily_id"', daily_form_arch)
        self.assertIn(
            "duplicate Opening lines are intentionally hidden",
            daily_form_arch,
        )

    def test_new_daily_form_hides_existing_receipt_sheet(self):
        self._distribution(sent_qty=10, received_qty=7)
        Daily = self.env["restaurant.stock.daily"].with_user(
            self.stockkeeper
        ).with_context(allowed_company_ids=[self.company.id])

        defaults = Daily.default_get([
            "branch_id",
            "stock_date",
            "line_ids",
        ])
        self.assertEqual(defaults["branch_id"], self.branch.id)
        self.assertEqual(defaults["line_ids"], [])

        existing = Daily.search([
            ("branch_id", "=", self.branch.id),
            ("stock_date", "=", defaults["stock_date"]),
            ("state", "!=", "cancelled"),
        ])
        self.assertEqual(len(existing), 1)
        self.assertEqual(existing.line_ids.received_qty, 7)

        duplicate = Daily.new(defaults)
        self.assertEqual(duplicate.existing_daily_id, existing)
        self.assertFalse(duplicate.line_ids)

    def test_daily_stock_refresh_is_idempotent(self):
        daily = self._daily_sheet()
        request = self._request(dispatched_qty=10, received_qty=10)
        request.action_confirm_branch_receipt()

        daily.action_refresh_movements()
        daily.action_refresh_movements()

        line = daily.line_ids.filtered(lambda item: item.product_id == self.product)
        self.assertEqual(line.received_qty, 10)
        self.assertEqual(line.expected_closing_qty, 10)

    def test_daily_stock_adds_product_received_after_opening(self):
        daily = self._daily_sheet()
        self.assertFalse(daily.line_ids.filtered(
            lambda item: item.product_id == self.product
        ))

        request = self._request(dispatched_qty=6, received_qty=6)
        request.action_confirm_branch_receipt()
        daily.action_refresh_movements()

        line = daily.line_ids.filtered(lambda item: item.product_id == self.product)
        self.assertEqual(len(line), 1)
        self.assertEqual(line.opening_qty, 0)
        self.assertEqual(line.uom_id, self.product.uom_id)
        self.assertEqual(line.received_qty, 6)

    def test_dashboard_uses_frozen_daily_opening_after_receipt(self):
        today = fields.Date.context_today(
            self.env.user.with_context(tz=self.company.tz or "UTC")
        )
        self._closed_daily(today - timedelta(days=1), 10, 10)
        self.env["stock.quant"]._update_available_quantity(
            self.product,
            self.branch_warehouse.lot_stock_id,
            10,
        )
        daily = self._daily_sheet()
        line = daily.line_ids.filtered(lambda item: item.product_id == self.product)
        self.assertEqual(line.opening_qty, 10)
        daily.action_confirm_opening()

        request = self._request(dispatched_qty=25, received_qty=20)
        request.action_confirm_branch_receipt()

        line.invalidate_recordset(["received_qty", "opening_qty"])
        values = self._dashboard_values()
        self.assertEqual(line.opening_qty, 10)
        self.assertEqual(line.received_qty, 20)
        self.assertEqual(values["restaurant_stock_opening"], 10)
        self.assertEqual(values["restaurant_stock_received_today"], 20)
        self.assertEqual(values["restaurant_stock_on_hand"], 30)
        self.assertEqual(values["restaurant_stock_available"], 30)

        daily.action_refresh_movements()
        line.invalidate_recordset(["received_qty", "opening_qty"])
        self.assertEqual(line.opening_qty, 10)
        self.assertEqual(line.received_qty, 20)

    def test_dashboard_uses_previous_zero_closing_not_live_stock(self):
        today = fields.Date.context_today(
            self.env.user.with_context(tz=self.company.tz or "UTC")
        )
        self.env["stock.quant"]._update_available_quantity(
            self.product,
            self.branch_warehouse.lot_stock_id,
            4,
        )
        self._closed_daily(today - timedelta(days=1), 4, 0)
        self.env["stock.quant"]._update_available_quantity(
            self.product,
            self.branch_warehouse.lot_stock_id,
            5,
        )

        values = self._dashboard_values()

        self.assertEqual(values["restaurant_stock_opening"], 4)
        self.assertEqual(values["restaurant_stock_on_hand"], 5)

        daily = self._daily_sheet()
        values = self._dashboard_values()
        self.assertEqual(values["restaurant_stock_opening"], 4)

        daily.action_confirm_opening()
        values = self._dashboard_values()
        self.assertEqual(values["restaurant_stock_opening"], 0)
        self.assertEqual(values["restaurant_stock_on_hand"], 5)

    def test_dashboard_switches_received_on_first_receipt_of_new_day(self):
        self.company.tz = "Asia/Dubai"
        first_today = self._request(dispatched_qty=5, received_qty=5)
        first_today.action_confirm_branch_receipt()
        daily = self.env["restaurant.stock.daily"].search([
            ("branch_id", "=", self.branch.id),
            ("state", "=", "opened"),
        ])
        self.assertEqual(len(daily), 1)
        line = daily.line_ids.filtered(
            lambda item: item.product_id == self.product
        )
        values = self._dashboard_values()
        self.assertEqual(line.received_qty, 5)
        self.assertEqual(values["restaurant_stock_received_today"], 5)
        self.assertEqual(values["restaurant_stock_on_hand"], 5)

        second_today = self._request(dispatched_qty=3, received_qty=3)
        second_today.action_confirm_branch_receipt()
        line.invalidate_recordset(["received_qty"])
        values = self._dashboard_values()
        self.assertEqual(line.received_qty, 8)
        self.assertEqual(values["restaurant_stock_received_today"], 8)
        self.assertEqual(values["restaurant_stock_on_hand"], 8)

    def test_dashboard_consumption_waits_for_today_close_and_survives_receipts(self):
        self.company.tz = "Asia/Dubai"
        today = fields.Date.context_today(
            self.stockkeeper.with_context(tz=self.company.tz)
        )

        self.env["stock.quant"]._update_available_quantity(
            self.product,
            self.branch_warehouse.lot_stock_id,
            10,
        )
        yesterday = self._historical_daily(self.stockkeeper, {
            "branch_id": self.branch.id,
            "stock_date": today - timedelta(days=1),
            "line_ids": [Command.create({
                "product_id": self.product.id,
                "opening_qty": 10,
            })],
        }, confirm=True)
        yesterday_line = yesterday.line_ids.filtered(
            lambda line: line.product_id == self.product
        )
        yesterday_line.write({
            "consumption_qty": 4,
            "actual_closing_qty": 6,
            "actual_closing_state": "counted",
        })
        yesterday.action_submit_closing()
        yesterday.with_user(self.manager).action_close()

        values = self._dashboard_values()
        self.assertEqual(values["restaurant_stock_opening"], 10)
        self.assertEqual(values["restaurant_stock_consumption"], 4)

        daily = self._daily_sheet()
        values = self._dashboard_values()
        self.assertEqual(values["restaurant_stock_opening"], 10)
        self.assertEqual(values["restaurant_stock_consumption"], 4)

        daily.action_confirm_opening()
        values = self._dashboard_values()
        self.assertEqual(values["restaurant_stock_opening"], 6)
        self.assertEqual(values["restaurant_stock_consumption"], 0)

        first_today = self._request(dispatched_qty=5, received_qty=5)
        first_today.action_confirm_branch_receipt()
        today_line = daily.line_ids.filtered(
            lambda line: line.product_id == self.product
        )
        today_line.write({
            "consumption_qty": 7,
            "actual_closing_qty": 8,
            "actual_closing_state": "counted",
        })

        values = self._dashboard_values()
        self.assertEqual(values["restaurant_stock_received_today"], 5)
        self.assertEqual(values["restaurant_stock_consumption"], 0)

        daily.action_submit_closing()
        values = self._dashboard_values()
        self.assertEqual(daily.state, "closing_review")
        self.assertEqual(values["restaurant_stock_consumption"], 0)

        daily.with_user(self.manager).action_close()
        values = self._dashboard_values()
        self.assertEqual(values["restaurant_stock_consumption"], 7)

        second_today = self._request(dispatched_qty=3, received_qty=3)
        second_today.action_confirm_branch_receipt()
        values = self._dashboard_values()
        self.assertEqual(values["restaurant_stock_received_today"], 5)
        self.assertEqual(values["restaurant_stock_consumption"], 7)
        self.assertEqual(values["restaurant_stock_on_hand"], 7)
        self.assertEqual(values["restaurant_stock_available"], 7)

    def test_receipt_confirmation_opens_next_service_without_opening_button(self):
        self.company.tz = "Asia/Dubai"
        service_date = fields.Date.to_date("2026-10-05")
        next_service_date = fields.Date.to_date("2026-10-06")

        self.env["stock.quant"]._update_available_quantity(
            self.product,
            self.branch_warehouse.lot_stock_id,
            10,
        )
        service_sheet = self._historical_daily(self.stockkeeper, {
            "branch_id": self.branch.id,
            "stock_date": service_date,
            "line_ids": [Command.create({
                "product_id": self.product.id,
                "opening_qty": 10,
            })],
        }, confirm=True)
        service_line = service_sheet.line_ids
        service_line.write({
            "consumption_qty": 2,
            "actual_closing_qty": 7,
            "actual_closing_state": "counted",
            "request_tomorrow_qty": 3,
        })
        service_sheet.action_submit_closing()

        # 2026-10-05 21:00 UTC is 2026-10-06 01:00 in Dubai, but
        # the closed sheet remains the explicitly dated October 5 service.
        close_timestamp = datetime(2026, 10, 5, 21, 0, 0)
        with patch.object(fields.Datetime, "now", return_value=close_timestamp):
            service_sheet.with_user(self.manager).action_close()

        self.assertEqual(service_sheet.stock_date, service_date)
        self.assertEqual(service_sheet.state, "closed")
        self.assertEqual(service_line.consumption_qty, 2)
        self.assertEqual(service_line.actual_closing_qty, 7)
        self.assertEqual(
            self.env["stock.quant"]._get_available_quantity(
                self.product,
                self.branch_warehouse.lot_stock_id,
            ),
            7,
        )

        request = service_sheet.generated_request_id
        self.assertEqual(request.request_date, next_service_date)
        self.env["stock.quant"]._update_available_quantity(
            self.product,
            self.central_warehouse.lot_stock_id,
            3,
        )
        central_request = request.with_user(self.central_storekeeper)
        central_request.action_calculate_availability()
        central_request.action_prepare_transfer()
        central_request.action_dispatch()
        branch_request = request.with_user(self.stockkeeper)
        branch_request.line_ids.write({"branch_received_qty": 3})

        # The real receipt validation time controls the new service date.
        # No separate Daily Stock action_confirm_opening call is made here.
        receipt_timestamp = datetime(2026, 10, 6, 5, 0, 0)
        with patch.object(fields.Datetime, "now", return_value=receipt_timestamp):
            branch_request.action_confirm_branch_receipt()

        self.assertEqual(
            branch_request.receipt_picking_id.sudo().date_done,
            receipt_timestamp,
        )
        next_sheet = self.env["restaurant.stock.daily"].search([
            ("branch_id", "=", self.branch.id),
            ("stock_date", "=", next_service_date),
            ("state", "!=", "cancelled"),
        ])
        self.assertEqual(len(next_sheet), 1)
        self.assertEqual(next_sheet.state, "opened")
        next_line = next_sheet.line_ids.filtered(
            lambda line: line.product_id == self.product
        )
        self.assertEqual(next_line.opening_qty, 7)
        self.assertEqual(next_line.received_qty, 3)
        self.assertEqual(next_line.consumption_qty, 0)

        values = self._dashboard_values()
        self.assertEqual(values["restaurant_stock_opening"], 7)
        self.assertEqual(values["restaurant_stock_received_today"], 3)
        self.assertEqual(values["restaurant_stock_consumption"], 0)
        self.assertEqual(values["restaurant_stock_on_hand"], 10)
        self.assertEqual(values["restaurant_stock_available"], 10)

        manager_template = (
            self.product.product_tmpl_id
            .with_user(self.manager)
            .with_context(allowed_company_ids=[self.company.id])
        )
        dashboard_fields = [field for field in values if field != "id"]
        manager_template.invalidate_recordset(dashboard_fields)
        manager_values = manager_template.read(dashboard_fields)[0]
        self.assertEqual(
            {field: manager_values[field] for field in dashboard_fields},
            {field: values[field] for field in dashboard_fields},
        )

        # A later receipt on the same date cannot rewrite a confirmed close.
        next_line.write({
            "consumption_qty": 1,
            "actual_closing_qty": 10,
            "actual_closing_state": "counted",
        })
        next_sheet.action_submit_closing()
        next_sheet.with_user(self.manager).action_close()
        late_request = self._request(dispatched_qty=2, received_qty=2)
        late_timestamp = datetime(2026, 10, 6, 6, 0, 0)
        with patch.object(fields.Datetime, "now", return_value=late_timestamp):
            late_request.action_confirm_branch_receipt()

        service_line.invalidate_recordset()
        next_line.invalidate_recordset()
        self.assertEqual(service_sheet.state, "closed")
        self.assertEqual(service_line.consumption_qty, 2)
        self.assertEqual(service_line.actual_closing_qty, 7)
        self.assertEqual(next_sheet.state, "closed")
        self.assertEqual(next_line.received_qty, 3)
        self.assertEqual(next_line.consumption_qty, 1)
        values = self._dashboard_values()
        self.assertEqual(values["restaurant_stock_received_today"], 3)
        self.assertEqual(values["restaurant_stock_consumption"], 1)
        self.assertEqual(values["restaurant_stock_on_hand"], 11)
        self.assertEqual(values["restaurant_stock_available"], 11)

    def test_central_dashboard_does_not_mix_branch_daily_values(self):
        template = self.product.product_tmpl_id.with_user(
            self.central_branch_manager
        ).with_context(allowed_company_ids=[self.company.id])

        self.assertFalse(template._restaurant_dashboard_branch())
        self.assertEqual(
            template._restaurant_dashboard_location(),
            self.central_warehouse.lot_stock_id,
        )

    def test_central_consumption_uses_done_dispatches_by_branch_and_date(self):
        self.company.tz = "Asia/Dubai"
        dashboard_date = fields.Date.to_date("2026-10-08")
        date_probe = self.env["restaurant.stock.daily"].new({
            "branch_id": self.mirdif.id,
            "stock_date": dashboard_date,
        })
        utc_start, _utc_end = date_probe._get_stock_day_utc_range()

        mirdif_request = self._request(
            dispatched_qty=5,
            received_qty=5,
            branch=self.mirdif,
            stockkeeper=self.mirdif_stockkeeper,
        )
        jafilya_request = self._request(
            dispatched_qty=3,
            received_qty=3,
            branch=self.jafilya,
            stockkeeper=self.jafilya_stockkeeper,
        )
        old_jafilya_request = self._request(
            dispatched_qty=7,
            received_qty=7,
            branch=self.jafilya,
            stockkeeper=self.jafilya_stockkeeper,
        )

        self.env["stock.quant"]._update_available_quantity(
            self.product,
            self.central_warehouse.lot_stock_id,
            4,
        )
        distribution = self.env[
            "restaurant.stock.distribution"
        ].with_user(self.central_storekeeper).create({
            "branch_id": self.global_village.id,
            "line_ids": [Command.create({
                "product_id": self.product.id,
                "sent_qty": 4,
            })],
        })
        distribution.action_dispatch()

        mirdif_request.dispatch_picking_id.sudo().write({
            "date_done": utc_start + timedelta(hours=1),
        })
        jafilya_request.dispatch_picking_id.sudo().write({
            "date_done": utc_start + timedelta(hours=2),
        })
        distribution.dispatch_picking_id.sudo().write({
            "date_done": utc_start + timedelta(hours=3),
        })
        old_jafilya_request.dispatch_picking_id.sudo().write({
            "date_done": utc_start - timedelta(seconds=1),
        })

        dashboard_fields = [
            "restaurant_stock_consumption",
            "restaurant_stock_consumption_mirdif",
            "restaurant_stock_consumption_jafilya",
            "restaurant_stock_consumption_global_village",
            "restaurant_stock_waste",
            "restaurant_stock_damaged",
        ]
        central_template = (
            self.product.product_tmpl_id
            .with_user(self.central_storekeeper)
            .with_context(allowed_company_ids=[self.company.id])
        )
        with patch.object(
            stock_dashboard,
            "get_company_business_date",
            return_value=dashboard_date,
        ):
            central_template.invalidate_recordset(dashboard_fields)
            central_values = central_template.read(dashboard_fields)[0]

        self.assertEqual(central_values["restaurant_stock_consumption"], 12)
        self.assertEqual(
            central_values["restaurant_stock_consumption_mirdif"],
            5,
        )
        self.assertEqual(
            central_values["restaurant_stock_consumption_jafilya"],
            3,
        )
        self.assertEqual(
            central_values[
                "restaurant_stock_consumption_global_village"
            ],
            4,
        )
        self.assertEqual(central_values["restaurant_stock_waste"], 0)
        self.assertEqual(central_values["restaurant_stock_damaged"], 0)

        branch_template = (
            self.product.product_tmpl_id
            .with_user(self.mirdif_stockkeeper)
            .with_context(allowed_company_ids=[self.company.id])
        )
        branch_template.invalidate_recordset(dashboard_fields)
        branch_values = branch_template.read(dashboard_fields)[0]
        self.assertEqual(branch_values["restaurant_stock_consumption"], 0)
        self.assertEqual(
            branch_values["restaurant_stock_consumption_mirdif"],
            0,
        )

        view = self.env.ref(
            "restaurant_stock.restaurant_current_stock_view_list"
        )
        central_arch = self.env["product.template"].with_user(
            self.central_storekeeper
        ).get_view(view_id=view.id, view_type="list")["arch"]
        branch_arch = self.env["product.template"].with_user(
            self.mirdif_stockkeeper
        ).get_view(view_id=view.id, view_type="list")["arch"]
        manager_arch = self.env["product.template"].with_user(
            self.manager
        ).get_view(view_id=view.id, view_type="list")["arch"]
        self.assertIn("restaurant_stock_consumption_mirdif", central_arch)
        self.assertIn("restaurant_stock_consumption_jafilya", central_arch)
        self.assertIn(
            "restaurant_stock_consumption_global_village",
            central_arch,
        )
        self.assertNotIn("restaurant_stock_consumption_mirdif", branch_arch)
        self.assertIn("restaurant_stock_available", central_arch)
        self.assertNotIn("restaurant_stock_available", branch_arch)
        self.assertNotIn("restaurant_stock_available", manager_arch)

    def test_company_timezone_boundaries_ignore_viewer_timezone(self):
        self.company.tz = "Asia/Dubai"
        daily = self._daily_sheet()
        start_dt, end_dt = daily._get_stock_day_utc_range()
        cases = (
            (3, start_dt - timedelta(seconds=1)),
            (5, start_dt),
            (11, end_dt - timedelta(seconds=1)),
            (7, end_dt),
        )
        for quantity, completion_time in cases:
            request = self._request(
                dispatched_qty=quantity,
                received_qty=quantity,
            )
            request.action_confirm_branch_receipt()
            request.receipt_picking_id.sudo().write({
                "date_done": completion_time,
            })

        self.stockkeeper.sudo().tz = "Pacific/Honolulu"
        daily.with_user(self.stockkeeper).action_refresh_movements()
        line = daily.line_ids.filtered(lambda item: item.product_id == self.product)
        self.assertEqual(line.received_qty, 16)

        self.stockkeeper.sudo().tz = "Asia/Tokyo"
        daily.with_user(self.stockkeeper).action_refresh_movements()
        line.invalidate_recordset(["received_qty"])
        self.assertEqual(line.received_qty, 16)

    def test_daily_create_and_open_require_current_company_date(self):
        self.company.tz = "Asia/Dubai"
        current_date = fields.Date.to_date("2026-10-04")
        past_date = fields.Date.to_date("2026-10-03")
        future_date = fields.Date.to_date("2026-10-05")
        current_timestamp = datetime(2026, 10, 3, 21, 0, 0)
        Daily = self.env["restaurant.stock.daily"].with_user(
            self.stockkeeper
        )

        with patch.object(
            fields.Datetime,
            "now",
            return_value=current_timestamp,
        ):
            current = Daily.create({
                "branch_id": self.branch.id,
                "stock_date": current_date,
            })
            with self.assertRaisesRegex(
                ValidationError,
                "current company service date.*Selected date",
            ):
                Daily.create({
                    "branch_id": self.branch.id,
                    "stock_date": past_date,
                })
            with self.assertRaisesRegex(
                ValidationError,
                "current company service date.*Selected date",
            ):
                Daily.create({
                    "branch_id": self.branch.id,
                    "stock_date": future_date,
                })
            with self.assertRaisesRegex(
                ValidationError,
                "current company service date.*Selected date",
            ):
                current.write({"stock_date": future_date})

            current.action_confirm_opening()

        legacy_draft = (
            self.env["restaurant.stock.daily"]
            .with_user(self.stockkeeper)
            .sudo()
            .with_context(**{
                restaurant_stock_daily.ALLOW_NONCURRENT_DAILY_DATE_CONTEXT:
                    True,
            })
            .create({
                "branch_id": self.branch.id,
                "stock_date": past_date,
            })
            .sudo(False)
        )
        with patch.object(
            fields.Datetime,
            "now",
            return_value=current_timestamp,
        ):
            with self.assertRaisesRegex(
                ValidationError,
                "current company service date.*Selected date",
            ):
                legacy_draft.action_confirm_opening()

        form_arch = self.env.ref(
            "restaurant_stock.restaurant_stock_daily_view_form"
        ).arch_db
        normalized_arch = " ".join(form_arch.split())
        self.assertIn(
            'name="stock_date" readonly="1"',
            normalized_arch,
        )

    def test_opened_prior_day_sheet_can_close_after_midnight(self):
        self.company.tz = "Asia/Dubai"
        service_date = fields.Date.to_date("2026-10-03")
        before_midnight = datetime(2026, 10, 3, 19, 0, 0)
        after_midnight = datetime(2026, 10, 3, 21, 0, 0)

        self.env["stock.quant"]._update_available_quantity(
            self.product,
            self.branch_warehouse.lot_stock_id,
            10,
        )
        with patch.object(
            fields.Datetime,
            "now",
            return_value=before_midnight,
        ):
            daily = self.env["restaurant.stock.daily"].with_user(
                self.stockkeeper
            ).create({
                "branch_id": self.branch.id,
                "stock_date": service_date,
            })
            daily.action_confirm_opening()

        line = daily.line_ids.filtered(
            lambda item: item.product_id == self.product
        )
        with patch.object(
            fields.Datetime,
            "now",
            return_value=after_midnight,
        ):
            line.write({
                "consumption_qty": 5,
                "actual_closing_qty": 5,
                "actual_closing_state": "counted",
            })
            daily.action_submit_closing()
            daily.with_user(self.manager).action_close()

        self.assertEqual(daily.stock_date, service_date)
        self.assertEqual(daily.state, "closed")
        self.assertEqual(daily.reviewed_at, after_midnight)
        self.assertEqual(
            self.env["stock.quant"]._get_available_quantity(
                self.product,
                self.branch_warehouse.lot_stock_id,
            ),
            5,
        )

    def test_receipt_created_daily_uses_company_local_current_date(self):
        self.company.tz = "Asia/Dubai"
        receipt_timestamp = datetime(2026, 10, 3, 21, 0, 0)
        expected_date = fields.Date.to_date("2026-10-04")
        request = self._request(dispatched_qty=4, received_qty=4)

        with patch.object(
            fields.Datetime,
            "now",
            return_value=receipt_timestamp,
        ):
            request.action_confirm_branch_receipt()

        daily = self.env["restaurant.stock.daily"].search([
            ("branch_id", "=", self.branch.id),
            ("stock_date", "=", expected_date),
            ("state", "!=", "cancelled"),
        ])
        self.assertEqual(len(daily), 1)
        self.assertEqual(daily.state, "opened")
        self.assertEqual(daily.line_ids.received_qty, 4)

    def test_sync_isolates_branch_company_product_and_converts_uom(self):
        daily = self._daily_sheet()
        other_product = self.env["product.product"].create({
            "name": "Receipt UoM Isolation Product",
            "is_storable": True,
        })
        dozen = self.env.ref("uom.product_uom_dozen")

        self._native_receipt(
            other_product,
            self.branch_warehouse,
            2,
            uom=dozen,
        )
        self._native_receipt(
            self.product,
            self.other_warehouse,
            6,
        )

        other_company = self.env["res.company"].sudo().create({
            "name": "Receipt Isolation Other Company",
            "tz": self.company.tz or "UTC",
        })
        other_company_warehouse = self.env["stock.warehouse"].sudo().search([
            ("company_id", "=", other_company.id),
        ], limit=1)
        self._native_receipt(
            other_product,
            other_company_warehouse,
            3,
            uom=dozen,
        )

        daily.action_refresh_movements()

        other_line = daily.line_ids.filtered(
            lambda item: item.product_id == other_product
        )
        self.assertEqual(other_line.received_qty, 24)
        self.assertFalse(daily.line_ids.filtered(
            lambda item: item.product_id == self.product
        ))

    def test_closed_daily_is_frozen_after_late_receipt(self):
        today = fields.Date.context_today(
            self.env.user.with_context(tz=self.company.tz or "UTC")
        )
        daily = self._closed_daily(today, 0, 0)
        request = self._request(dispatched_qty=4, received_qty=4)
        message_count = len(daily.sudo().message_ids)

        request.action_confirm_branch_receipt()
        daily.action_refresh_movements()

        line = daily.line_ids.filtered(lambda item: item.product_id == self.product)
        line.invalidate_recordset(["received_qty"])
        self.assertEqual(daily.state, "closed")
        self.assertEqual(line.received_qty, 0)
        self.assertEqual(line.opening_qty, 0)
        self.assertEqual(line.current_on_hand_qty, 0)
        self.assertEqual(line.expected_closing_qty, 0)
        self.assertEqual(line.actual_closing_qty, 0)
        self.assertEqual(line.difference_qty, 0)
        self.assertEqual(len(daily.sudo().message_ids), message_count)
        with self.assertRaises(AccessError):
            line.write({"received_qty": 4})

    def test_closing_review_receipt_updates_derived_received_only(self):
        daily = self._daily_sheet()
        daily.action_confirm_opening()
        daily.line_ids.write({"actual_closing_state": "counted"})
        daily.action_submit_closing()
        request = self._request(dispatched_qty=6, received_qty=6)

        request.action_confirm_branch_receipt()

        line = daily.line_ids.filtered(lambda item: item.product_id == self.product)
        self.assertEqual(daily.state, "closing_review")
        self.assertEqual(line.received_qty, 6)
        self.assertEqual(line.opening_qty, 0)

    def test_hidden_daily_columns_keep_model_fields(self):
        view = self.env.ref(
            "restaurant_stock.restaurant_stock_daily_view_form"
        )
        arch = view.arch_db
        hidden_fields = (
            "incoming_transfer_qty",
            "outgoing_transfer_qty",
            "consumption_qty",
            "difference_qty",
        )
        line_model = self.env["restaurant.stock.daily.line"]
        for field_name in hidden_fields:
            self.assertIn(field_name, line_model._fields)
            self.assertIn(
                'name="%s"' % field_name,
                arch,
            )
        self.assertEqual(arch.count('column_invisible="1"'), 4)
        self.assertIn('name="expected_closing_qty" readonly="1"', arch)
        self.assertIn('name="actual_closing_state"', arch)

    def test_current_stock_view_shows_on_hand_without_opening(self):
        arch = self.env.ref(
            "restaurant_stock.restaurant_current_stock_view_list"
        ).arch_db
        normalized_arch = " ".join(arch.split())

        self.assertNotIn('name="restaurant_stock_opening"', arch)
        self.assertIn(
            'name="restaurant_stock_on_hand" string="On Hand"',
            normalized_arch,
        )
        self.assertLess(
            normalized_arch.index('name="restaurant_stock_on_hand"'),
            normalized_arch.index('name="restaurant_stock_received_today"'),
        )

    def test_branch_requests_menu_reuses_direct_request_workflow(self):
        action = self.env.ref(
            "restaurant_stock.restaurant_stock_branch_request_action"
        )
        menu = self.env.ref(
            "restaurant_stock.restaurant_stock_branch_request_menu"
        )
        incoming_action = self.env.ref(
            "restaurant_stock.restaurant_stock_incoming_action"
        )
        incoming_menu = self.env.ref(
            "restaurant_stock.restaurant_stock_incoming_menu"
        )

        self.assertEqual(action.name, "Requests")
        self.assertEqual(action.res_model, "restaurant.stock.request")
        self.assertEqual(action.view_mode, "list,form")
        self.assertEqual(
            action.views,
            [
                [
                    self.env.ref(
                        "restaurant_stock.restaurant_stock_branch_request_view_list"
                    ).id,
                    "list",
                ],
                [
                    self.env.ref(
                        "restaurant_stock.restaurant_stock_branch_request_view_form"
                    ).id,
                    "form",
                ],
            ],
        )
        self.assertEqual(menu.action, action)
        self.assertIn(
            self.env.ref("restaurant_core.group_restaurant_stockkeeper"),
            menu.group_ids,
        )
        self.assertIn(
            self.env.ref("restaurant_core.group_restaurant_branch_manager"),
            menu.group_ids,
        )
        self.assertEqual(incoming_action.name, "Incoming Deliveries")
        self.assertEqual(
            incoming_action.res_model,
            "restaurant.incoming.delivery",
        )
        self.assertEqual(incoming_menu.name, "Incoming Deliveries")

        list_arch = self.env.ref(
            "restaurant_stock.restaurant_stock_branch_request_view_list"
        ).arch_db
        form_arch = self.env.ref(
            "restaurant_stock.restaurant_stock_branch_request_view_form"
        ).arch_db
        self.assertNotIn('create="0"', list_arch)
        self.assertNotIn('create="0"', form_arch)
        self.assertIn('name="current_on_hand_qty"', form_arch)
        self.assertIn('name="requested_qty"', form_arch)
        self.assertTrue(
            self.env["restaurant.stock.request"]
            .with_user(self.stockkeeper)
            .has_access("create")
        )
        self.assertTrue(
            self.env["restaurant.stock.request"]
            .with_user(self.manager)
            .has_access("create")
        )

        manager_visible = self.env["ir.ui.menu"].with_user(
            self.manager
        )._visible_menu_ids()
        self.assertIn(menu.id, manager_visible)

        request = self._request(
            dispatched_qty=4,
            received_qty=4,
            state="draft",
        )
        self.assertFalse(request.daily_stock_id)
        request.action_submit()
        self.assertEqual(request.state, "submitted")

    def test_daily_distribution_menu_is_hidden_from_branch_roles(self):
        distribution_menu = self.env.ref(
            "restaurant_stock.restaurant_stock_distribution_menu"
        )
        incoming_menu = self.env.ref(
            "restaurant_stock.restaurant_stock_incoming_menu"
        )
        distribution_action = self.env.ref(
            "restaurant_stock.restaurant_stock_distribution_action"
        )

        expected_groups = self.env["res.groups"].browse([
            self.env.ref(
                "restaurant_core.group_restaurant_operations_manager"
            ).id,
            self.env.ref(
                "restaurant_core.group_restaurant_owner"
            ).id,
        ])
        self.assertEqual(distribution_menu.group_ids, expected_groups)
        self.assertEqual(
            distribution_action.res_model,
            "restaurant.stock.distribution",
        )

        stockkeeper_visible = self.env["ir.ui.menu"].with_user(
            self.stockkeeper
        )._visible_menu_ids()
        central_visible = self.env["ir.ui.menu"].with_user(
            self.central_storekeeper
        )._visible_menu_ids()
        manager_visible = self.env["ir.ui.menu"].with_user(
            self.manager
        )._visible_menu_ids()

        self.assertNotIn(distribution_menu.id, stockkeeper_visible)
        self.assertNotIn(distribution_menu.id, central_visible)
        self.assertNotIn(distribution_menu.id, manager_visible)
        self.assertIn(incoming_menu.id, stockkeeper_visible)
        self.assertIn(incoming_menu.id, manager_visible)

    def test_manager_can_request_and_receive_for_assigned_branch_only(self):
        self.env["stock.quant"]._update_available_quantity(
            self.product,
            self.branch_warehouse.lot_stock_id,
            6,
        )
        ManagerRequest = self.env["restaurant.stock.request"].with_user(
            self.manager
        )
        request = ManagerRequest.create({
            "branch_id": self.branch.id,
            "line_ids": [Command.create({
                "product_id": self.product.id,
                "requested_qty": 4,
            })],
        })
        self.assertEqual(request.requested_by_id, self.manager)
        self.assertEqual(request.line_ids.current_on_hand_qty, 6)
        request.action_submit()
        self.assertEqual(request.state, "submitted")

        with self.assertRaises(AccessError):
            ManagerRequest.create({
                "branch_id": self.other_branch.id,
                "line_ids": [Command.create({
                    "product_id": self.product.id,
                    "requested_qty": 1,
                })],
            })

        self.env["stock.quant"]._update_available_quantity(
            self.product,
            self.central_warehouse.lot_stock_id,
            4,
        )
        central_request = request.with_user(self.central_storekeeper)
        central_request.action_calculate_availability()
        central_request.action_prepare_transfer()
        central_request.action_dispatch()

        manager_request = request.with_user(self.manager)
        manager_request.line_ids.write({"branch_received_qty": 4})
        manager_request.action_confirm_branch_receipt()
        self.assertEqual(manager_request.state, "received")
        self.assertEqual(manager_request.branch_received_by_id, self.manager)
        self.assertEqual(manager_request.receipt_picking_id.state, "done")

    def test_integrated_request_receipt_close_and_next_day_flow(self):
        self.company.tz = "Asia/Dubai"
        service_date = fields.Date.to_date("2026-10-04")
        next_date = fields.Date.to_date("2026-10-05")
        service_timestamp = datetime(2026, 10, 4, 10, 0, 0)
        overnight_close = datetime(2026, 10, 4, 21, 0, 0)
        next_receipt_timestamp = datetime(2026, 10, 4, 22, 0, 0)

        self.env["stock.quant"]._update_available_quantity(
            self.product,
            self.branch_warehouse.lot_stock_id,
            6,
        )
        self.env["stock.quant"]._update_available_quantity(
            self.product,
            self.central_warehouse.lot_stock_id,
            10,
        )

        with patch.object(
            fields.Datetime,
            "now",
            return_value=service_timestamp,
        ):
            request = self.env["restaurant.stock.request"].with_user(
                self.manager
            ).create({
                "branch_id": self.branch.id,
                "line_ids": [Command.create({
                    "product_id": self.product.id,
                    "requested_qty": 4,
                })],
            })
            self.assertEqual(request.line_ids.current_on_hand_qty, 6)
            self.assertEqual(
                self.env["stock.quant"]._get_available_quantity(
                    self.product,
                    self.branch_warehouse.lot_stock_id,
                ),
                6,
            )

            request.action_submit()
            central_request = request.with_user(self.central_storekeeper)
            central_request.action_calculate_availability()
            central_request.action_prepare_transfer()
            central_request.action_dispatch()
            self.assertEqual(
                self.env["stock.quant"]._get_available_quantity(
                    self.product,
                    self.branch_warehouse.lot_stock_id,
                ),
                6,
            )

            incoming_domain = [("request_id", "=", request.id)]
            self.assertEqual(
                self.env["restaurant.incoming.delivery"].with_user(
                    self.stockkeeper
                ).search_count(incoming_domain),
                1,
            )
            self.assertEqual(
                self.env["restaurant.incoming.delivery"].with_user(
                    self.manager
                ).search_count(incoming_domain),
                1,
            )

            manager_request = request.with_user(self.manager)
            manager_request.line_ids.write({"branch_received_qty": 4})
            manager_request.action_confirm_branch_receipt()

        daily = self.env["restaurant.stock.daily"].search([
            ("branch_id", "=", self.branch.id),
            ("stock_date", "=", service_date),
            ("state", "!=", "cancelled"),
        ])
        self.assertEqual(len(daily), 1)
        line = daily.line_ids.filtered(
            lambda item: item.product_id == self.product
        )
        self.assertEqual(daily.state, "opened")
        self.assertEqual(line.opening_qty, 6)
        self.assertEqual(line.received_qty, 4)
        self.assertEqual(line.current_on_hand_qty, 10)

        dashboard_fields = [
            "restaurant_stock_on_hand",
            "restaurant_stock_received_today",
            "restaurant_stock_consumption",
        ]
        for user in (self.stockkeeper, self.manager):
            template = self.product.product_tmpl_id.with_user(user)
            template.invalidate_recordset(dashboard_fields)
            values = template.read(dashboard_fields)[0]
            self.assertEqual(values["restaurant_stock_on_hand"], 10)
            self.assertEqual(values["restaurant_stock_received_today"], 4)
            self.assertEqual(values["restaurant_stock_consumption"], 0)

        line.with_user(self.stockkeeper).write({
            "consumption_qty": 7,
            "actual_closing_qty": 3,
            "actual_closing_state": "counted",
            "request_tomorrow_qty": 5,
        })
        daily.with_user(self.stockkeeper).action_submit_closing()
        self.assertFalse(line.consumption_move_id)

        line.with_user(self.manager).write({
            "consumption_qty": 8,
            "actual_closing_qty": 2,
            "actual_closing_state": "counted",
            "request_tomorrow_qty": 3,
            "note": "Integrated manager review",
        })
        with patch.object(
            fields.Datetime,
            "now",
            return_value=overnight_close,
        ):
            daily.with_user(self.manager).action_close()

        self.assertEqual(daily.stock_date, service_date)
        self.assertEqual(daily.state, "closed")
        self.assertEqual(daily.reviewed_at, overnight_close)
        self.assertEqual(line.received_qty, 4)
        self.assertEqual(line.consumption_move_id.state, "done")
        self.assertEqual(line.consumption_move_id.quantity, 8)
        self.assertEqual(
            self.env["stock.quant"]._get_available_quantity(
                self.product,
                self.branch_warehouse.lot_stock_id,
            ),
            2,
        )
        generated_request = daily.generated_request_id
        self.assertEqual(generated_request.line_ids.requested_qty, 3)

        for user in (self.stockkeeper, self.manager):
            template = self.product.product_tmpl_id.with_user(user)
            template.invalidate_recordset(dashboard_fields)
            values = template.read(dashboard_fields)[0]
            self.assertEqual(values["restaurant_stock_on_hand"], 2)
            self.assertEqual(values["restaurant_stock_received_today"], 4)
            self.assertEqual(values["restaurant_stock_consumption"], 8)

        with patch.object(
            fields.Datetime,
            "now",
            return_value=next_receipt_timestamp,
        ):
            central_next = generated_request.with_user(
                self.central_storekeeper
            )
            central_next.action_calculate_availability()
            central_next.action_prepare_transfer()
            central_next.action_dispatch()
            manager_next = generated_request.with_user(self.manager)
            manager_next.line_ids.write({"branch_received_qty": 3})
            manager_next.action_confirm_branch_receipt()
            with self.assertRaisesRegex(UserError, "already been received"):
                manager_next.action_confirm_branch_receipt()

            next_daily = self.env["restaurant.stock.daily"].search([
                ("branch_id", "=", self.branch.id),
                ("stock_date", "=", next_date),
                ("state", "!=", "cancelled"),
            ])
            self.assertEqual(len(next_daily), 1)
            next_daily.with_user(self.stockkeeper).action_refresh_movements()
            next_daily.with_user(self.stockkeeper).action_refresh_movements()

            defaults = self.env["restaurant.stock.daily"].with_user(
                self.stockkeeper
            ).default_get(["branch_id", "stock_date", "line_ids"])

        next_line = next_daily.line_ids.filtered(
            lambda item: item.product_id == self.product
        )
        self.assertEqual(next_daily.state, "opened")
        self.assertEqual(next_line.opening_qty, 2)
        self.assertEqual(next_line.received_qty, 3)
        self.assertEqual(next_line.current_on_hand_qty, 5)
        self.assertEqual(next_line.consumption_qty, 0)
        self.assertEqual(daily.line_ids.received_qty, 4)
        self.assertEqual(defaults["stock_date"], next_date)
        self.assertEqual(defaults["branch_id"], self.branch.id)
        self.assertEqual(defaults["line_ids"], [])

        for user in (self.stockkeeper, self.manager):
            template = self.product.product_tmpl_id.with_user(user)
            template.invalidate_recordset(dashboard_fields)
            values = template.read(dashboard_fields)[0]
            self.assertEqual(values["restaurant_stock_on_hand"], 5)
            self.assertEqual(values["restaurant_stock_received_today"], 3)
            self.assertEqual(values["restaurant_stock_consumption"], 0)

        report = self.env[
            "restaurant.stock.central.daily.report"
        ].with_user(self.central_storekeeper).create({
            "service_date": service_date,
        })
        day_one_detail = report.detail_line_ids.filtered(
            lambda item: item.daily_id == daily
            and item.product_id == self.product
        )
        day_one_total = report.product_total_ids.filtered(
            lambda item: item.product_id == self.product
        )
        self.assertEqual(len(day_one_detail), 1)
        self.assertEqual(day_one_detail.opening_qty, 6)
        self.assertEqual(day_one_detail.received_qty, 4)
        self.assertEqual(day_one_detail.current_on_hand_qty, 10)
        self.assertEqual(day_one_detail.consumption_qty, 8)
        self.assertEqual(day_one_detail.actual_closing_qty, 2)
        self.assertEqual(day_one_total.opening_qty, 6)
        self.assertEqual(day_one_total.received_qty, 4)
        self.assertEqual(day_one_total.consumption_qty, 8)

        report.write({"service_date": next_date})
        report.invalidate_recordset(["detail_line_ids", "product_total_ids"])
        day_two_detail = report.detail_line_ids.filtered(
            lambda item: item.daily_id == next_daily
            and item.product_id == self.product
        )
        day_two_total = report.product_total_ids.filtered(
            lambda item: item.product_id == self.product
        )
        self.assertEqual(len(day_two_detail), 1)
        self.assertEqual(day_two_detail.opening_qty, 2)
        self.assertEqual(day_two_detail.received_qty, 3)
        self.assertEqual(day_two_detail.current_on_hand_qty, 5)
        self.assertEqual(day_two_detail.consumption_qty, 0)
        self.assertEqual(day_two_total.opening_qty, 2)
        self.assertEqual(day_two_total.received_qty, 3)
        self.assertEqual(day_two_total.consumption_qty, 0)

    def test_central_branch_daily_stock_is_read_only_and_company_isolated(self):
        today = fields.Date.context_today(
            self.env.user.with_context(tz=self.company.tz or "UTC")
        )
        own_date = today - timedelta(days=30)
        other_branch_date = today - timedelta(days=31)

        own_daily = self._historical_daily(self.stockkeeper, {
            "branch_id": self.branch.id,
            "stock_date": own_date,
            "line_ids": [Command.create({
                "product_id": self.product.id,
                "opening_qty": 12,
                "consumption_qty": 3,
                "waste_qty": 1,
                "damaged_qty": 2,
                "actual_closing_qty": 6,
                "actual_closing_state": "counted",
                "request_tomorrow_qty": 4,
                "note": "Central read-only view test",
            })],
        })
        other_branch_daily = self._historical_daily(
            self.wrong_branch_stockkeeper,
            {
            "branch_id": self.other_branch.id,
            "stock_date": other_branch_date,
            "line_ids": [Command.create({
                "product_id": self.product.id,
                "opening_qty": 8,
            })],
            },
        )

        central_lines = self.env["restaurant.stock.daily.line"].with_user(
            self.central_storekeeper
        ).search([
            ("daily_id", "in", [own_daily.id, other_branch_daily.id]),
        ])
        self.assertEqual(
            set(central_lines.daily_id.ids),
            {own_daily.id, other_branch_daily.id},
        )

        branch_lines = self.env["restaurant.stock.daily.line"].with_user(
            self.stockkeeper
        ).search([
            ("daily_id", "in", [own_daily.id, other_branch_daily.id]),
        ])
        self.assertEqual(branch_lines.daily_id, own_daily)

        CentralLine = self.env["restaurant.stock.daily.line"].with_user(
            self.central_storekeeper
        )
        self.assertTrue(CentralLine.has_access("read"))
        self.assertFalse(CentralLine.has_access("create"))
        self.assertFalse(CentralLine.has_access("write"))
        self.assertFalse(CentralLine.has_access("unlink"))

        other_company = self.env["res.company"].create({
            "name": "Branch Daily Stock Isolation Company",
        })
        other_warehouse = self.env["stock.warehouse"].with_company(
            other_company
        ).create({
            "name": "Branch Daily Stock Isolation Warehouse",
            "code": "BDS",
            "company_id": other_company.id,
        })
        isolated_branch = self.env["restaurant.branch"].create({
            "name": "Branch Daily Stock Isolated Branch",
            "code": "BDS",
            "company_id": other_company.id,
            "warehouse_id": other_warehouse.id,
        })
        isolated_stockkeeper = new_test_user(
            self.env,
            login="branch_daily_stock_isolated_stockkeeper",
            groups="restaurant_core.group_restaurant_stockkeeper",
            company_id=other_company.id,
            company_ids=[Command.set(other_company.ids)],
            restaurant_branch_ids=[Command.set(isolated_branch.ids)],
        )
        isolated_daily = self._historical_daily(isolated_stockkeeper, {
            "branch_id": isolated_branch.id,
            "stock_date": own_date,
            "line_ids": [Command.create({
                "product_id": self.product.id,
                "opening_qty": 5,
            })],
        })
        self.assertFalse(
            self.env["restaurant.stock.daily.line"].with_user(
                self.central_storekeeper
            ).search_count([("daily_id", "=", isolated_daily.id)])
        )

    def test_central_branch_daily_stock_view_and_menu(self):
        action = self.env.ref(
            "restaurant_stock.restaurant_branch_daily_stock_action"
        )
        report_action = self.env.ref(
            "restaurant_stock.restaurant_central_daily_report_server_action"
        )
        report_view = self.env.ref(
            "restaurant_stock.restaurant_central_daily_report_view_form"
        )
        menu = self.env.ref(
            "restaurant_stock.restaurant_branch_daily_stock_menu"
        )
        list_view = self.env.ref(
            "restaurant_stock.restaurant_branch_daily_stock_line_view_list"
        )
        search_view = self.env.ref(
            "restaurant_stock.restaurant_branch_daily_stock_line_view_search"
        )
        central_group = self.env.ref(
            "restaurant_core.group_restaurant_central_storekeeper"
        )

        self.assertEqual(action.res_model, "restaurant.stock.daily.line")
        self.assertEqual(action.view_mode, "list")
        self.assertIn("'search_default_group_branch': 1", action.context)
        self.assertNotIn("search_default_group_date", action.context)
        self.assertEqual(menu.action, report_action)
        self.assertEqual(
            report_action.model_id.model,
            "restaurant.stock.central.daily.report",
        )
        self.assertEqual(menu.group_ids, central_group)

        normalized_report_arch = " ".join(report_view.arch_db.split())
        self.assertIn('name="service_date"', normalized_report_arch)
        self.assertIn('name="detail_line_ids"', normalized_report_arch)
        self.assertIn('name="product_total_ids"', normalized_report_arch)
        self.assertLess(
            normalized_report_arch.index('name="service_date"'),
            normalized_report_arch.index('name="detail_line_ids"'),
        )
        self.assertLess(
            normalized_report_arch.index('name="service_date"'),
            normalized_report_arch.index('name="product_total_ids"'),
        )
        self.assertIn('string="Branch Details"', normalized_report_arch)
        self.assertIn('string="Per-Product Totals"', normalized_report_arch)

        for attribute in ('create="0"', 'edit="0"', 'delete="0"'):
            self.assertIn(attribute, list_view.arch_db)
        self.assertIn(
            'default_order="branch_id, stock_date desc, product_id"',
            list_view.arch_db,
        )
        self.assertIn('expand="1"', list_view.arch_db)
        for field_name in (
            "branch_id",
            "stock_date",
            "daily_id",
            "daily_state",
            "product_id",
            "uom_id",
            "opening_qty",
            "received_qty",
            "current_on_hand_qty",
            "consumption_qty",
            "waste_qty",
            "damaged_qty",
            "expected_closing_qty",
            "actual_closing_state",
            "actual_closing_qty",
            "request_tomorrow_qty",
            "difference_qty",
            "note",
        ):
            self.assertIn(f'name="{field_name}"', list_view.arch_db)
        self.assertNotIn("<button", list_view.arch_db)
        self.assertIn('name="stock_date_filter"', search_view.arch_db)
        self.assertIn('name="group_branch"', search_view.arch_db)
        self.assertIn('name="group_date"', search_view.arch_db)

        central_visible = self.env["ir.ui.menu"].with_user(
            self.central_storekeeper
        )._visible_menu_ids()
        stockkeeper_visible = self.env["ir.ui.menu"].with_user(
            self.stockkeeper
        )._visible_menu_ids()
        manager_visible = self.env["ir.ui.menu"].with_user(
            self.manager
        )._visible_menu_ids()
        self.assertIn(menu.id, central_visible)
        self.assertNotIn(menu.id, stockkeeper_visible)
        self.assertNotIn(menu.id, manager_visible)

        Report = self.env[
            "restaurant.stock.central.daily.report"
        ].with_user(self.central_storekeeper)
        self.assertTrue(Report.has_access("read"))
        self.assertTrue(Report.has_access("create"))
        self.assertTrue(Report.has_access("write"))
        self.assertFalse(
            self.env["restaurant.stock.central.daily.report"].with_user(
                self.stockkeeper
            ).has_access("read")
        )

    def test_central_branch_daily_stock_keeps_product_quantities_separate(self):
        lamb = self.env["product.product"].create({
            "name": "Branch Daily Stock Lamb",
            "is_storable": True,
        })
        stock_date = fields.Date.context_today(
            self.stockkeeper.with_context(tz=self.company.tz or "UTC")
        ) - timedelta(days=60)
        daily = self._historical_daily(self.stockkeeper, {
            "branch_id": self.branch.id,
            "stock_date": stock_date,
            "line_ids": [
                Command.create({
                    "product_id": self.product.id,
                    "opening_qty": 10,
                }),
                Command.create({
                    "product_id": lamb.id,
                    "opening_qty": 20,
                }),
            ],
        })

        lines = self.env["restaurant.stock.daily.line"].with_user(
            self.central_storekeeper
        ).search([("daily_id", "=", daily.id)])
        self.assertEqual(len(lines), 2)
        self.assertEqual(
            {
                line.product_id.display_name: line.opening_qty
                for line in lines
            },
            {
                self.product.display_name: 10,
                lamb.display_name: 20,
            },
        )
        self.assertNotIn(30, lines.mapped("opening_qty"))

        for field_name in (
            "opening_qty",
            "received_qty",
            "current_on_hand_qty",
            "consumption_qty",
            "waste_qty",
            "damaged_qty",
            "expected_closing_qty",
            "actual_closing_qty",
            "request_tomorrow_qty",
            "difference_qty",
        ):
            self.assertIsNone(
                self.env["restaurant.stock.daily.line"]
                ._fields[field_name]
                .aggregator
            )

    def test_central_report_selected_date_isolates_details_and_totals(self):
        today = fields.Date.context_today(
            self.stockkeeper.with_context(tz=self.company.tz or "UTC")
        )
        selected_date = today - timedelta(days=95)
        historical_date = selected_date - timedelta(days=1)

        selected_own = self._historical_daily(self.stockkeeper, {
            "branch_id": self.branch.id,
            "stock_date": selected_date,
            "line_ids": [Command.create({
                "product_id": self.product.id,
                "opening_qty": 10,
            })],
        })
        selected_other = self._historical_daily(
            self.wrong_branch_stockkeeper,
            {
            "branch_id": self.other_branch.id,
            "stock_date": selected_date,
            "line_ids": [Command.create({
                "product_id": self.product.id,
                "opening_qty": 20,
            })],
            },
        )
        historical = self._historical_daily(self.stockkeeper, {
            "branch_id": self.branch.id,
            "stock_date": historical_date,
            "line_ids": [Command.create({
                "product_id": self.product.id,
                "opening_qty": 99,
            })],
        })
        self.env.flush_all()

        Report = self.env[
            "restaurant.stock.central.daily.report"
        ].with_user(self.central_storekeeper)
        report = Report.create({"service_date": selected_date})

        self.assertEqual(
            set(report.detail_line_ids.daily_id.ids),
            {selected_own.id, selected_other.id},
        )
        self.assertEqual(
            set(report.detail_line_ids.mapped("stock_date")),
            {selected_date},
        )
        selected_total = report.product_total_ids.filtered(
            lambda total: total.product_id == self.product
        )
        self.assertEqual(selected_total.stock_date, selected_date)
        self.assertEqual(selected_total.opening_qty, 30)

        report.write({"service_date": historical_date})
        report.invalidate_recordset([
            "detail_line_ids",
            "product_total_ids",
        ])
        self.assertEqual(report.detail_line_ids.daily_id, historical)
        self.assertEqual(
            set(report.detail_line_ids.mapped("stock_date")),
            {historical_date},
        )
        historical_total = report.product_total_ids.filtered(
            lambda total: total.product_id == self.product
        )
        self.assertEqual(historical_total.stock_date, historical_date)
        self.assertEqual(historical_total.opening_qty, 99)

        self.company.tz = "Asia/Dubai"
        with patch.object(
            fields.Datetime,
            "now",
            return_value=datetime(2026, 10, 3, 21, 0, 0),
        ):
            open_action = Report.action_open_report()
        default_report = Report.browse(open_action["res_id"])
        self.assertEqual(
            default_report.service_date,
            fields.Date.to_date("2026-10-04"),
        )
        self.assertEqual(
            open_action["res_model"],
            "restaurant.stock.central.daily.report",
        )
        self.assertEqual(open_action["view_mode"], "form")

    def test_daily_product_totals_sum_same_product_only_by_date_and_company(self):
        lamb = self.env["product.product"].create({
            "name": "Daily Product Total Lamb",
            "is_storable": True,
        })
        stock_date = fields.Date.context_today(
            self.stockkeeper.with_context(tz=self.company.tz or "UTC")
        ) - timedelta(days=80)
        other_date = stock_date - timedelta(days=1)

        self._historical_daily(self.stockkeeper, {
            "branch_id": self.branch.id,
            "stock_date": stock_date,
            "line_ids": [
                Command.create({
                    "product_id": self.product.id,
                    "opening_qty": 10,
                }),
                Command.create({
                    "product_id": lamb.id,
                    "opening_qty": 5,
                }),
            ],
        })
        self._historical_daily(self.wrong_branch_stockkeeper, {
            "branch_id": self.other_branch.id,
            "stock_date": stock_date,
            "line_ids": [
                Command.create({
                    "product_id": self.product.id,
                    "opening_qty": 20,
                }),
                Command.create({
                    "product_id": lamb.id,
                    "opening_qty": 15,
                }),
            ],
        })
        self._historical_daily(self.stockkeeper, {
            "branch_id": self.branch.id,
            "stock_date": other_date,
            "line_ids": [Command.create({
                "product_id": self.product.id,
                "opening_qty": 99,
            })],
        })
        self.env.flush_all()

        Total = self.env["restaurant.stock.daily.product.total"].with_user(
            self.central_storekeeper
        )
        totals = Total.search([
            ("stock_date", "=", stock_date),
            ("product_id", "in", [self.product.id, lamb.id]),
        ])
        self.assertEqual(len(totals), 2)
        chicken_total = totals.filtered(
            lambda total: total.product_id == self.product
        )
        lamb_total = totals.filtered(
            lambda total: total.product_id == lamb
        )
        self.assertEqual(chicken_total.opening_qty, 30)
        self.assertEqual(chicken_total.current_on_hand_qty, 30)
        self.assertEqual(chicken_total.branch_count, 2)
        self.assertEqual(lamb_total.opening_qty, 20)
        self.assertEqual(lamb_total.current_on_hand_qty, 20)
        self.assertEqual(lamb_total.branch_count, 2)
        self.assertNotEqual(chicken_total.opening_qty, 50)
        self.assertEqual(
            Total.search([
                ("stock_date", "=", other_date),
                ("product_id", "=", self.product.id),
            ]).opening_qty,
            99,
        )

        other_company = self.env["res.company"].create({
            "name": "Daily Product Total Isolation Company",
        })
        other_warehouse = self.env["stock.warehouse"].with_company(
            other_company
        ).create({
            "name": "Daily Product Total Isolation Warehouse",
            "code": "DPT",
            "company_id": other_company.id,
        })
        isolated_branch = self.env["restaurant.branch"].create({
            "name": "Daily Product Total Isolated Branch",
            "code": "DPT",
            "company_id": other_company.id,
            "warehouse_id": other_warehouse.id,
        })
        isolated_stockkeeper = new_test_user(
            self.env,
            login="daily_product_total_isolated_stockkeeper",
            groups="restaurant_core.group_restaurant_stockkeeper",
            company_id=other_company.id,
            company_ids=[Command.set(other_company.ids)],
            restaurant_branch_ids=[Command.set(isolated_branch.ids)],
        )
        self._historical_daily(isolated_stockkeeper, {
            "branch_id": isolated_branch.id,
            "stock_date": stock_date,
            "line_ids": [Command.create({
                "product_id": self.product.id,
                "opening_qty": 40,
            })],
        })
        self.env.flush_all()

        self.assertEqual(
            Total.search([
                ("stock_date", "=", stock_date),
                ("product_id", "=", self.product.id),
            ]).opening_qty,
            30,
        )
        self.assertEqual(
            set(
                self.env["restaurant.stock.daily.product.total"]
                .sudo()
                .search([
                    ("stock_date", "=", stock_date),
                    ("product_id", "=", self.product.id),
                ])
                .mapped("company_id")
                .ids
            ),
            {self.company.id, other_company.id},
        )

    def test_daily_product_totals_are_read_only_and_menu_is_hidden(self):
        action = self.env.ref(
            "restaurant_stock.restaurant_daily_product_total_action"
        )
        menu = self.env.ref(
            "restaurant_stock.restaurant_daily_product_total_menu"
        )
        list_view = self.env.ref(
            "restaurant_stock.restaurant_daily_product_total_view_list"
        )
        search_view = self.env.ref(
            "restaurant_stock.restaurant_daily_product_total_view_search"
        )
        central_group = self.env.ref(
            "restaurant_core.group_restaurant_central_storekeeper"
        )

        self.assertEqual(
            action.res_model,
            "restaurant.stock.daily.product.total",
        )
        self.assertEqual(action.view_mode, "list")
        self.assertEqual(menu.action, action)
        self.assertEqual(menu.group_ids, central_group)
        self.assertFalse(menu.active)
        for attribute in ('create="0"', 'edit="0"', 'delete="0"'):
            self.assertIn(attribute, list_view.arch_db)
        for field_name in (
            "stock_date",
            "product_id",
            "uom_id",
            "included_statuses",
            "branch_count",
            "counted_line_count",
            "uncounted_line_count",
            "opening_qty",
            "received_qty",
            "current_on_hand_qty",
            "consumption_qty",
            "waste_qty",
            "damaged_qty",
            "expected_closing_qty",
            "actual_closing_qty",
            "request_tomorrow_qty",
            "difference_qty",
        ):
            self.assertIn(f'name="{field_name}"', list_view.arch_db)
        self.assertIn('name="stock_date_filter"', search_view.arch_db)

        Total = self.env["restaurant.stock.daily.product.total"].with_user(
            self.central_storekeeper
        )
        self.assertTrue(Total.has_access("read"))
        self.assertFalse(Total.has_access("create"))
        self.assertFalse(Total.has_access("write"))
        self.assertFalse(Total.has_access("unlink"))
        for field_name in (
            "opening_qty",
            "received_qty",
            "current_on_hand_qty",
            "consumption_qty",
            "waste_qty",
            "damaged_qty",
            "expected_closing_qty",
            "actual_closing_qty",
            "request_tomorrow_qty",
            "difference_qty",
        ):
            self.assertIsNone(Total._fields[field_name].aggregator)

        central_visible = self.env["ir.ui.menu"].with_user(
            self.central_storekeeper
        )._visible_menu_ids()
        stockkeeper_visible = self.env["ir.ui.menu"].with_user(
            self.stockkeeper
        )._visible_menu_ids()
        self.assertNotIn(menu.id, central_visible)
        self.assertNotIn(menu.id, stockkeeper_visible)

    def test_unified_incoming_deliveries_routes_both_origins_and_isolates_branch(self):
        request = self._request(state="draft")
        own_distribution = self.env[
            "restaurant.stock.distribution"
        ].with_user(self.central_storekeeper).create({
            "branch_id": self.branch.id,
        })
        other_distribution = self.env[
            "restaurant.stock.distribution"
        ].with_user(self.central_storekeeper).create({
            "branch_id": self.other_branch.id,
        })
        self.env.flush_all()

        Incoming = self.env["restaurant.incoming.delivery"].with_user(
            self.stockkeeper
        )
        incoming = Incoming.search([
            ("name", "in", [
                request.name,
                own_distribution.name,
                other_distribution.name,
            ]),
        ])
        self.assertEqual(
            set(incoming.mapped("name")),
            {request.name, own_distribution.name},
        )
        self.assertFalse(Incoming.has_access("create"))
        self.assertFalse(Incoming.has_access("write"))

        request_delivery = incoming.filtered(
            lambda delivery: delivery.delivery_type == "request"
        )
        distribution_delivery = incoming.filtered(
            lambda delivery: delivery.delivery_type == "distribution"
        )
        self.assertEqual(
            request_delivery.action_open_delivery()["res_model"],
            "restaurant.stock.request",
        )
        self.assertEqual(
            distribution_delivery.action_open_delivery()["res_model"],
            "restaurant.stock.distribution",
        )

    def test_central_can_deliver_less_than_requested_and_branch_receives_once(self):
        request = self._request(
            dispatched_qty=10,
            received_qty=10,
            state="draft",
        )
        self.env["stock.quant"]._update_available_quantity(
            self.product,
            self.central_warehouse.lot_stock_id,
            10,
        )
        request.action_submit()
        central_request = request.with_user(self.central_storekeeper)
        central_request.action_calculate_availability()
        central_request.action_prepare_transfer()
        self.assertEqual(central_request.line_ids.dispatched_qty, 10)

        with self.assertRaises(AccessError):
            request.line_ids.with_user(self.stockkeeper).write({
                "dispatched_qty": 7,
            })

        central_request.line_ids.write({"dispatched_qty": 7})
        central_request.action_dispatch()
        self.assertEqual(central_request.state, "dispatched")
        self.assertEqual(central_request.line_ids.dispatched_qty, 7)
        self.assertEqual(
            central_request.dispatch_picking_id.move_ids.quantity,
            7,
        )
        self.assertTrue(central_request.activity_ids.filtered(
            lambda activity: activity.user_id == self.stockkeeper
        ))

        branch_request = central_request.with_user(self.stockkeeper)
        branch_request.line_ids.write({"branch_received_qty": 7})
        branch_request.action_confirm_branch_receipt()
        self.assertEqual(branch_request.state, "received")
        self.assertEqual(
            branch_request.receipt_picking_id.sudo().move_ids.quantity,
            7,
        )

        with self.assertRaises(UserError):
            branch_request.action_confirm_branch_receipt()

    def test_new_request_form_updates_actual_on_hand_before_save(self):
        no_stock_product = self.env["product.product"].create({
            "name": "No Stock Request Form Product",
            "is_storable": True,
        })
        self.env["stock.quant"]._update_available_quantity(
            self.product,
            self.branch_warehouse.lot_stock_id,
            12,
        )

        accessible_branches = self.env["restaurant.branch"].with_user(
            self.stockkeeper
        ).search([
            ("id", "in", [self.branch.id, self.other_branch.id]),
        ])
        self.assertEqual(accessible_branches, self.branch)

        with Form(
            self.env["restaurant.stock.request"].with_user(self.stockkeeper)
        ) as form:
            form.branch_id = self.branch
            with form.line_ids.new() as line:
                line.product_id = self.product
                self.assertEqual(line.current_on_hand_qty, 12)
                line.product_id = no_stock_product
                self.assertEqual(line.current_on_hand_qty, 0)
                line.product_id = self.product
                self.assertEqual(line.current_on_hand_qty, 12)
                line.requested_qty = 3

        self.assertEqual(form.record.state, "draft")
        self.assertEqual(form.record.line_ids.requested_qty, 3)

    def test_sectioned_receipt_routes_only_to_assigned_daily(self):
        kitchen, bar = self._stock_sections(assign_product=True)
        self._distribution(6, 6)
        today = fields.Date.context_today(
            self.stockkeeper.with_context(tz=self.company.tz or "UTC")
        )
        dailies = self.env["restaurant.stock.daily"].search([
            ("branch_id", "=", self.branch.id),
            ("stock_date", "=", today),
            ("state", "!=", "cancelled"),
        ])
        self.assertEqual(dailies.section_id, kitchen)
        product_line = dailies.line_ids.filtered(
            lambda line: line.product_id == self.product
        )
        self.assertTrue(product_line)
        self.assertEqual(product_line.received_qty, 6)
        self.assertFalse(dailies.filtered(lambda daily: daily.section_id == bar))

    def test_sectioned_receipt_rejects_unassigned_product_safely(self):
        self._stock_sections(assign_product=False)
        with self.assertRaisesRegex(
            ValidationError,
            "approved Kitchen, Bar, or Disposable Product Category",
        ):
            self._distribution(4, 4)
        today = fields.Date.context_today(
            self.stockkeeper.with_context(tz=self.company.tz or "UTC")
        )
        self.assertFalse(self.env["restaurant.stock.daily"].search([
            ("branch_id", "=", self.branch.id),
            ("stock_date", "=", today),
            ("state", "!=", "cancelled"),
        ]))

    def test_central_uses_fixed_native_product_categories(self):
        original_category = self.product.product_tmpl_id.categ_id
        Category = self.env["product.category"].with_user(
            self.central_storekeeper
        ).with_context(allowed_company_ids=[self.company.id])
        approved_names = {
            "Raw Meat and Sea Food",
            "Vegetables",
            "Dry Items",
            "Dairy Items",
            "Disposable",
            "Bar",
        }
        categories = Category.search([
            ("is_restaurant_stock_category", "=", True),
        ]).with_context(lang="en_US")
        self.assertEqual(set(categories.mapped("name")), approved_names)
        self.assertEqual(len(categories), 6)
        self.assertFalse(categories.mapped("company_id"))
        self.assertFalse(categories.mapped("parent_id"))
        self.assertTrue(Category.has_access("read"))
        self.assertFalse(Category.has_access("create"))
        self.assertFalse(Category.has_access("write"))
        self.assertFalse(Category.has_access("unlink"))

        with self.assertRaises(AccessError):
            Category.create({
                "name": "Unauthorized Product Category",
            })

        goods = self.env.ref("product.product_category_goods")
        for field_name in (
            "property_cost_method",
            "property_valuation",
            "property_account_income_categ_id",
            "property_account_expense_categ_id",
            "property_stock_valuation_account_id",
            "property_stock_journal",
        ):
            self.assertEqual(
                {category[field_name] for category in categories},
                {goods[field_name]},
            )

        CentralProduct = self.env["product.template"].with_user(
            self.central_storekeeper
        ).with_context(allowed_company_ids=[self.company.id])
        with self.assertRaisesRegex(
            AccessError,
            "approved Restaurant Product Categories",
        ):
            CentralProduct.create({"name": "Missing Category Product"})
        with self.assertRaisesRegex(
            AccessError,
            "approved Restaurant Product Categories",
        ):
            CentralProduct.create({
                "name": "Legacy Category Product",
                "categ_id": goods.id,
            })

        bar = self.env.ref("restaurant_stock.product_category_bar")
        product = CentralProduct.create({
            "name": "Approved Bar Product",
            "categ_id": bar.id,
        })
        self.assertEqual(product.categ_id, bar)
        self.assertTrue(product.is_storable)
        self.assertFalse(product.company_id)
        self.assertFalse(self.env[
            "restaurant.stock.section.product"
        ].search([("product_id", "=", product.product_variant_id.id)]))

        product.write({"default_code": "APPROVED-BAR"})
        self.assertEqual(product.categ_id, bar)
        with self.assertRaisesRegex(
            AccessError,
            "approved Restaurant Product Categories",
        ):
            product.write({"categ_id": goods.id})

        product_view = self.env.ref(
            "restaurant_stock.restaurant_product_view_form"
        )
        product_arch = CentralProduct.get_view(
            view_id=product_view.id,
            view_type="form",
        )["arch"]
        self.assertIn("is_restaurant_stock_category", product_arch)
        self.assertIn("no_create", product_arch)

        self.assertEqual(
            self.product.product_tmpl_id.categ_id,
            original_category,
        )

        menu = self.env.ref(
            "restaurant_stock.restaurant_stock_product_categories_menu"
        )
        visible = self.env["ir.ui.menu"].with_user(
            self.central_storekeeper
        )._visible_menu_ids()
        self.assertFalse(menu.active)
        self.assertNotIn(menu.id, visible)

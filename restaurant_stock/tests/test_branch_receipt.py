from odoo import Command
from odoo.exceptions import AccessError, UserError
from odoo.tests.common import new_test_user

from odoo.addons.base.tests.common import BaseCommon


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
        cls.central_warehouse = cls.env["stock.warehouse"].search([
            ("company_id", "=", cls.company.id),
            ("code", "=", "WH"),
        ], limit=1)
        cls.product = cls.env["product.product"].create({
            "name": "Branch Receipt Test Product",
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

    def _request(self, dispatched_qty=10, received_qty=10, state="dispatched"):
        request = self.env["restaurant.stock.request"].with_user(
            self.stockkeeper
        ).create({
            "branch_id": self.branch.id,
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
            central_request.line_ids.write({"dispatched_qty": dispatched_qty})
            central_request.action_prepare_transfer()
            central_request.action_dispatch()
            request.line_ids.write({"branch_received_qty": received_qty})
        return request.with_user(self.stockkeeper).with_context(
            allowed_company_ids=[self.company.id]
        )

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

        request.action_confirm_branch_receipt()

        self.assertEqual(request.state, "received")
        self.assertEqual(request.line_ids.difference_qty, -3)
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

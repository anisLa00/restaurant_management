from odoo import Command
from odoo.exceptions import AccessError, UserError
from odoo.tests.common import new_test_user

from odoo.addons.base.tests.common import BaseCommon


class TestPurchaseIntegration(BaseCommon):
    _test_user_groups = ()

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.company = cls.env.company
        cls.central_warehouse = cls.env["stock.warehouse"].search([
            ("company_id", "=", cls.company.id),
            ("code", "=", "WH"),
        ], limit=1)
        cls.branch_warehouse = cls.env["stock.warehouse"].create({
            "name": "Purchase Test Branch Warehouse",
            "code": "PTB",
            "company_id": cls.company.id,
        })
        cls.branch = cls.env["restaurant.branch"].create({
            "name": "Purchase Test Branch",
            "code": "PTB",
            "company_id": cls.company.id,
            "warehouse_id": cls.branch_warehouse.id,
        })
        cls.stockkeeper = cls._make_user(
            "purchase_test_stockkeeper",
            "restaurant_core.group_restaurant_stockkeeper",
        )
        cls.central_storekeeper = cls._make_user(
            "purchase_test_central",
            "restaurant_core.group_restaurant_central_storekeeper",
        )
        cls.purchasing_officer = cls._make_user(
            "purchase_test_officer",
            "restaurant_core.group_restaurant_purchasing",
        )
        cls.product = cls.env["product.product"].create({
            "name": "Purchase Integration Test Product",
            "is_storable": True,
            "purchase_ok": True,
        })
        cls.vendor = cls.env["res.partner"].create({
            "name": "Purchase Integration Test Vendor",
            "supplier_rank": 1,
        })

    @classmethod
    def _make_user(cls, login, group):
        return new_test_user(
            cls.env,
            login=login,
            groups=group,
            company_id=cls.company.id,
            company_ids=[Command.set(cls.company.ids)],
            restaurant_branch_ids=[Command.set(cls.branch.ids)],
        )

    def _purchase_required_request(self, quantity=10):
        request = self.env["restaurant.stock.request"].with_user(
            self.stockkeeper
        ).create({
            "branch_id": self.branch.id,
            "line_ids": [Command.create({
                "product_id": self.product.id,
                "requested_qty": quantity,
            })],
        })
        request.action_submit()
        central_request = request.with_user(self.central_storekeeper)
        central_request.line_ids.write({"purchase_qty": quantity})
        central_request.action_require_purchase()
        return request

    def _create_purchase_order(self, quantity=10):
        request = self._purchase_required_request(quantity)
        purchasing_request = request.with_user(self.purchasing_officer)
        purchasing_request.write({"vendor_id": self.vendor.id})
        action = purchasing_request.action_start_purchasing()
        return request, action

    def _confirm_purchase_order(self, request):
        purchase_order = request.purchase_order_id.with_user(
            self.purchasing_officer
        )
        purchase_order.button_confirm()
        return purchase_order.sudo()

    @staticmethod
    def _validate_receipt(picking, quantity):
        for move in picking.move_ids:
            move.quantity = quantity
        return picking.with_context(skip_backorder=True).button_validate()

    def test_successful_purchase_order_creation_and_quantities(self):
        request, action = self._create_purchase_order(quantity=8)
        purchase_order = request.purchase_order_id.sudo()

        self.assertEqual(request.state, "purchasing")
        self.assertEqual(request.purchasing_officer_id, self.purchasing_officer)
        self.assertEqual(purchase_order.partner_id, self.vendor)
        self.assertEqual(purchase_order.user_id, self.purchasing_officer)
        self.assertEqual(purchase_order.origin, request.name)
        self.assertEqual(purchase_order.picking_type_id, self.central_warehouse.in_type_id)
        self.assertEqual(purchase_order.order_line.product_id, self.product)
        self.assertEqual(purchase_order.order_line.product_qty, 8)
        self.assertEqual(purchase_order.order_line.uom_id, self.product.uom_id)
        self.assertEqual(action["res_id"], purchase_order.id)

    def test_duplicate_purchase_order_is_rejected(self):
        request, _action = self._create_purchase_order()

        with self.assertRaises(UserError):
            request.with_user(self.purchasing_officer).action_start_purchasing()

        self.assertEqual(
            self.env["purchase.order"].search_count([
                ("origin", "=", request.name),
            ]),
            1,
        )

    def test_wrong_role_cannot_create_purchase_order(self):
        request = self._purchase_required_request()

        with self.assertRaises(AccessError):
            request.with_user(self.central_storekeeper).action_start_purchasing()

    def test_invalid_state_cannot_create_purchase_order(self):
        request = self.env["restaurant.stock.request"].with_user(
            self.stockkeeper
        ).create({
            "branch_id": self.branch.id,
            "line_ids": [Command.create({
                "product_id": self.product.id,
                "requested_qty": 10,
            })],
        })
        request.action_submit()

        with self.assertRaises(UserError):
            request.with_user(self.purchasing_officer).action_start_purchasing()

    def test_confirmed_po_creates_supplier_receipt_to_central_stock(self):
        request, _action = self._create_purchase_order(quantity=6)
        purchase_order = self._confirm_purchase_order(request)
        receipt = purchase_order.picking_ids

        self.assertEqual(purchase_order.state, "purchase")
        self.assertEqual(len(receipt), 1)
        self.assertEqual(receipt.picking_type_code, "incoming")
        self.assertEqual(receipt.location_dest_id, self.central_warehouse.lot_stock_id)
        self.assertEqual(receipt.move_ids.product_uom_qty, 6)

        self._validate_receipt(receipt, 6)

        self.assertEqual(receipt.state, "done")
        self.assertEqual(
            self.env["stock.quant"]._get_available_quantity(
                self.product,
                self.central_warehouse.lot_stock_id,
            ),
            6,
        )

    def test_central_storekeeper_verifies_completed_receipt(self):
        request, _action = self._create_purchase_order(quantity=5)
        purchase_order = self._confirm_purchase_order(request)
        self._validate_receipt(purchase_order.picking_ids, 5)

        request.with_user(self.central_storekeeper).action_receive_central()

        self.assertEqual(request.state, "central_received")
        self.assertEqual(request.line_ids.central_received_qty, 5)

    def test_central_verification_requires_done_receipt(self):
        request, _action = self._create_purchase_order()
        self._confirm_purchase_order(request)

        with self.assertRaises(UserError):
            request.with_user(self.central_storekeeper).action_receive_central()

    def test_central_received_quantity_cannot_be_entered_manually(self):
        request, _action = self._create_purchase_order()

        with self.assertRaises(AccessError):
            request.with_user(
                self.central_storekeeper
            ).line_ids.write({"central_received_qty": 10})

    def test_partial_receipt_records_quantity_and_keeps_native_backorder(self):
        request, _action = self._create_purchase_order(quantity=10)
        purchase_order = self._confirm_purchase_order(request)
        first_receipt = purchase_order.picking_ids

        self._validate_receipt(first_receipt, 4)
        purchase_order.invalidate_recordset(["picking_ids"])
        request.with_user(self.central_storekeeper).action_receive_central()

        backorder = purchase_order.picking_ids.filtered(
            lambda picking: picking.state not in ("done", "cancel")
        )
        self.assertEqual(first_receipt.state, "done")
        self.assertEqual(len(backorder), 1)
        self.assertEqual(backorder.move_ids.product_uom_qty, 6)
        self.assertEqual(request.state, "purchasing")
        self.assertEqual(request.line_ids.central_received_qty, 4)

        self._validate_receipt(backorder, 6)
        request.with_user(self.central_storekeeper).action_receive_central()

        self.assertEqual(request.state, "central_received")
        self.assertEqual(request.line_ids.central_received_qty, 10)

    def test_branch_stockkeeper_has_no_purchase_access(self):
        self.assertFalse(
            self.stockkeeper.has_group("purchase.group_purchase_user")
        )
        self.assertFalse(
            self.env["purchase.order"].with_user(
                self.stockkeeper
            ).has_access("create")
        )

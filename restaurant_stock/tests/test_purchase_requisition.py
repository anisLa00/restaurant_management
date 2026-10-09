from datetime import timedelta

from odoo import Command, fields
from odoo.exceptions import AccessError, ValidationError
from odoo.tests.common import new_test_user
from odoo.tools.binary import BinaryBytes

from odoo.addons.base.tests.common import BaseCommon


class TestPurchaseRequisition(BaseCommon):
    _test_user_groups = ()

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.company = cls.env.company
        cls.branch_warehouse = cls.env["stock.warehouse"].create({
            "name": "Requisition Test Branch Warehouse",
            "code": "RTB",
            "company_id": cls.company.id,
        })
        cls.branch = cls.env["restaurant.branch"].create({
            "name": "Requisition Test Branch",
            "code": "RTB",
            "company_id": cls.company.id,
            "warehouse_id": cls.branch_warehouse.id,
        })
        cls.manager = cls._make_user(
            "requisition_test_manager",
            "restaurant_core.group_restaurant_branch_manager",
        )
        cls.purchasing = cls._make_user(
            "requisition_test_purchasing",
            "restaurant_core.group_restaurant_purchasing",
        )
        cls.operations = cls._make_user(
            "requisition_test_operations",
            "restaurant_core.group_restaurant_operations_manager",
        )
        cls.owner = cls._make_user(
            "requisition_test_owner",
            "restaurant_core.group_restaurant_owner",
        )
        cls.central = cls._make_user(
            "requisition_test_central",
            "restaurant_core.group_restaurant_central_storekeeper",
        )
        cls.vendor = cls.env["res.partner"].create({
            "name": "Requisition Test Vendor",
            "supplier_rank": 1,
        })
        cls.service = cls.env["product.product"].create({
            "name": "Requisition Test Service",
            "type": "service",
            "purchase_ok": True,
        })
        cls.goods = cls.env["product.product"].create({
            "name": "Requisition Test Goods",
            "is_storable": True,
            "purchase_ok": True,
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

    def _new_requisition(self, product=None, quantity=2):
        product = product or self.service
        return self.env["restaurant.purchase.requisition"].with_user(
            self.manager
        ).create({
            "branch_id": self.branch.id,
            "needed_by": fields.Date.today() + timedelta(days=7),
            "purpose": "Required for restaurant operations.",
            "line_ids": [Command.create({
                "product_id": product.id,
                "description": product.display_name,
                "quantity": quantity,
                "uom_id": product.uom_id.id,
            })],
        })

    def _source_offer(self, requisition, price=100):
        requisition.with_user(self.manager).action_submit()
        requisition.with_user(self.purchasing).action_start_sourcing()
        offer = self.env["restaurant.purchase.requisition.offer"].with_user(
            self.purchasing
        ).create({
            "name": "VENDOR-QUOTE-001",
            "requisition_id": requisition.id,
            "vendor_id": self.vendor.id,
            "attachment": BinaryBytes(b"vendor quote", filename="vendor-quote.pdf"),
        })
        offer.line_ids.write({"price_unit": price})
        offer.action_mark_ready()
        offer.action_select()
        requisition.with_user(self.purchasing).write({
            "single_quote_justification": "Only approved vendor was available.",
        })
        return offer

    def _approve_and_order(self, requisition, price=100):
        offer = self._source_offer(requisition, price=price)
        requisition.with_user(self.purchasing).action_submit_approval()
        approver = self.owner if requisition.state == "awaiting_owner" else self.operations
        requisition.with_user(approver).action_approve()
        requisition.with_user(self.purchasing).action_create_purchase_order()
        return offer, requisition.purchase_order_id.sudo()

    def test_service_requisition_runs_to_fulfilled(self):
        requisition = self._new_requisition(quantity=3)
        offer, purchase_order = self._approve_and_order(requisition, price=25)

        self.assertEqual(purchase_order.partner_id, self.vendor)
        self.assertEqual(purchase_order.origin, requisition.name)
        self.assertEqual(purchase_order.order_line.product_qty, 3)
        self.assertEqual(purchase_order.order_line.price_unit, 25)
        self.assertEqual(offer.state, "ready")
        self.assertEqual(requisition.state, "ordered")

        purchase_order.with_user(self.purchasing).button_confirm()
        requisition.with_user(self.operations).action_accept_services()

        self.assertEqual(requisition.state, "fulfilled")
        self.assertTrue(requisition.services_accepted)

    def test_high_value_requisition_requires_owner(self):
        self.company.restaurant_owner_purchase_approval_threshold = 50
        requisition = self._new_requisition(quantity=2)
        self._source_offer(requisition, price=100)
        requisition.with_user(self.purchasing).action_submit_approval()

        self.assertEqual(requisition.state, "awaiting_owner")
        with self.assertRaises(AccessError):
            requisition.with_user(self.operations).action_approve()

        requisition.with_user(self.owner).action_approve()
        self.assertEqual(requisition.state, "approved")

    def test_ready_offer_and_submitted_lines_are_locked(self):
        requisition = self._new_requisition()
        offer = self._source_offer(requisition)

        with self.assertRaises(AccessError):
            offer.with_user(self.purchasing).write({"name": "CHANGED"})
        with self.assertRaises(AccessError):
            requisition.line_ids.with_user(self.manager).write({"quantity": 5})

        offer.with_user(self.purchasing).action_reset_draft()
        offer.with_user(self.purchasing).write({"name": "CHANGED"})
        self.assertEqual(offer.name, "CHANGED")

    def test_incomplete_or_duplicate_quote_cannot_be_completed(self):
        requisition = self._new_requisition()
        requisition.with_user(self.manager).action_submit()
        requisition.with_user(self.purchasing).action_start_sourcing()
        offer = self.env["restaurant.purchase.requisition.offer"].with_user(
            self.purchasing
        ).create({
            "name": "DUPLICATE-QUOTE",
            "requisition_id": requisition.id,
            "vendor_id": self.vendor.id,
            "attachment": BinaryBytes(b"quote", filename="quote.pdf"),
        })
        source_line = requisition.line_ids
        offer.line_ids.create({
            "offer_id": offer.id,
            "requisition_line_id": source_line.id,
            "product_id": source_line.product_id.id,
            "description": source_line.description,
            "quantity": source_line.quantity,
            "uom_id": source_line.uom_id.id,
            "price_unit": 10,
        })
        offer.line_ids.write({"price_unit": 10})

        with self.assertRaises(ValidationError):
            offer.action_mark_ready()

    def test_goods_require_completed_supplier_receipt(self):
        requisition = self._new_requisition(product=self.goods, quantity=4)
        _offer, purchase_order = self._approve_and_order(requisition, price=12)
        purchase_order.with_user(self.purchasing).button_confirm()

        with self.assertRaises(ValidationError):
            requisition.with_user(self.central).action_verify_goods_receipt()

        receipt = purchase_order.picking_ids
        for move in receipt.move_ids:
            move.quantity = 4
        receipt.with_user(self.central).with_context(skip_backorder=True).button_validate()
        requisition.with_user(self.central).action_verify_goods_receipt()

        self.assertTrue(requisition.goods_received)
        self.assertEqual(requisition.state, "fulfilled")

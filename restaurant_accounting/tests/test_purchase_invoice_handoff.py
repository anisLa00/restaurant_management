from odoo import Command, fields
from odoo.exceptions import AccessError, ValidationError
from odoo.tests.common import new_test_user
from odoo.tools.binary import BinaryBytes

from odoo.addons.account.tests.common import AccountTestInvoicingCommon


class TestPurchaseInvoiceHandoff(AccountTestInvoicingCommon):
    _test_user_groups = ()

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.company = cls.env.company
        cls.purchasing = cls._make_user(
            "handoff_test_purchasing",
            "restaurant_core.group_restaurant_purchasing",
        )
        cls.operations = cls._make_user(
            "handoff_test_operations",
            "restaurant_core.group_restaurant_operations_manager",
        )
        cls.owner = cls._make_user(
            "handoff_test_owner",
            "restaurant_core.group_restaurant_owner",
        )
        cls.accountant = cls._make_user(
            "handoff_test_accountant",
            "restaurant_core.group_restaurant_accountant",
        )
        cls.vendor = cls.env["res.partner"].create({
            "name": "Supplier Invoice Handoff Vendor",
            "supplier_rank": 1,
        })
        cls.service = cls.env["product.product"].create({
            "name": "Supplier Invoice Handoff Service",
            "type": "service",
            "purchase_ok": True,
            "property_account_expense_id": cls.company_data["default_account_expense"].id,
            "supplier_taxes_id": [Command.clear()],
        })

    @classmethod
    def _make_user(cls, login, group):
        return new_test_user(
            cls.env,
            login=login,
            groups=group,
            company_id=cls.company.id,
            company_ids=[Command.set(cls.company.ids)],
        )

    def _purchase_order(self, quantity=2, price=100):
        purchase_order = self.env["purchase.order"].with_user(self.purchasing).create({
            "partner_id": self.vendor.id,
            "company_id": self.company.id,
            "order_line": [Command.create({
                "product_id": self.service.id,
                "name": self.service.display_name,
                "product_qty": quantity,
                "uom_id": self.service.uom_id.id,
                "price_unit": price,
                "date_planned": fields.Datetime.now(),
            })],
        })
        purchase_order.button_confirm()
        return purchase_order.sudo()

    def _handoff(self, purchase_order, reference="SUP-INV-001"):
        return self.env["restaurant.purchase.invoice.handoff"].with_user(
            self.purchasing
        ).create({
            "purchase_order_id": purchase_order.id,
            "invoice_reference": reference,
            "invoice_date": fields.Date.today(),
            "vendor_invoice_file": BinaryBytes(
                b"supplier invoice", filename=f"{reference}.pdf"
            ),
            "vendor_invoice_filename": f"{reference}.pdf",
        })

    def test_matched_invoice_creates_and_posts_vendor_bill(self):
        purchase_order = self._purchase_order(quantity=2, price=100)
        handoff = self._handoff(purchase_order)

        self.assertEqual(handoff.match_state, "matched")
        self.assertEqual(purchase_order.restaurant_invoice_handoff_count, 1)
        handoff_action = purchase_order.action_open_restaurant_invoice_handoffs()
        self.assertEqual(
            handoff_action["context"]["default_purchase_order_id"],
            purchase_order.id,
        )
        handoff.with_user(self.purchasing).action_submit()
        self.assertEqual(handoff.state, "awaiting_accounting")

        with self.assertRaises(AccessError):
            handoff.with_user(self.purchasing).action_create_vendor_bill()

        action = handoff.with_user(self.accountant).action_create_vendor_bill()
        bill = handoff.vendor_bill_id.sudo()
        self.assertEqual(action["res_id"], bill.id)
        self.assertEqual(handoff.state, "bill_draft")
        self.assertEqual(bill.ref, "SUP-INV-001")
        self.assertEqual(bill.restaurant_purchase_handoff_id, handoff)
        self.assertEqual(bill.invoice_line_ids.filtered("purchase_line_id").quantity, 2)
        self.assertEqual(bill.invoice_line_ids.filtered("purchase_line_id").price_unit, 100)

        handoff.with_user(self.accountant).action_post_vendor_bill()
        self.assertEqual(handoff.state, "posted")
        self.assertEqual(bill.state, "posted")
        self.assertEqual(handoff.bill_posted_by_id, self.accountant)

    def test_price_variance_requires_operations_approval(self):
        self.company.restaurant_owner_purchase_approval_threshold = 5000
        handoff = self._handoff(self._purchase_order(), reference="SUP-INV-VAR")
        handoff.line_ids.with_user(self.purchasing).write({"invoice_unit_price": 110})
        self.assertEqual(handoff.match_state, "price_variance")

        handoff.with_user(self.purchasing).action_submit()
        self.assertEqual(handoff.state, "awaiting_operations")
        handoff.with_user(self.operations).write({
            "exception_reason": "Urgent approved supplier price adjustment.",
        })
        handoff.with_user(self.operations).action_approve_exception()

        self.assertEqual(handoff.state, "awaiting_accounting")
        self.assertEqual(handoff.exception_approved_by_id, self.operations)

    def test_large_variance_requires_owner(self):
        self.company.restaurant_owner_purchase_approval_threshold = 5
        handoff = self._handoff(self._purchase_order(), reference="SUP-INV-HIGH")
        handoff.line_ids.with_user(self.purchasing).write({"invoice_unit_price": 110})
        handoff.with_user(self.purchasing).action_submit()

        self.assertEqual(handoff.state, "awaiting_owner")
        with self.assertRaises(AccessError):
            handoff.with_user(self.operations).action_approve_exception()

        handoff.with_user(self.owner).write({"exception_reason": "Owner approved variance."})
        handoff.with_user(self.owner).action_approve_exception()
        self.assertEqual(handoff.state, "awaiting_accounting")

    def test_submitted_invoice_lines_are_locked(self):
        handoff = self._handoff(self._purchase_order(), reference="SUP-INV-LOCK")
        handoff.with_user(self.purchasing).action_submit()

        with self.assertRaises(AccessError):
            handoff.line_ids.with_user(self.purchasing).write({"invoice_quantity": 1})

    def test_unconfirmed_purchase_order_is_rejected(self):
        purchase_order = self.env["purchase.order"].with_user(self.purchasing).create({
            "partner_id": self.vendor.id,
            "company_id": self.company.id,
            "order_line": [Command.create({
                "product_id": self.service.id,
                "name": self.service.display_name,
                "product_qty": 1,
                "uom_id": self.service.uom_id.id,
                "price_unit": 100,
                "date_planned": fields.Datetime.now(),
            })],
        })
        with self.assertRaises(ValidationError):
            self._handoff(purchase_order.sudo(), reference="SUP-INV-DRAFT-PO")

from odoo import Command
from odoo.exceptions import AccessError, ValidationError
from odoo.tests.common import new_test_user

from odoo.addons.base.tests.common import BaseCommon


class TestCentralAllocation(BaseCommon):
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
            "name": "Allocation Test Branch Warehouse",
            "code": "ATB",
            "company_id": cls.company.id,
        })
        cls.branch = cls.env["restaurant.branch"].create({
            "name": "Allocation Test Branch",
            "code": "ATB",
            "company_id": cls.company.id,
            "warehouse_id": cls.branch_warehouse.id,
        })
        cls.stockkeeper = cls._make_user(
            "allocation_test_stockkeeper",
            "restaurant_core.group_restaurant_stockkeeper",
        )
        cls.central_storekeeper = cls._make_user(
            "allocation_test_central",
            "restaurant_core.group_restaurant_central_storekeeper",
        )
        cls.purchasing_officer = cls._make_user(
            "allocation_test_purchasing",
            "restaurant_core.group_restaurant_purchasing",
        )
        cls.products = cls.env["product.product"].create([
            {
                "name": "Allocation Test Product A",
                "is_storable": True,
                "purchase_ok": True,
            },
            {
                "name": "Allocation Test Product B",
                "is_storable": True,
                "purchase_ok": True,
            },
        ])

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

    def _add_stock(self, product, quantity):
        self.env["stock.quant"]._update_available_quantity(
            product,
            self.central_warehouse.lot_stock_id,
            quantity,
        )

    def _submitted_request(self, quantities):
        request = self.env["restaurant.stock.request"].with_user(
            self.stockkeeper
        ).create({
            "branch_id": self.branch.id,
            "line_ids": [
                Command.create({
                    "product_id": product.id,
                    "requested_qty": quantity,
                })
                for product, quantity in quantities
            ],
        })
        request.action_submit()
        return request

    def _calculate(self, request):
        request.with_user(
            self.central_storekeeper
        ).action_calculate_availability()
        request.invalidate_recordset()
        return request

    def test_full_availability_is_reserved_for_transfer(self):
        product = self.products[0]
        self._add_stock(product, 10)
        request = self._calculate(self._submitted_request([(product, 6)]))

        self.assertEqual(request.line_ids.transfer_qty, 6)
        self.assertEqual(request.line_ids.purchase_qty, 0)
        self.assertEqual(request.dispatch_picking_id.sudo().state, "assigned")
        self.assertEqual(request.dispatch_picking_id.sudo().move_ids.quantity, 6)

    def test_zero_availability_goes_entirely_to_purchase(self):
        product = self.products[0]
        request = self._calculate(self._submitted_request([(product, 7)]))

        self.assertEqual(request.line_ids.transfer_qty, 0)
        self.assertEqual(request.line_ids.purchase_qty, 7)
        self.assertEqual(request.dispatch_picking_id.sudo().move_ids.quantity, 0)

    def test_partial_availability_splits_quantity(self):
        product = self.products[0]
        self._add_stock(product, 4)
        request = self._calculate(self._submitted_request([(product, 9)]))

        self.assertEqual(request.line_ids.transfer_qty, 4)
        self.assertEqual(request.line_ids.purchase_qty, 5)
        self.assertEqual(
            request.dispatch_picking_id.sudo().move_ids.quantity,
            4,
        )

    def test_multiple_lines_are_allocated_independently(self):
        first_product, second_product = self.products
        self._add_stock(first_product, 8)
        self._add_stock(second_product, 2)
        request = self._calculate(self._submitted_request([
            (first_product, 5),
            (second_product, 6),
        ]))
        lines = {line.product_id: line for line in request.line_ids}

        self.assertEqual(lines[first_product].transfer_qty, 5)
        self.assertEqual(lines[first_product].purchase_qty, 0)
        self.assertEqual(lines[second_product].transfer_qty, 2)
        self.assertEqual(lines[second_product].purchase_qty, 4)

    def test_native_reservation_prevents_double_allocation(self):
        product = self.products[0]
        self._add_stock(product, 10)
        first_request = self._calculate(
            self._submitted_request([(product, 7)])
        )
        second_request = self._calculate(
            self._submitted_request([(product, 7)])
        )

        self.assertEqual(first_request.line_ids.transfer_qty, 7)
        self.assertEqual(second_request.line_ids.transfer_qty, 3)
        self.assertEqual(second_request.line_ids.purchase_qty, 4)
        self.assertEqual(
            self.env["stock.quant"]._get_available_quantity(
                product,
                self.central_warehouse.lot_stock_id,
            ),
            0,
        )

    def test_recalculation_releases_and_rebuilds_reservation(self):
        product = self.products[0]
        self._add_stock(product, 5)
        request = self._calculate(self._submitted_request([(product, 8)]))
        old_picking = request.dispatch_picking_id.sudo()

        self._add_stock(product, 3)
        self._calculate(request)

        self.assertEqual(old_picking.state, "cancel")
        self.assertEqual(request.line_ids.transfer_qty, 8)
        self.assertEqual(request.line_ids.purchase_qty, 0)

    def test_allocation_quantities_cannot_be_edited_manually(self):
        request = self._submitted_request([(self.products[0], 5)])

        with self.assertRaises(AccessError):
            request.with_user(self.stockkeeper).line_ids.write({
                "transfer_qty": 5,
            })

        with self.assertRaises(AccessError):
            request.with_user(self.purchasing_officer).line_ids.write({
                "purchase_qty": 5,
            })

    def test_path_requires_calculation(self):
        request = self._submitted_request([(self.products[0], 5)])

        with self.assertRaises(ValidationError):
            request.with_user(
                self.central_storekeeper
            ).action_prepare_transfer()

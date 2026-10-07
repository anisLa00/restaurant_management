from odoo import Command, fields
from odoo.exceptions import AccessError, UserError, ValidationError
from odoo.tests.common import new_test_user

from odoo.addons.base.tests.common import BaseCommon


class TestRestaurantStockEvents(BaseCommon):
    _test_user_groups = ()

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.company = cls.env.company
        cls.warehouse, cls.other_warehouse = cls.env["stock.warehouse"].create([
            {
                "name": "Service Event Test Warehouse",
                "code": "SET",
                "company_id": cls.company.id,
            },
            {
                "name": "Service Event Other Warehouse",
                "code": "SEO",
                "company_id": cls.company.id,
            },
        ])
        cls.branch, cls.other_branch = cls.env["restaurant.branch"].create([
            {
                "name": "Service Event Test Branch",
                "code": "SET",
                "company_id": cls.company.id,
                "warehouse_id": cls.warehouse.id,
            },
            {
                "name": "Service Event Other Branch",
                "code": "SEO",
                "company_id": cls.company.id,
                "warehouse_id": cls.other_warehouse.id,
            },
        ])
        cls.reception = cls._make_user(
            "event_reception",
            "restaurant_core.group_restaurant_reception",
            cls.branch,
        )
        cls.stockkeeper = cls._make_user(
            "event_stockkeeper",
            "restaurant_core.group_restaurant_stockkeeper",
            cls.branch,
        )
        cls.central_storekeeper = cls._make_user(
            "event_central_storekeeper",
            "restaurant_core.group_restaurant_central_storekeeper",
            cls.branch,
        )
        cls.manager = cls._make_user(
            "event_manager",
            "restaurant_core.group_restaurant_branch_manager",
            cls.branch,
        )
        cls.other_manager = cls._make_user(
            "event_other_manager",
            "restaurant_core.group_restaurant_branch_manager",
            cls.other_branch,
        )
        cls.dual_role_user = cls._make_user(
            "event_dual_role",
            (
                "restaurant_core.group_restaurant_stockkeeper,"
                "restaurant_core.group_restaurant_branch_manager"
            ),
            cls.branch,
        )
        cls.product = cls.env["product.product"].create({
            "name": "Synthetic Recipe Stock Product",
            "is_storable": True,
        })
        cls.category = cls.env["restaurant.menu.category"].create({
            "name": "Synthetic Main Dishes",
            "company_id": cls.company.id,
            "branch_id": cls.branch.id,
        })
        cls.sized_item = cls.env["restaurant.menu.item"].create({
            "name": "Synthetic Grilled Dish",
            "category_id": cls.category.id,
            "size_applicable": True,
        })
        cls.full_variant, cls.half_variant = cls.env[
            "restaurant.menu.variant"
        ].create([
            {"item_id": cls.sized_item.id, "size": "full"},
            {"item_id": cls.sized_item.id, "size": "half"},
        ])
        cls.env["restaurant.menu.stock.mapping"].create([
            {
                "variant_id": cls.full_variant.id,
                "stock_product_id": cls.product.id,
                "stock_qty": 2.0,
                "stock_uom_id": cls.product.uom_id.id,
                "note": "Synthetic test-only ratio",
            },
            {
                "variant_id": cls.half_variant.id,
                "stock_product_id": cls.product.id,
                "stock_qty": 1.0,
                "stock_uom_id": cls.product.uom_id.id,
                "note": "Synthetic test-only ratio",
            },
        ])
        cls.unmapped_item = cls.env["restaurant.menu.item"].create({
            "name": "Synthetic Refreshment",
            "category_id": cls.category.id,
            "size_applicable": False,
        })
        cls.unmapped_variant = cls.env["restaurant.menu.variant"].create({
            "item_id": cls.unmapped_item.id,
            "size": "standard",
        })
        cls.service_date = fields.Date.context_today(
            cls.env["restaurant.stock.event"].with_context(
                tz=cls.company.tz or "UTC"
            )
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

    def _event(
        self,
        event_type="cancellation",
        variant=None,
        quantity=1.0,
        branch=None,
        user=None,
        closing=None,
        order_reference=None,
        reported_amount=0.0,
    ):
        variant = variant or self.full_variant
        branch = branch or self.branch
        user = user or self.reception
        return self.env["restaurant.stock.event"].with_user(user).create({
            "event_type": event_type,
            "branch_id": branch.id,
            "service_date": self.service_date,
            "category_id": variant.category_id.id,
            "menu_item_id": variant.item_id.id,
            "variant_id": variant.id,
            "quantity": quantity,
            "reported_amount": reported_amount,
            "closing_id": closing.id if closing else False,
            "order_reference": order_reference,
            "reason": "Synthetic workflow test",
        })

    def _closing(self, **values):
        closing = self.env["restaurant.daily.closing"].with_user(
            self.reception
        ).create({
            "branch_id": self.branch.id,
            "closing_date": self.service_date,
            "reported_total_sales": 100.0,
            **values,
        })
        # These stock-event tests intentionally isolate the historical
        # Reception closing flow. Standalone Waiter Sales & Tips integration
        # has its own focused lifecycle and reconciliation coverage.
        closing.sudo().with_context(service_tracking_migration=True).write({
            "service_tracking_mode": "legacy",
        })
        return closing

    def _reception_sale(self, closing, variant=None, menu_qty=1.0, **values):
        return self.env["restaurant.reception.sold.menu.line"].with_user(
            self.reception
        ).create({
            "closing_id": closing.id,
            "variant_id": (variant or self.full_variant).id,
            "menu_qty": menu_qty,
            **values,
        })

    def _reception_flavor_sale(self, closing, menu_item, **counts):
        return self.env["restaurant.reception.sold.menu.line"].with_user(
            self.reception
        ).create({
            "closing_id": closing.id,
            "menu_item_id": menu_item.id,
            **counts,
        })

    def _to_manager_review(self, event, prepared=True, stockkeeper=None):
        event.with_user(self.reception).action_submit_kitchen()
        if event.event_type == "complimentary":
            if not prepared:
                raise AssertionError(
                    "New served Complimentary events always submit as prepared."
                )
            return event
        kitchen_event = event.with_user(stockkeeper or self.stockkeeper)
        if prepared:
            kitchen_event.action_mark_prepared()
        else:
            kitchen_event.action_mark_not_prepared()
        return event

    def _approve(self, event, prepared=True):
        self._to_manager_review(event, prepared=prepared)
        event.with_user(self.manager).action_approve()
        return event

    def _daily(self, opening_qty=20.0):
        self.env["stock.quant"]._update_available_quantity(
            self.product,
            self.warehouse.lot_stock_id,
            opening_qty,
        )
        Daily = self.env["restaurant.stock.daily"].with_user(self.stockkeeper)
        daily = Daily.create({
            "branch_id": self.branch.id,
            "stock_date": self.service_date,
        })
        daily.action_confirm_opening()
        return daily

    def _stock_sections(self, assign_product=True):
        Section = self.env["restaurant.stock.section"].with_user(
            self.stockkeeper
        )
        kitchen, bar = Section.create([
            {
                "name": "Event Kitchen",
                "section_type": "kitchen",
                "branch_id": self.branch.id,
            },
            {
                "name": "Event Bar",
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

    def _confirm_zero_catalog_lines(self, daily):
        daily.line_ids.filtered(
            lambda line: (
                line.product_id != self.product
                and line.actual_closing_state != "counted"
            )
        ).with_user(self.stockkeeper).write({
            "actual_closing_qty": 0.0,
            "actual_closing_state": "counted",
        })

    def test_prepared_events_apply_once_and_post_separate_moves(self):
        cancellation = self._approve(
            self._event("cancellation", self.full_variant, 2.0),
            prepared=True,
        )
        complimentary = self._approve(
            self._event("complimentary", self.half_variant, 3.0),
            prepared=True,
        )

        self.assertEqual(cancellation.application_status, "pending_daily")
        self.assertEqual(cancellation.application_ids.quantity, 4.0)
        self.assertEqual(cancellation.application_ids.effect_type, "waste")
        self.assertEqual(complimentary.application_ids.quantity, 3.0)
        self.assertEqual(
            complimentary.application_ids.effect_type,
            "consumption",
        )

        self.env["restaurant.menu.mapping.profile"].create({
            "name": "Synthetic Additional Consumption Setup",
            "branch_id": self.branch.id,
            "category_id": self.category.id,
            "stock_product_id": self.product.id,
            "allow_additional_consumption": True,
        })

        daily = self._daily(20.0)
        line = daily.line_ids.filtered(
            lambda current: current.product_id == self.product
        )
        self.assertTrue(line)
        self.assertEqual(cancellation.daily_id, daily)
        self.assertEqual(complimentary.daily_id, daily)
        self.assertEqual(line.auto_complimentary_qty, 3.0)
        self.assertEqual(line.auto_cancellation_waste_qty, 4.0)

        line.with_user(self.stockkeeper).write({
            "consumption_qty": 3.0,
            "actual_closing_qty": 10.0,
            "actual_closing_state": "counted",
            "note": "Synthetic preparation outside mapped Sold.",
        })
        self.assertEqual(line.total_consumption_qty, 6.0)
        self.assertEqual(line.total_waste_qty, 4.0)
        self.assertEqual(line.expected_closing_qty, 10.0)

        daily.action_submit_closing()
        daily.with_user(self.manager).action_close()
        self.assertEqual(daily.state, "closed")
        self.assertEqual(line.consumption_move_id.state, "done")
        self.assertEqual(line.consumption_move_id.quantity, 6.0)
        self.assertEqual(line.cancellation_waste_move_id.state, "done")
        self.assertEqual(line.cancellation_waste_move_id.quantity, 4.0)
        native_quantity = self.env["stock.quant"]._get_available_quantity(
            self.product,
            self.warehouse.lot_stock_id,
        )
        self.assertEqual(native_quantity, 10.0)

        summary = self.env[
            "restaurant.stock.daily.event.summary"
        ].search([("daily_id", "=", daily.id)])
        full_summary = summary.filtered(
            lambda current: current.variant_id == self.full_variant
        )
        half_summary = summary.filtered(
            lambda current: current.variant_id == self.half_variant
        )
        self.assertEqual(full_summary.prepared_cancelled_qty, 2.0)
        self.assertEqual(half_summary.prepared_complimentary_qty, 3.0)

        consumption_move = line.consumption_move_id
        waste_move = line.cancellation_waste_move_id
        with self.assertRaises(UserError):
            daily.with_user(self.manager).action_close()
        self.assertEqual(line.consumption_move_id, consumption_move)
        self.assertEqual(line.cancellation_waste_move_id, waste_move)

    def test_unprepared_cancellation_is_reported_without_stock_effect(self):
        cancellation = self._approve(
            self._event("cancellation", self.unmapped_variant, 2.0),
            prepared=False,
        )
        daily = self._daily(8.0)
        line = daily.line_ids.filtered(
            lambda current: current.product_id == self.product
        )

        self.assertFalse(cancellation.application_ids)
        self.assertEqual(cancellation.application_status, "no_stock_effect")
        self.assertEqual(line.auto_complimentary_qty, 0.0)
        self.assertEqual(line.auto_cancellation_waste_qty, 0.0)

        summary = self.env[
            "restaurant.stock.daily.event.summary"
        ].search([("daily_id", "=", daily.id)])
        self.assertEqual(summary.unprepared_cancelled_qty, 2.0)
        self.assertEqual(summary.unprepared_complimentary_qty, 0.0)

    def test_submissions_split_cancellation_and_complimentary_workflows(self):
        cancellation = self._event("cancellation")
        complimentary = self._event("complimentary", self.half_variant)

        cancellation.with_user(self.reception).action_submit_kitchen()
        complimentary.with_user(self.reception).action_submit_kitchen()

        self.assertEqual(cancellation.state, "kitchen_review")
        self.assertEqual(cancellation.preparation_status, "pending")
        self.assertFalse(cancellation.kitchen_checked_by_id)
        self.assertEqual(complimentary.state, "manager_review")
        self.assertEqual(complimentary.preparation_status, "prepared")
        self.assertFalse(complimentary.kitchen_checked_by_id)
        self.assertFalse(complimentary.kitchen_checked_at)

        with self.assertRaisesRegex(
            UserError,
            "do not require a Kitchen Check",
        ):
            complimentary.with_user(self.manager).action_return_to_kitchen()

        complimentary.with_user(self.manager).action_approve()
        self.assertEqual(complimentary.state, "approved")
        self.assertEqual(complimentary.application_ids.effect_type, "consumption")

    def test_prepared_event_without_mapping_stays_in_manager_review(self):
        event = self._event(
            "cancellation",
            self.unmapped_variant,
            1.0,
        )
        self._to_manager_review(event, prepared=True)

        with self.assertRaisesRegex(ValidationError, "Missing Mapping"):
            event.with_user(self.manager).action_approve()

        self.assertEqual(event.state, "manager_review")
        self.assertTrue(event.mapping_missing)
        self.assertFalse(event.application_ids)

    def test_branch_and_role_boundaries(self):
        event = self._event()
        event.with_user(self.reception).action_submit_kitchen()

        with self.assertRaises(AccessError):
            event.with_user(self.other_manager).action_approve()
        with self.assertRaises(AccessError):
            event.with_user(self.manager).action_mark_prepared()

        event.with_user(self.stockkeeper).action_mark_prepared()
        event.with_user(self.manager).action_approve()
        with self.assertRaises(UserError):
            event.with_user(self.manager).action_approve()

    def test_kitchen_checker_cannot_approve_same_event(self):
        event = self._event()
        event.with_user(self.reception).action_submit_kitchen()
        event.with_user(self.dual_role_user).action_mark_prepared()
        with self.assertRaises(AccessError):
            event.with_user(self.dual_role_user).action_approve()
        self.assertEqual(event.state, "manager_review")

    def test_closed_daily_blocks_late_approval_without_mutation(self):
        daily = self._daily(10.0)
        event = self._event("complimentary", self.half_variant, 2.0)
        self._to_manager_review(event, prepared=True)

        line = daily.line_ids.filtered(
            lambda current: current.product_id == self.product
        )
        line.with_user(self.stockkeeper).write({
            "actual_closing_qty": 10.0,
            "actual_closing_state": "counted",
        })
        daily.action_submit_closing()
        daily.with_user(self.manager).action_close()
        move_before = line.consumption_move_id

        with self.assertRaisesRegex(ValidationError, "already closed"):
            event.with_user(self.manager).action_approve()

        self.assertEqual(event.state, "manager_review")
        self.assertFalse(event.application_ids)
        self.assertEqual(line.consumption_move_id, move_before)
        self.assertEqual(line.total_consumption_qty, 0.0)

    def test_prepared_event_requires_stockkeeper_section_assignment(self):
        kitchen, _bar = self._stock_sections(assign_product=False)
        event = self._event("complimentary", self.half_variant, 2.0)
        self._to_manager_review(event, prepared=True)

        with self.assertRaisesRegex(
            ValidationError,
            "approved Kitchen, Bar, or Disposable Product Category",
        ):
            event.with_user(self.manager).action_approve()
        self.assertEqual(event.state, "manager_review")
        self.assertFalse(event.application_ids)

        self.env["restaurant.stock.section.product"].with_user(
            self.stockkeeper
        ).create({
            "section_id": kitchen.id,
            "product_id": self.product.id,
        })
        event.with_user(self.manager).action_approve()
        self.assertEqual(event.application_ids.section_id, kitchen)

    def test_approved_event_routes_once_to_its_stock_section(self):
        kitchen, bar = self._stock_sections()
        event = self._approve(
            self._event("complimentary", self.half_variant, 2.0),
            prepared=True,
        )
        self.env["stock.quant"]._update_available_quantity(
            self.product,
            self.warehouse.lot_stock_id,
            10.0,
        )
        Daily = self.env["restaurant.stock.daily"].with_user(self.stockkeeper)
        kitchen_daily = Daily.create({
            "branch_id": self.branch.id,
            "section_id": kitchen.id,
            "stock_date": self.service_date,
        })
        bar_daily = Daily.create({
            "branch_id": self.branch.id,
            "section_id": bar.id,
            "stock_date": self.service_date,
        })
        application = event.application_ids
        self.assertEqual(application.daily_id, kitchen_daily)
        self.assertEqual(application.daily_line_id.daily_id, kitchen_daily)
        self.assertEqual(application.daily_line_id.auto_complimentary_qty, 2.0)
        self.assertFalse(
            bar_daily.line_ids.filtered(
                lambda line: line.product_id == self.product
            )
        )

        kitchen_daily.sudo()._sync_approved_stock_events()
        bar_daily.sudo()._sync_approved_stock_events()
        self.assertEqual(len(event.application_ids), 1)
        self.assertEqual(application.daily_line_id.daily_id, kitchen_daily)

    def test_reception_review_waits_for_all_required_stock_sections(self):
        kitchen, bar = self._stock_sections(assign_product=False)
        closing = self._closing()
        closing.action_submit()
        Daily = self.env["restaurant.stock.daily"].with_user(self.stockkeeper)
        kitchen_daily = Daily.create({
            "branch_id": self.branch.id,
            "section_id": kitchen.id,
            "stock_date": self.service_date,
        })
        bar_daily = Daily.create({
            "branch_id": self.branch.id,
            "section_id": bar.id,
            "stock_date": self.service_date,
        })
        for daily in (kitchen_daily, bar_daily):
            daily.action_confirm_opening()
            self._confirm_zero_catalog_lines(daily)
            daily.action_submit_closing()

        kitchen_daily.with_user(self.manager).action_close()
        with self.assertRaisesRegex(ValidationError, "Still waiting for"):
            closing.with_user(self.manager).action_confirm()
        self.assertEqual(closing.state, "manager_review")

        bar_daily.with_user(self.manager).action_close()
        closing.with_user(self.manager).action_confirm()
        self.assertEqual(closing.state, "confirmed")
        self.assertEqual(closing.stock_section_readiness, "ready")

    def test_sold_losses_and_missing_post_once_with_explicit_mapping(self):
        kitchen, _bar = self._stock_sections()
        closing = self._closing()
        self._reception_sale(
            closing,
            variant=self.full_variant,
            menu_qty=3.0,
        )
        complimentary = self._approve(
            self._event("complimentary", self.half_variant, 2.0),
            prepared=True,
        )
        cancellation = self._approve(
            self._event("cancellation", self.half_variant, 1.0),
            prepared=True,
        )
        self.env["stock.quant"]._update_available_quantity(
            self.product,
            self.warehouse.lot_stock_id,
            20.0,
        )
        daily = self.env["restaurant.stock.daily"].with_user(
            self.stockkeeper
        ).create({
            "branch_id": self.branch.id,
            "section_id": kitchen.id,
            "stock_date": self.service_date,
        })
        line = daily.line_ids.filtered(
            lambda current: current.product_id == self.product
        )
        self.assertEqual(line.operation_mode, "menu")
        self.assertEqual(line.sold_qty, 6.0)
        self.assertEqual(line.auto_complimentary_qty, 2.0)
        self.assertEqual(line.auto_cancellation_waste_qty, 1.0)

        line.with_user(self.stockkeeper).write({
            "total_waste_qty": 3.0,
            "damaged_qty": 1.0,
            "actual_closing_qty": 6.0,
            "actual_closing_state": "counted",
        })
        self.assertEqual(line.waste_qty, 2.0)
        self.assertEqual(line.total_waste_qty, 3.0)
        self.assertEqual(line.expected_closing_qty, 8.0)
        self.assertEqual(line.missing_qty, 2.0)
        self.assertEqual(line.surplus_qty, 0.0)

        daily.action_confirm_opening()
        self._confirm_zero_catalog_lines(daily)
        daily.action_submit_closing()
        daily.with_user(self.manager).action_close()
        self.assertEqual(line.sold_move_id.quantity, 6.0)
        self.assertEqual(line.consumption_move_id.quantity, 2.0)
        self.assertEqual(line.waste_move_id.quantity, 2.0)
        self.assertEqual(line.cancellation_waste_move_id.quantity, 1.0)
        self.assertEqual(line.damaged_move_id.quantity, 1.0)
        self.assertEqual(line.missing_move_id.quantity, 2.0)
        self.assertEqual(
            self.env["stock.quant"]._get_available_quantity(
                self.product,
                self.warehouse.lot_stock_id,
            ),
            6.0,
        )
        move_ids = (
            line.sold_move_id
            | line.consumption_move_id
            | line.waste_move_id
            | line.cancellation_waste_move_id
            | line.damaged_move_id
            | line.missing_move_id
        ).ids
        with self.assertRaises(UserError):
            daily.with_user(self.manager).action_close()
        self.assertEqual(
            self.env["stock.move"].search_count([("id", "in", move_ids)]),
            len(move_ids),
        )
        self.assertEqual(complimentary.application_ids.daily_line_id, line)
        self.assertEqual(cancellation.application_ids.daily_line_id, line)

    def test_reception_sold_menu_feeds_section_daily_once(self):
        kitchen, bar = self._stock_sections()
        self.env["stock.quant"]._update_available_quantity(
            self.product,
            self.warehouse.lot_stock_id,
            20.0,
        )
        closing = self._closing(reported_total_sales=735.0)
        sale = self._reception_sale(
            closing,
            variant=self.full_variant,
            menu_qty=3.0,
        )
        self.assertEqual(sale.component_ids.quantity_per_menu_unit, 2.0)
        self.assertEqual(sale.component_ids.sold_qty, 6.0)
        self.assertEqual(sale.component_ids.section_id, kitchen)
        self.assertEqual(closing.reported_total_sales, 735.0)

        Daily = self.env["restaurant.stock.daily"].with_user(self.stockkeeper)
        kitchen_daily = Daily.create({
            "branch_id": self.branch.id,
            "section_id": kitchen.id,
            "stock_date": self.service_date,
        })
        bar_daily = Daily.create({
            "branch_id": self.branch.id,
            "section_id": bar.id,
            "stock_date": self.service_date,
        })
        kitchen_line = kitchen_daily.line_ids.filtered(
            lambda line: line.product_id == self.product
        )
        self.assertEqual(kitchen_line.sold_qty, 6.0)
        self.assertEqual(kitchen_daily.reception_sold_menu_line_ids, sale)
        self.assertFalse(bar_daily.reception_sold_menu_line_ids)
        self.assertFalse(bar_daily.line_ids.filtered(
            lambda line: line.product_id == self.product
        ))

        sale.write({"menu_qty": 4.0})
        self.assertEqual(sale.component_ids.sold_qty, 8.0)
        self.assertEqual(kitchen_line.sold_qty, 8.0)
        self.assertEqual(len(sale.component_ids), 1)
        with self.assertRaises(AccessError):
            self.env["restaurant.stock.daily.sale"].with_user(
                self.stockkeeper
            ).create({
                "daily_id": kitchen_daily.id,
                "variant_id": self.full_variant.id,
                "menu_qty": 1.0,
            })

        sale.unlink()
        self.assertEqual(kitchen_line.sold_qty, 0.0)
        self.assertFalse(kitchen_daily.reception_sold_menu_line_ids)

    def test_reception_enters_one_flavor_row_with_combined_portion_counts(self):
        kitchen, _bar = self._stock_sections()
        setup = self.env["restaurant.menu.mapping.profile"].create({
            "name": "Synthetic Reception Combined Portions",
            "branch_id": self.branch.id,
            "category_id": self.category.id,
            "stock_product_id": self.product.id,
            "size_applicable": True,
            "item_ids": [Command.create({
                "name": "Synthetic Reception Plain Flavor",
            })],
        })
        flavor = setup.item_ids
        closing = self._closing()
        sale = self._reception_flavor_sale(
            closing,
            flavor,
            full_count=5.0,
            half_count=7.0,
        )

        self.assertFalse(sale.variant_id)
        self.assertEqual(sale.menu_item_id, flavor)
        self.assertEqual(len(closing.sold_menu_line_ids), 1)
        self.assertEqual(len(sale.component_ids), 2)
        self.assertEqual(sum(sale.component_ids.mapped("sold_qty")), 8.5)

        self.env["stock.quant"]._update_available_quantity(
            self.product,
            self.warehouse.lot_stock_id,
            20.0,
        )
        daily = self.env["restaurant.stock.daily"].with_user(
            self.stockkeeper
        ).create({
            "branch_id": self.branch.id,
            "section_id": kitchen.id,
            "stock_date": self.service_date,
        })
        line = daily.line_ids.filtered(
            lambda current: current.product_id == self.product
        )
        self.assertEqual(line.sold_qty, 8.5)

        sale.write({"half_count": 3.0})
        self.assertEqual(sum(sale.component_ids.mapped("sold_qty")), 6.5)
        self.assertEqual(line.sold_qty, 6.5)
        with self.assertRaises(ValidationError):
            self._reception_flavor_sale(
                closing,
                flavor,
                full_count=1.0,
            )

    def test_reception_standard_flavor_uses_one_count_and_one_stock_unit(self):
        kitchen, _bar = self._stock_sections()
        setup = self.env["restaurant.menu.mapping.profile"].create({
            "name": "Synthetic Reception Standard",
            "branch_id": self.branch.id,
            "category_id": self.category.id,
            "stock_product_id": self.product.id,
            "item_ids": [Command.create({
                "name": "Synthetic Reception Standard Flavor",
            })],
        })
        flavor = setup.item_ids
        self.assertEqual(setup.standard_stock_qty, 1.0)
        closing = self._closing()
        sale = self._reception_flavor_sale(
            closing,
            flavor,
            standard_count=4.0,
        )
        self.assertEqual(len(sale.component_ids), 1)
        self.assertEqual(sale.component_ids.sold_qty, 4.0)

        self.env["stock.quant"]._update_available_quantity(
            self.product,
            self.warehouse.lot_stock_id,
            20.0,
        )
        daily = self.env["restaurant.stock.daily"].with_user(
            self.stockkeeper
        ).create({
            "branch_id": self.branch.id,
            "section_id": kitchen.id,
            "stock_date": self.service_date,
        })
        self.assertEqual(
            daily.line_ids.filtered(
                lambda current: current.product_id == self.product
            ).sold_qty,
            4.0,
        )
        with self.assertRaises(ValidationError):
            sale.write({"half_count": 1.0})

    def test_reception_sold_menu_routes_multi_component_by_section(self):
        kitchen, bar = self._stock_sections()
        bar_product = self.env["product.product"].create({
            "name": "Synthetic Bar Recipe Product",
            "is_storable": True,
        })
        self.env["restaurant.stock.section.product"].with_user(
            self.stockkeeper
        ).create({
            "section_id": bar.id,
            "product_id": bar_product.id,
        })
        self.env["stock.quant"]._update_available_quantity(
            self.product,
            self.warehouse.lot_stock_id,
            10.0,
        )
        self.env["stock.quant"]._update_available_quantity(
            bar_product,
            self.warehouse.lot_stock_id,
            10.0,
        )
        self.env["restaurant.menu.stock.mapping"].create({
            "variant_id": self.full_variant.id,
            "stock_product_id": bar_product.id,
            "stock_qty": 0.5,
            "stock_uom_id": bar_product.uom_id.id,
            "note": "Synthetic bar component",
        })
        closing = self._closing()
        sale = self._reception_sale(closing, menu_qty=2.0)
        self.assertEqual(set(sale.component_ids.section_id.ids), {kitchen.id, bar.id})

        Daily = self.env["restaurant.stock.daily"].with_user(self.stockkeeper)
        kitchen_daily = Daily.create({
            "branch_id": self.branch.id,
            "section_id": kitchen.id,
            "stock_date": self.service_date,
        })
        bar_daily = Daily.create({
            "branch_id": self.branch.id,
            "section_id": bar.id,
            "stock_date": self.service_date,
        })
        self.assertEqual(
            kitchen_daily.line_ids.filtered(
                lambda line: line.product_id == self.product
            ).sold_qty,
            4.0,
        )
        self.assertEqual(
            bar_daily.line_ids.filtered(
                lambda line: line.product_id == bar_product
            ).sold_qty,
            1.0,
        )
        self.assertEqual(kitchen_daily.reception_sold_menu_line_ids, sale)
        self.assertEqual(bar_daily.reception_sold_menu_line_ids, sale)

    def test_reception_sold_menu_requires_mapping_and_draft_closing(self):
        closing = self._closing()
        with self.assertRaisesRegex(ValidationError, "Missing Mapping"):
            self._reception_sale(closing, variant=self.unmapped_variant)

        sale = self._reception_sale(closing, menu_qty=2.0)
        closing.action_submit()
        for operation in (
            lambda: sale.write({"menu_qty": 3.0}),
            lambda: sale.unlink(),
            lambda: self._reception_sale(
                closing,
                variant=self.half_variant,
                menu_qty=1.0,
            ),
        ):
            with self.assertRaises(AccessError), self.env.cr.savepoint():
                operation()

    def test_reopened_reception_sale_cannot_change_closed_daily_stock(self):
        kitchen, _bar = self._stock_sections()
        self.env["stock.quant"]._update_available_quantity(
            self.product,
            self.warehouse.lot_stock_id,
            10.0,
        )
        closing = self._closing()
        sale = self._reception_sale(closing, menu_qty=2.0)
        daily = self.env["restaurant.stock.daily"].with_user(
            self.stockkeeper
        ).create({
            "branch_id": self.branch.id,
            "section_id": kitchen.id,
            "stock_date": self.service_date,
        })
        line = daily.line_ids.filtered(
            lambda current: current.product_id == self.product
        )
        line.write({"actual_closing_qty": 6.0})
        daily.action_confirm_opening()
        self._confirm_zero_catalog_lines(daily)
        daily.action_submit_closing()
        daily.with_user(self.manager).action_close()

        closing.action_submit()
        manager_closing = closing.with_user(self.manager)
        manager_closing.correction_reason = "Correct Reception sold portions."
        manager_closing.action_return_to_draft()
        with self.assertRaisesRegex(ValidationError, "already closed"):
            sale.write({"menu_qty": 3.0})
        with self.assertRaisesRegex(ValidationError, "already closed"):
            manager_closing.with_user(self.reception).action_cancel()
        self.assertEqual(line.sudo().sold_move_id.state, "done")
        self.assertEqual(line.sudo().sold_move_id.quantity, 4.0)

    def test_reception_sold_menu_views_have_single_entry_owner(self):
        reception_arch = self.env.ref(
            "restaurant_stock_events.restaurant_daily_closing_view_form_events"
        ).arch_db
        daily_arch = self.env.ref(
            "restaurant_stock_events.restaurant_stock_daily_view_form_events"
        ).arch_db
        self.assertIn('name="sold_menu_line_ids"', reception_arch)
        self.assertIn('name="menu_item_id"', reception_arch)
        self.assertIn('string="Menu Item"', reception_arch)
        self.assertNotIn('string="Flavor"', reception_arch)
        self.assertIn("('mapping_profile_id', '!=', False)", reception_arch)
        self.assertIn("('stock_product_id', '!=', False)", reception_arch)
        self.assertIn('name="full_count"', reception_arch)
        self.assertIn('name="half_count"', reception_arch)
        self.assertIn('name="standard_count"', reception_arch)
        self.assertIn('name="branch_id" column_invisible="True"', reception_arch)
        self.assertNotIn("parent.branch_id", reception_arch)
        self.assertIn('name="reception_sold_menu_line_ids"', daily_arch)
        self.assertIn('string="Menu Item"', daily_arch)
        self.assertNotIn('string="Flavor"', daily_arch)
        self.assertIn("('mapping_profile_id', '!=', False)", daily_arch)
        self.assertIn('create="0" edit="0" delete="0"', daily_arch)
        self.assertNotIn('name="sold_menu_line_ids"', daily_arch)

    def test_menu_item_search_more_uses_product_flavor_display_name(self):
        list_view = self.env.ref(
            "restaurant_stock_events.restaurant_menu_item_selection_view_list"
        )
        search_view = self.env.ref(
            "restaurant_stock_events.restaurant_menu_item_selection_view_search"
        )
        resolved_list = self.env["restaurant.menu.item"].with_user(
            self.reception
        ).get_view(view_type="list")
        resolved_search = self.env["restaurant.menu.item"].with_user(
            self.reception
        ).get_view(view_type="search")

        self.assertEqual(resolved_list["id"], list_view.id)
        self.assertEqual(resolved_search["id"], search_view.id)
        self.assertIn('name="display_name"', resolved_list["arch"])
        self.assertIn('string="Menu Item"', resolved_list["arch"])
        self.assertIn('name="display_name"', resolved_search["arch"])

    def test_reception_menu_item_dropdown_is_configured_and_branch_scoped(self):
        setup = self.env["restaurant.menu.mapping.profile"].create({
            "name": "Synthetic Reception Dropdown Setup",
            "branch_id": self.branch.id,
            "category_id": self.category.id,
            "stock_product_id": self.product.id,
            "item_ids": [Command.create({
                "name": "Synthetic Visible Reception Flavor",
            })],
        })
        closing = self._closing()
        Sale = self.env["restaurant.reception.sold.menu.line"].with_user(
            self.reception
        ).with_context(default_closing_id=closing.id)
        draft_line = Sale.new(Sale.default_get(["closing_id"]))
        self.assertEqual(draft_line.branch_id, self.branch)

        results = self.env["restaurant.menu.item"].with_user(
            self.reception
        ).web_name_search(
            "",
            {"display_name": {}},
            domain=[
                ("branch_id", "=", draft_line.branch_id.id),
                ("mapping_profile_id", "!=", False),
                ("stock_product_id", "!=", False),
            ],
        )
        result_by_id = {result["id"]: result["display_name"] for result in results}
        self.assertIn(setup.item_ids.id, result_by_id)
        self.assertIn(self.product.display_name, result_by_id[setup.item_ids.id])
        self.assertIn(setup.item_ids.name, result_by_id[setup.item_ids.id])
        self.assertNotIn(self.sized_item.id, result_by_id)
        menu_item_field = self.env[
            "restaurant.reception.sold.menu.line"
        ]._fields["menu_item_id"]
        self.assertEqual(menu_item_field.comodel_name, "restaurant.menu.item")
        self.assertEqual(menu_item_field.string, "Menu Item")

    def test_sold_snapshot_is_section_scoped_and_mapping_based(self):
        kitchen, bar = self._stock_sections()
        closing = self._closing()
        sale = self._reception_sale(
            closing,
            variant=self.full_variant,
            menu_qty=2.0,
        )
        self.env["stock.quant"]._update_available_quantity(
            self.product,
            self.warehouse.lot_stock_id,
            10.0,
        )
        Daily = self.env["restaurant.stock.daily"].with_user(self.stockkeeper)
        kitchen_daily = Daily.create({
            "branch_id": self.branch.id,
            "section_id": kitchen.id,
            "stock_date": self.service_date,
        })
        bar_daily = Daily.create({
            "branch_id": self.branch.id,
            "section_id": bar.id,
            "stock_date": self.service_date,
        })
        self.assertEqual(sale.component_ids.quantity_per_menu_unit, 2.0)
        kitchen_line = kitchen_daily.line_ids.filtered(
            lambda line: line.product_id == self.product
        )
        self.assertEqual(kitchen_line.sold_qty, 4.0)
        self.assertEqual(sale.component_ids.section_id, kitchen)
        self.assertFalse(bar_daily.reception_sold_menu_line_ids)
        self.assertFalse(bar_daily.line_ids.filtered(
            lambda line: line.product_id == self.product
        ))
        with self.assertRaisesRegex(ValidationError, "Missing Mapping"):
            self._reception_sale(
                closing,
                variant=self.unmapped_variant,
                menu_qty=1.0,
            )

    def test_surplus_never_reduces_sold_or_creates_missing_move(self):
        kitchen, _bar = self._stock_sections()
        closing = self._closing()
        self._reception_sale(
            closing,
            variant=self.half_variant,
            menu_qty=2.0,
        )
        self.env["stock.quant"]._update_available_quantity(
            self.product,
            self.warehouse.lot_stock_id,
            10.0,
        )
        daily = self.env["restaurant.stock.daily"].with_user(
            self.stockkeeper
        ).create({
            "branch_id": self.branch.id,
            "section_id": kitchen.id,
            "stock_date": self.service_date,
        })
        line = daily.line_ids.filtered(
            lambda current: current.product_id == self.product
        )
        line.with_user(self.stockkeeper).write({
            "actual_closing_qty": 9.0,
            "actual_closing_state": "counted",
        })
        self.assertEqual(line.sold_qty, 2.0)
        self.assertEqual(line.expected_closing_qty, 8.0)
        self.assertEqual(line.missing_qty, 0.0)
        self.assertEqual(line.surplus_qty, 1.0)
        daily.action_confirm_opening()
        self._confirm_zero_catalog_lines(daily)
        daily.action_submit_closing()
        daily.with_user(self.manager).action_close()
        self.assertEqual(line.sold_move_id.quantity, 2.0)
        self.assertFalse(line.missing_move_id)

    def test_catalog_requires_explicit_compatible_mapping(self):
        with self.assertRaises(ValidationError):
            self.env["restaurant.menu.variant"].create({
                "item_id": self.sized_item.id,
                "size": "standard",
            })
        with self.assertRaises(ValidationError):
            self.env["restaurant.menu.stock.mapping"].create({
                "variant_id": self.unmapped_variant.id,
                "stock_product_id": self.product.id,
                "stock_qty": 0.0,
                "stock_uom_id": self.product.uom_id.id,
            })

    def test_menu_setup_creates_flavors_and_portions_in_one_save(self):
        setup = self.env["restaurant.menu.mapping.profile"].with_user(
            self.manager
        ).create({
            "branch_id": self.branch.id,
            "category_id": self.category.id,
            "stock_product_id": self.product.id,
            "size_applicable": True,
            "half_stock_qty": 0.75,
            "full_stock_qty": 1.5,
            "item_ids": [
                Command.create({"name": "Synthetic Setup Flavor A"}),
                Command.create({"name": "Synthetic Setup Flavor B"}),
            ],
        })
        flavors = setup.item_ids
        self.assertEqual(set(flavors.mapped("name")), {
            "Synthetic Setup Flavor A",
            "Synthetic Setup Flavor B",
        })
        for flavor in flavors:
            self.assertEqual(set(flavor.variant_ids.mapped("size")), {"half", "full"})
            self.assertEqual(flavor.mapping_profile_id, setup)
            self.assertEqual(
                flavor.variant_ids.mapping_ids.mapped("stock_product_id"),
                self.product,
            )
            self.assertEqual(flavor.variant_ids.mapping_ids.mapped("setup_id"), setup)
        counts_before = (
            len(flavors.variant_ids),
            len(flavors.variant_ids.mapping_ids),
        )
        setup.with_user(self.manager).write({"half_stock_qty": 0.8})
        self.assertEqual(
            counts_before,
            (
                len(flavors.variant_ids),
                len(flavors.variant_ids.mapping_ids),
            ),
        )
        half_mappings = flavors.variant_ids.filtered(
            lambda variant: variant.size == "half"
        ).mapping_ids
        self.assertEqual(set(half_mappings.mapped("stock_qty")), {0.8})
        self.assertFalse(setup.line_ids)
        self.assertNotIn(
            "restaurant.menu.mapping.profile.apply.wizard",
            self.env.registry,
        )

    def test_menu_setup_defaults_normal_portions_without_overwriting_exceptions(self):
        setup = self.env["restaurant.menu.mapping.profile"].create({
            "name": "Synthetic Normal Portion Defaults",
            "branch_id": self.branch.id,
            "category_id": self.category.id,
            "stock_product_id": self.product.id,
            "size_applicable": True,
            "item_ids": [Command.create({
                "name": "Synthetic Normal Portion Flavor",
            })],
        })
        self.assertEqual(setup.half_stock_qty, 0.5)
        self.assertEqual(setup.full_stock_qty, 1.0)
        self.assertEqual(
            set(
                setup.item_ids.variant_ids.filtered(
                    lambda variant: variant.size == "half"
                ).mapping_ids.mapped("stock_qty")
            ),
            {0.5},
        )
        self.assertEqual(
            set(
                setup.item_ids.variant_ids.filtered(
                    lambda variant: variant.size == "full"
                ).mapping_ids.mapped("stock_qty")
            ),
            {1.0},
        )

        exception = self.env["restaurant.menu.mapping.profile"].create({
            "name": "Synthetic Verified Portion Exception",
            "branch_id": self.branch.id,
            "category_id": self.category.id,
            "stock_product_id": self.product.id,
            "size_applicable": True,
            "half_stock_qty": 0.4,
            "full_stock_qty": 0.75,
            "item_ids": [Command.create({
                "name": "Synthetic Verified Portion Flavor",
            })],
        })
        self.assertEqual(exception.half_stock_qty, 0.4)
        self.assertEqual(exception.full_stock_qty, 0.75)

        standard = self.env["restaurant.menu.mapping.profile"].create({
            "name": "Synthetic Standard Portion Default",
            "branch_id": self.branch.id,
            "category_id": self.category.id,
            "stock_product_id": self.product.id,
            "item_ids": [Command.create({
                "name": "Synthetic Standard Portion Flavor",
            })],
        })
        self.assertEqual(standard.standard_stock_qty, 1.0)
        self.assertEqual(standard.item_ids.variant_ids.mapping_ids.stock_qty, 1.0)

    def test_menu_setup_name_can_be_reused_after_archive_only(self):
        Setup = self.env["restaurant.menu.mapping.profile"]
        archived = Setup.create({
            "name": "Synthetic Reusable Archived Setup",
            "branch_id": self.branch.id,
            "category_id": self.category.id,
            "stock_product_id": self.product.id,
            "standard_stock_qty": 1.0,
            "item_ids": [Command.create({
                "name": "Synthetic Preserved Historical Flavor",
            })],
        })
        historical_items = archived.item_ids
        historical_variants = historical_items.variant_ids
        historical_mappings = historical_variants.mapping_ids

        archived.unlink()
        self.assertFalse(archived.active)
        self.assertFalse(historical_items.active)
        self.assertFalse(historical_variants.active)

        replacement = Setup.create({
            "name": archived.name,
            "branch_id": self.branch.id,
            "category_id": self.category.id,
            "stock_product_id": self.product.id,
            "standard_stock_qty": 1.0,
            "item_ids": [Command.create({
                "name": historical_items.name,
            })],
        })
        self.assertTrue(replacement.active)
        self.assertNotEqual(replacement, archived)
        self.assertTrue(historical_items.exists())
        self.assertTrue(historical_variants.exists())
        self.assertTrue(historical_mappings.exists())

        with self.assertRaises(ValidationError):
            Setup.create({
                "name": archived.name,
                "branch_id": self.branch.id,
                "category_id": self.category.id,
                "stock_product_id": self.product.id,
                "standard_stock_qty": 1.0,
                "item_ids": [Command.create({
                    "name": "Synthetic Forbidden Active Duplicate Flavor",
                })],
            })

        with self.assertRaises(ValidationError):
            archived.write({"active": True})

    def test_menu_setup_rejects_active_flavor_duplicate_without_merging(self):
        Setup = self.env["restaurant.menu.mapping.profile"]
        existing = Setup.create({
            "name": "Synthetic Existing Flavor Owner",
            "branch_id": self.branch.id,
            "category_id": self.category.id,
            "stock_product_id": self.product.id,
            "item_ids": [Command.create({
                "name": "Synthetic Active Shared Flavor",
            })],
        })
        existing_item = existing.item_ids
        existing_mappings = existing_item.variant_ids.mapping_ids

        with self.assertRaisesRegex(ValidationError, "already active"):
            Setup.create({
                "name": "Synthetic Duplicate Flavor Attempt",
                "branch_id": self.branch.id,
                "category_id": self.category.id,
                "stock_product_id": self.product.id,
                "item_ids": [Command.create({
                    "name": existing_item.name,
                })],
            })

        self.assertTrue(existing.active)
        self.assertTrue(existing_item.active)
        self.assertTrue(existing_mappings.exists())
        self.assertFalse(Setup.search([
            ("name", "=", "Synthetic Duplicate Flavor Attempt"),
        ]))

    def test_same_flavor_name_is_separate_for_different_stock_products(self):
        other_product = self.env["product.product"].create({
            "name": "Synthetic Other Parent Product",
            "is_storable": True,
        })
        Setup = self.env["restaurant.menu.mapping.profile"]
        first = Setup.create({
            "name": "Synthetic First Parent Setup",
            "branch_id": self.branch.id,
            "category_id": self.category.id,
            "stock_product_id": self.product.id,
            "item_ids": [Command.create({
                "name": "Synthetic Shared Flavor Name",
            })],
        })
        second = Setup.create({
            "name": "Synthetic Second Parent Setup",
            "branch_id": self.branch.id,
            "category_id": self.category.id,
            "stock_product_id": other_product.id,
            "item_ids": [Command.create({
                "name": "Synthetic Shared Flavor Name",
            })],
        })

        self.assertNotEqual(first.item_ids, second.item_ids)
        self.assertEqual(first.flavor_ids, second.flavor_ids)
        self.assertEqual(first.item_ids.flavor_id, second.item_ids.flavor_id)
        self.assertEqual(first.item_ids.stock_product_id, self.product)
        self.assertEqual(second.item_ids.stock_product_id, other_product)
        self.assertIn(self.product.display_name, first.item_ids.display_name)
        self.assertIn(other_product.display_name, second.item_ids.display_name)
        self.assertEqual(
            first.item_ids.variant_ids.mapping_ids.stock_product_id,
            self.product,
        )
        self.assertEqual(
            second.item_ids.variant_ids.mapping_ids.stock_product_id,
            other_product,
        )

    def test_reusable_flavor_checklist_archives_and_reactivates_same_items(self):
        Flavor = self.env["restaurant.menu.flavor"].with_user(self.manager)
        flavor_a, flavor_b = Flavor.create([
            {"name": "Synthetic Reusable A", "branch_id": self.branch.id},
            {"name": "Synthetic Reusable B", "branch_id": self.branch.id},
        ])
        setup = self.env["restaurant.menu.mapping.profile"].with_user(
            self.manager
        ).create({
            "name": "Synthetic Reusable Checklist",
            "branch_id": self.branch.id,
            "category_id": self.category.id,
            "stock_product_id": self.product.id,
            "flavor_ids": [Command.set((flavor_a | flavor_b).ids)],
        })
        all_items = self.env["restaurant.menu.item"].with_context(
            active_test=False,
        ).search([("mapping_profile_id", "=", setup.id)])
        item_b = all_items.filtered(lambda item: item.flavor_id == flavor_b)
        item_b_variant_ids = item_b.variant_ids.ids
        item_b_mapping_ids = item_b.variant_ids.mapping_ids.ids

        setup.with_user(self.manager).write({
            "flavor_ids": [Command.set(flavor_a.ids)],
        })
        self.assertEqual(setup.item_ids.flavor_id, flavor_a)
        self.assertFalse(item_b.active)
        self.assertFalse(any(item_b.variant_ids.mapped("active")))
        self.assertTrue(item_b.variant_ids.mapping_ids.exists())

        setup.with_user(self.manager).write({
            "flavor_ids": [Command.set((flavor_a | flavor_b).ids)],
        })
        self.assertTrue(item_b.active)
        self.assertEqual(item_b.variant_ids.ids, item_b_variant_ids)
        self.assertEqual(item_b.variant_ids.mapping_ids.ids, item_b_mapping_ids)

    def test_menu_setup_without_flavor_defaults_to_plain(self):
        setup = self.env["restaurant.menu.mapping.profile"].with_user(
            self.manager
        ).create({
            "name": "Synthetic Plain Default",
            "branch_id": self.branch.id,
            "category_id": self.category.id,
            "stock_product_id": self.product.id,
        })
        self.assertEqual(setup.flavor_ids.name.casefold(), "plain")
        self.assertEqual(setup.item_ids.flavor_id, setup.flavor_ids)
        self.assertEqual(setup.item_ids.variant_ids.mapped("size"), ["standard"])
        self.assertEqual(setup.item_ids.variant_ids.mapping_ids.stock_qty, 1.0)

    def test_menu_setup_view_uses_reusable_flavor_tags_and_checkboxes(self):
        arch = self.env.ref(
            "restaurant_stock_events.restaurant_menu_mapping_profile_view_form"
        ).arch_db
        self.assertIn('id="flavor_quick_add"', arch)
        self.assertIn('widget="many2many_tags"', arch)
        self.assertIn('id="flavor_checklist"', arch)
        self.assertIn('widget="many2many_checkboxes"', arch)
        self.assertNotIn('<list editable="bottom" string="Flavors">', arch)
        self.assertIn('name="action_manage_flavors"', arch)
        self.assertIn('string="Manage Flavors"', arch)

    def test_manage_flavors_action_is_branch_scoped_and_recoverable(self):
        setup = self.env["restaurant.menu.mapping.profile"].with_user(
            self.manager
        ).create({
            "name": "Synthetic Flavor Manager Setup",
            "branch_id": self.branch.id,
            "category_id": self.category.id,
            "stock_product_id": self.product.id,
        })
        action = setup.action_manage_flavors()

        self.assertEqual(
            action["res_model"],
            "restaurant.menu.flavor",
        )
        self.assertEqual(
            action["domain"],
            [("branch_id", "=", self.branch.id)],
        )
        self.assertEqual(
            action["context"]["default_branch_id"],
            self.branch.id,
        )
        search_arch = self.env.ref(
            "restaurant_stock_events.restaurant_menu_flavor_view_search"
        ).arch_db
        form_arch = self.env.ref(
            "restaurant_stock_events.restaurant_menu_flavor_view_form"
        ).arch_db
        self.assertIn('string="Archived Flavors"', search_arch)
        self.assertIn("historical sales", form_arch)

    def test_archived_flavor_must_be_restored_not_duplicated(self):
        Flavor = self.env["restaurant.menu.flavor"].with_user(self.manager)
        flavor = Flavor.create({
            "name": "Synthetic Recoverable Flavor",
            "branch_id": self.branch.id,
        })
        flavor.unlink()
        self.assertFalse(flavor.active)

        with self.assertRaisesRegex(ValidationError, "Archived Flavors"):
            Flavor.create({
                "name": "synthetic recoverable flavor",
                "branch_id": self.branch.id,
            })

        flavor.write({"active": True})
        self.assertTrue(flavor.active)

    def test_flavor_rename_keeps_existing_menu_item_snapshot(self):
        Flavor = self.env["restaurant.menu.flavor"].with_user(self.manager)
        flavor = Flavor.create({
            "name": "Synthetic Original Catalog Name",
            "branch_id": self.branch.id,
        })
        setup = self.env["restaurant.menu.mapping.profile"].with_user(
            self.manager
        ).create({
            "name": "Synthetic Flavor Rename Setup",
            "branch_id": self.branch.id,
            "category_id": self.category.id,
            "stock_product_id": self.product.id,
            "flavor_ids": [Command.set(flavor.ids)],
        })
        item_name = setup.item_ids.name

        flavor.write({"name": "Synthetic Renamed Catalog Name"})

        self.assertEqual(setup.item_ids.name, item_name)
        self.assertEqual(flavor.name, "Synthetic Renamed Catalog Name")

    def test_menu_setup_rejects_non_positive_quantities_and_cross_branch(self):
        with self.assertRaises(ValidationError):
            self.env["restaurant.menu.mapping.profile"].create({
                "branch_id": self.branch.id,
                "category_id": self.category.id,
                "stock_product_id": self.product.id,
                "size_applicable": True,
                "half_stock_qty": 0.8,
                "full_stock_qty": 0.0,
                "item_ids": [
                    Command.create({"name": "Synthetic Incomplete Flavor"}),
                ],
            })

        other_category = self.env["restaurant.menu.category"].create({
            "name": "Synthetic Setup Other Branch",
            "branch_id": self.other_branch.id,
        })
        with self.assertRaises(ValidationError):
            self.env["restaurant.menu.mapping.profile"].create({
                "branch_id": self.branch.id,
                "category_id": other_category.id,
                "stock_product_id": self.product.id,
                "standard_stock_qty": 1.0,
                "item_ids": [
                    Command.create({"name": "Synthetic Cross Branch Flavor"}),
                ],
            })

        setup = self.env["restaurant.menu.mapping.profile"].create({
            "branch_id": self.branch.id,
            "category_id": self.category.id,
            "stock_product_id": self.product.id,
            "standard_stock_qty": 1.0,
            "item_ids": [Command.create({"name": "Synthetic Ready Flavor"})],
        })

        self.assertFalse(
            self.env["restaurant.menu.mapping.profile"].with_user(
                self.other_manager
            ).search([("id", "=", setup.id)])
        )
        with self.assertRaises(AccessError):
            setup.with_user(self.central_storekeeper).write({
                "standard_stock_qty": 9.0,
            })

    def test_integrated_menu_item_five_flavors_share_stock_conversion(self):
        flavor_names = [
            "Synthetic Fish Lemon",
            "Synthetic Fish Spicy",
            "Synthetic Fish Garlic",
            "Synthetic Fish Herb",
            "Synthetic Fish Classic",
        ]
        menu_item = self.env[
            "restaurant.menu.mapping.profile"
        ].with_user(self.manager).create({
            "name": "Synthetic Fish",
            "branch_id": self.branch.id,
            "category_id": self.category.id,
            "stock_product_id": self.product.id,
            "stock_uom_id": self.product.uom_id.id,
            "size_applicable": True,
            "half_stock_qty": 0.4,
            "full_stock_qty": 0.75,
            "item_ids": [
                Command.create({"name": flavor_name})
                for flavor_name in flavor_names
            ],
        })

        self.assertTrue(menu_item.configuration_complete)
        self.assertEqual(len(menu_item.item_ids), 5)
        self.assertEqual(set(menu_item.item_ids.mapped("name")), set(flavor_names))
        for flavor in menu_item.item_ids:
            self.assertEqual(set(flavor.variant_ids.mapped("size")), {"half", "full"})
            self.assertEqual(
                flavor.variant_ids.mapping_ids.mapped("stock_product_id"),
                self.product,
            )
            half = flavor.variant_ids.filtered(lambda variant: variant.size == "half")
            full = flavor.variant_ids.filtered(lambda variant: variant.size == "full")
            self.assertEqual(half.mapping_ids.stock_qty, 0.4)
            self.assertEqual(full.mapping_ids.stock_qty, 0.75)

        exception_flavor = menu_item.item_ids[0]
        exception_half = exception_flavor.variant_ids.filtered(
            lambda variant: variant.size == "half"
        ).mapping_ids
        exception_half.with_user(self.manager).write({"stock_qty": 0.45})
        menu_item.with_user(self.manager).write({"half_stock_qty": 0.5})
        self.assertEqual(exception_half.stock_qty, 0.5)
        inherited_half = menu_item.item_ids[1].variant_ids.filtered(
            lambda variant: variant.size == "half"
        ).mapping_ids
        self.assertEqual(inherited_half.stock_qty, 0.5)

        kitchen, _bar = self._stock_sections()
        self.env["stock.quant"]._update_available_quantity(
            self.product,
            self.warehouse.lot_stock_id,
            10.0,
        )
        daily = self.env["restaurant.stock.daily"].with_user(
            self.stockkeeper
        ).create({
            "branch_id": self.branch.id,
            "section_id": kitchen.id,
            "stock_date": self.service_date,
        })
        closing = self._closing()
        sale = self._reception_sale(
            closing,
            variant=inherited_half.variant_id,
            menu_qty=2.0,
        )
        self.assertEqual(sale.component_ids.quantity_per_menu_unit, 0.5)
        self.assertEqual(sale.component_ids.sold_qty, 1.0)
        self.assertEqual(
            daily.line_ids.filtered(
                lambda line: line.product_id == self.product
            ).sold_qty,
            1.0,
        )

        with self.assertRaises(ValidationError):
            menu_item.with_user(self.manager).write({
                "stock_product_id": self.env["product.product"].create({
                    "name": "Synthetic Replacement Fish",
                    "is_storable": True,
                }).id,
            })

    def test_integrated_menu_requires_positive_size_quantities(self):
        with self.assertRaises(ValidationError):
            self.env["restaurant.menu.mapping.profile"].with_user(
                self.manager
            ).create({
                "name": "Synthetic Incomplete Fish",
                "branch_id": self.branch.id,
                "category_id": self.category.id,
                "stock_product_id": self.product.id,
                    "stock_uom_id": self.product.uom_id.id,
                    "size_applicable": True,
                    "half_stock_qty": 0.0,
                    "full_stock_qty": 0.75,
                "item_ids": [Command.create({"name": "Synthetic Flavor"})],
            })

    def test_branch_manager_owns_only_assigned_branch_menu(self):
        ManagerCategory = self.env["restaurant.menu.category"].with_user(
            self.manager
        )
        category = ManagerCategory.create({
            "name": "Synthetic Manager Category",
            "branch_id": self.branch.id,
        })
        setup = self.env["restaurant.menu.mapping.profile"].with_user(
            self.manager
        ).create({
            "branch_id": self.branch.id,
            "category_id": category.id,
            "stock_product_id": self.product.id,
            "half_stock_qty": 0.5,
            "full_stock_qty": 1.0,
            "size_applicable": True,
            "item_ids": [Command.create({
                "name": "Synthetic Manager Flavor",
            })],
        })
        item = setup.item_ids
        variants = item.variant_ids
        self.assertEqual(category.branch_id, self.branch)
        self.assertEqual(item.branch_id, self.branch)
        self.assertEqual(variants.mapped("branch_id"), self.branch)

        other_category = self.env["restaurant.menu.category"].create({
            "name": "Synthetic Other Branch Category",
            "branch_id": self.other_branch.id,
        })
        self.assertFalse(ManagerCategory.search([
            ("id", "=", other_category.id),
        ]))
        with self.assertRaises(AccessError):
            other_category.with_user(self.manager).write({
                "name": "Cross-Branch Tamper",
            })
        with self.assertRaises(AccessError):
            ManagerCategory.create({
                "name": "Unauthorized Other Branch Category",
                "branch_id": self.other_branch.id,
            })
        with self.assertRaises(AccessError):
            self.env["restaurant.menu.category"].with_user(
                self.reception
            ).create({
                "name": "Reception Must Not Configure",
                "branch_id": self.branch.id,
            })

        reception_categories = self.env["restaurant.menu.category"].with_user(
            self.reception
        ).search([("id", "in", [category.id, other_category.id])])
        self.assertEqual(reception_categories, category)

        menu = self.env.ref(
            "restaurant_stock_events.restaurant_branch_menu_setup_root"
        )
        mapping_menu = self.env.ref(
            "restaurant_stock_events.restaurant_branch_menu_variant_menu"
        )
        integrated_menu = self.env.ref(
            "restaurant_stock_events.restaurant_branch_menu_item_menu"
        )
        profile_menu = self.env.ref(
            "restaurant_stock_events.restaurant_branch_menu_mapping_profile_menu"
        )
        category_menu = self.env.ref(
            "restaurant_stock_events.restaurant_branch_menu_category_menu"
        )
        visible = self.env["ir.ui.menu"].with_user(
            self.manager
        )._visible_menu_ids()
        self.assertIn(menu.id, visible)
        self.assertEqual(
            menu.action,
            self.env.ref(
                "restaurant_stock_events.restaurant_menu_mapping_profile_action"
            ),
        )
        self.assertFalse(integrated_menu.active)
        self.assertFalse(mapping_menu.active)
        self.assertFalse(profile_menu.active)
        self.assertFalse(category_menu.active)
        self.assertNotIn(integrated_menu.id, visible)
        self.assertNotIn(mapping_menu.id, visible)
        self.assertNotIn(profile_menu.id, visible)
        self.assertNotIn(category_menu.id, visible)

    def test_reusable_flavor_catalog_respects_branch_and_role_permissions(self):
        ManagerFlavor = self.env["restaurant.menu.flavor"].with_user(
            self.manager
        )
        flavor = ManagerFlavor.create({
            "name": "Synthetic Manager Catalog Flavor",
            "branch_id": self.branch.id,
        })
        other_flavor = self.env["restaurant.menu.flavor"].create({
            "name": "Synthetic Other Branch Catalog Flavor",
            "branch_id": self.other_branch.id,
        })

        self.assertEqual(
            self.env["restaurant.menu.flavor"].with_user(
                self.reception
            ).search([("id", "in", (flavor | other_flavor).ids)]),
            flavor,
        )
        self.assertFalse(ManagerFlavor.search([("id", "=", other_flavor.id)]))
        with self.assertRaises(AccessError):
            ManagerFlavor.create({
                "name": "Synthetic Cross Branch Catalog Flavor",
                "branch_id": self.other_branch.id,
            })
        with self.assertRaises(AccessError):
            self.env["restaurant.menu.flavor"].with_user(
                self.reception
            ).create({
                "name": "Reception Must Not Configure Flavor",
                "branch_id": self.branch.id,
            })

    def test_manager_menu_setup_owns_linkage_and_central_cannot_configure(self):
        setup = self.env["restaurant.menu.mapping.profile"].with_user(
            self.manager
        ).create({
            "branch_id": self.branch.id,
            "category_id": self.category.id,
            "stock_product_id": self.product.id,
            "standard_stock_qty": 1.25,
            "item_ids": [
                Command.create({"name": "Synthetic Spicy Flavor"}),
                Command.create({"name": "Synthetic Lemon Flavor"}),
            ],
        })
        flavor_a, flavor_b = setup.item_ids
        variant_a, variant_b = setup.item_ids.variant_ids
        CentralCategory = self.env["restaurant.menu.category"].with_user(
            self.central_storekeeper
        )
        CentralItem = self.env["restaurant.menu.item"].with_user(
            self.central_storekeeper
        )
        CentralVariant = self.env["restaurant.menu.variant"].with_user(
            self.central_storekeeper
        )

        for model, values in (
            (CentralCategory, {
                "name": "Unauthorized Central Category",
                "company_id": self.company.id,
                "branch_id": self.branch.id,
            }),
            (CentralItem, {
                "name": "Unauthorized Central Item",
                "category_id": self.category.id,
            }),
            (CentralVariant, {
                "item_id": flavor_a.id,
                "size": "half",
            }),
        ):
            with self.assertRaises(AccessError):
                model.create(values)

        mappings = setup.item_ids.variant_ids.mapping_ids
        setup.with_user(self.manager).write({"standard_stock_qty": 1.75})
        self.assertEqual(set(mappings.mapped("stock_qty")), {1.75})
        self.assertEqual(mappings.mapped("stock_product_id"), self.product)
        self.assertNotEqual(variant_a, variant_b)
        self.assertNotEqual(flavor_a, flavor_b)
        with self.assertRaises(AccessError):
            flavor_a.with_user(self.central_storekeeper).write({
                "name": "Central Must Not Rename Flavors",
            })
        self.assertEqual(
            self.env["restaurant.menu.stock.mapping"].with_user(
                self.central_storekeeper
            ).search([("id", "in", mappings.ids)]),
            mappings,
        )
        with self.assertRaises(AccessError):
            mappings[0].with_user(self.central_storekeeper).write({
                "stock_qty": 9.0,
            })
        with self.assertRaises(AccessError):
            self.env["restaurant.menu.stock.mapping"].with_user(
                self.central_storekeeper
            ).create({
                "variant_id": self.full_variant.id,
                "stock_product_id": self.product.id,
                "stock_qty": 9.0,
                "stock_uom_id": self.product.uom_id.id,
            })

        with self.assertRaises(AccessError):
            self._event(user=self.central_storekeeper)

        event = self._event()
        self._to_manager_review(event, prepared=True)
        with self.assertRaises(AccessError):
            event.with_user(self.central_storekeeper).action_approve()


    def test_central_storekeeper_has_no_menu_catalog_navigation(self):
        legacy_root = self.env.ref(
            "restaurant_stock_events.restaurant_central_menu_catalog_root"
        )
        mapping_menu = self.env.ref(
            "restaurant_stock_events.restaurant_central_menu_variant_menu"
        )
        service_events_menu = self.env.ref(
            "restaurant_stock_events.restaurant_stock_event_menu_root"
        )
        visible = self.env["ir.ui.menu"].with_user(
            self.central_storekeeper
        )._visible_menu_ids()

        self.assertFalse(legacy_root.active)
        self.assertFalse(mapping_menu.active)
        self.assertNotIn(legacy_root.id, visible)
        self.assertNotIn(mapping_menu.id, visible)
        self.assertNotIn(service_events_menu.id, visible)

    def test_automatic_daily_components_are_system_controlled(self):
        daily = self._daily(5.0)
        line = daily.line_ids.filtered(
            lambda current: current.product_id == self.product
        )

        with self.assertRaises(AccessError):
            line.with_user(self.stockkeeper).write({
                "auto_complimentary_qty": 4.0,
            })
        with self.assertRaises(AccessError):
            line.with_user(self.stockkeeper).write({
                "cancellation_waste_move_id": False,
            })

    def test_reception_closing_collects_events_and_requires_terminal_review(self):
        closing = self._closing()
        cancellation = self._event(
            "cancellation",
            self.unmapped_variant,
            2.0,
            closing=closing,
            order_reference="CLOSE-CAN-1",
            reported_amount=20.0,
        )
        complimentary = self._event(
            "complimentary",
            self.half_variant,
            1.0,
            closing=closing,
            order_reference="CLOSE-COMP-1",
            reported_amount=15.0,
        )

        self.assertEqual(closing.cancelled_item_qty, 2.0)
        self.assertEqual(closing.total_cancelled_amount, 20.0)
        self.assertEqual(closing.complimentary_item_qty, 1.0)
        self.assertEqual(closing.total_complimentary_amount, 15.0)
        with self.assertRaisesRegex(ValidationError, "Draft service event"):
            closing.action_submit()

        cancellation.with_user(self.reception).action_submit_kitchen()
        complimentary.with_user(self.reception).action_submit_kitchen()
        closing.action_submit()
        with self.assertRaisesRegex(ValidationError, "must be approved"):
            closing.with_user(self.manager).action_confirm()

        cancellation.with_user(self.stockkeeper).action_mark_not_prepared()
        cancellation.with_user(self.manager).action_approve()
        complimentary.with_user(self.manager).action_approve()
        closing.with_user(self.manager).action_confirm()
        self.assertEqual(closing.state, "confirmed")
        self.assertFalse(cancellation.application_ids)
        self.assertEqual(
            complimentary.application_ids.effect_type,
            "consumption",
        )
        self.assertIn("Complimentary:", closing.get_whatsapp_summary())
        self.assertIn("Cancelled:", closing.get_whatsapp_summary())

    def test_discount_and_complimentary_overlap_requires_confirmation(self):
        closing = self._closing()
        discount = self.env["restaurant.reception.discount"].with_user(
            self.reception
        ).create({
            "closing_id": closing.id,
            "order_reference": "MIXED-ORDER-1",
            "item_description": "Synthetic Discount Item",
            "quantity": 1.0,
            "amount": 5.0,
            "reason": "Synthetic discount",
        })
        complimentary = self._event(
            "complimentary",
            self.unmapped_variant,
            closing=closing,
            order_reference="mixed-order-1",
            reported_amount=10.0,
        )
        complimentary.with_user(self.reception).action_submit_kitchen()

        with self.assertRaisesRegex(ValidationError, "both Discount"):
            closing.action_submit()

        discount.with_user(self.reception).write({
            "separate_complimentary_event_confirmed": True,
        })
        closing.action_submit()
        self.assertEqual(closing.state, "manager_review")

    def test_closing_review_and_reopen_do_not_replay_stock_movements(self):
        closing = self._closing()
        cancellation = self._approve(
            self._event(
                "cancellation",
                self.full_variant,
                2.0,
                closing=closing,
                order_reference="MOVE-CAN-1",
                reported_amount=20.0,
            ),
            prepared=True,
        )
        complimentary = self._approve(
            self._event(
                "complimentary",
                self.half_variant,
                3.0,
                closing=closing,
                order_reference="MOVE-COMP-1",
                reported_amount=15.0,
            ),
            prepared=True,
        )
        daily = self._daily(20.0)
        line = daily.line_ids.filtered(
            lambda current: current.product_id == self.product
        )
        line.with_user(self.stockkeeper).write({
            "actual_closing_qty": 13.0,
            "actual_closing_state": "counted",
        })
        daily.action_submit_closing()
        daily.with_user(self.manager).action_close()

        consumption_move = line.consumption_move_id
        waste_move = line.cancellation_waste_move_id
        move_count = self.env["stock.move"].search_count([])
        quantity_before = self.env["stock.quant"]._get_available_quantity(
            self.product,
            self.warehouse.lot_stock_id,
        )

        closing.action_submit()
        manager_closing = closing.with_user(self.manager)
        manager_closing.correction_reason = "Correct Reception notes only."
        manager_closing.action_return_to_draft()
        closing.with_user(self.reception).action_submit()
        manager_closing.action_confirm()

        self.assertEqual(closing.state, "confirmed")
        self.assertEqual(line.consumption_move_id, consumption_move)
        self.assertEqual(line.cancellation_waste_move_id, waste_move)
        self.assertEqual(self.env["stock.move"].search_count([]), move_count)
        self.assertEqual(
            self.env["stock.quant"]._get_available_quantity(
                self.product,
                self.warehouse.lot_stock_id,
            ),
            quantity_before,
        )
        self.assertEqual(cancellation.application_ids.quantity, 4.0)
        self.assertEqual(complimentary.application_ids.quantity, 3.0)

    def test_events_and_closings_auto_link_without_duplicate_sources(self):
        event_before = self._event(
            "cancellation",
            self.unmapped_variant,
            order_reference="AUTO-BEFORE",
        )
        self.assertFalse(event_before.closing_id)
        closing = self._closing()
        self.assertEqual(event_before.closing_id, closing)

        event_after = self._event(
            "complimentary",
            self.unmapped_variant,
            order_reference="AUTO-AFTER",
        )
        self.assertEqual(event_after.closing_id, closing)
        with self.assertRaisesRegex(ValidationError, "linked service events"):
            closing.action_cancel()

        event_before.with_user(self.reception).action_cancel()
        event_after.with_user(self.reception).action_cancel()
        closing.action_cancel()
        self.assertEqual(closing.state, "cancelled")

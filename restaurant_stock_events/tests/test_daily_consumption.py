from odoo import Command, fields
from odoo.exceptions import AccessError, UserError, ValidationError
from odoo.tests.common import new_test_user

from odoo.addons.base.tests.common import BaseCommon


class TestDailyConsumption(BaseCommon):
    _test_user_groups = ()

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.company = cls.env.company
        cls.warehouse, cls.other_warehouse = cls.env["stock.warehouse"].create([
            {
                "name": "Consumption Test Warehouse",
                "code": "CTW",
                "company_id": cls.company.id,
            },
            {
                "name": "Consumption Other Warehouse",
                "code": "COW",
                "company_id": cls.company.id,
            },
        ])
        cls.branch, cls.other_branch = cls.env["restaurant.branch"].create([
            {
                "name": "Consumption Test Branch",
                "code": "CTB",
                "company_id": cls.company.id,
                "warehouse_id": cls.warehouse.id,
            },
            {
                "name": "Consumption Other Branch",
                "code": "COB",
                "company_id": cls.company.id,
                "warehouse_id": cls.other_warehouse.id,
            },
        ])
        cls.stockkeeper = cls._make_user(
            "consumption_stockkeeper",
            "restaurant_core.group_restaurant_stockkeeper",
            cls.branch,
        )
        cls.other_stockkeeper = cls._make_user(
            "consumption_other_stockkeeper",
            "restaurant_core.group_restaurant_stockkeeper",
            cls.other_branch,
        )
        cls.manager = cls._make_user(
            "consumption_manager",
            "restaurant_core.group_restaurant_branch_manager",
            cls.branch,
        )
        cls.pure_product, cls.menu_product, cls.incomplete_product = cls.env[
            "product.product"
        ].create([
            {"name": "Pure Consumption Product", "is_storable": True},
            {"name": "Menu Consumption Product", "is_storable": True},
            {"name": "Incomplete Setup Product", "is_storable": True},
        ])
        cls.category, cls.other_category = cls.env[
            "restaurant.menu.category"
        ].create([
            {
                "name": "Consumption Test Menu",
                "branch_id": cls.branch.id,
            },
            {
                "name": "Consumption Other Menu",
                "branch_id": cls.other_branch.id,
            },
        ])
        cls.section = cls.env["restaurant.stock.section"].with_user(
            cls.stockkeeper
        ).create({
            "name": "Consumption Kitchen",
            "section_type": "kitchen",
            "branch_id": cls.branch.id,
        })
        cls.other_section = cls.env["restaurant.stock.section"].with_user(
            cls.other_stockkeeper
        ).create({
            "name": "Consumption Other Kitchen",
            "section_type": "kitchen",
            "branch_id": cls.other_branch.id,
        })
        cls.env["restaurant.stock.section.product"].with_user(
            cls.stockkeeper
        ).create([
            {
                "section_id": cls.section.id,
                "product_id": product.id,
            }
            for product in (
                cls.pure_product,
                cls.menu_product,
                cls.incomplete_product,
            )
        ])
        cls.env["restaurant.stock.section.product"].with_user(
            cls.other_stockkeeper
        ).create([
            {
                "section_id": cls.other_section.id,
                "product_id": product.id,
            }
            for product in (
                cls.pure_product,
                cls.menu_product,
                cls.incomplete_product,
            )
        ])
        cls.menu_setup = cls.env["restaurant.menu.mapping.profile"].create({
            "name": "Menu Consumption Setup",
            "branch_id": cls.branch.id,
            "category_id": cls.category.id,
            "stock_product_id": cls.menu_product.id,
        })
        cls.service_date = fields.Date.context_today(
            cls.env["restaurant.stock.daily"].with_context(
                tz=cls.company.tz or "UTC"
            )
        )

    @classmethod
    def _make_user(cls, login, group, branch):
        return new_test_user(
            cls.env,
            login=login,
            groups=group,
            company_id=cls.company.id,
            company_ids=[Command.set(cls.company.ids)],
            restaurant_branch_ids=[Command.set(branch.ids)],
        )

    def _put_in_stock(self, product, quantity, warehouse=None):
        warehouse = warehouse or self.warehouse
        self.env["stock.quant"]._update_available_quantity(
            product,
            warehouse.lot_stock_id,
            quantity,
        )

    def _daily(self, branch=None, user=None):
        branch = branch or self.branch
        user = user or self.stockkeeper
        section = (
            self.other_section if branch == self.other_branch else self.section
        )
        return self.env["restaurant.stock.daily"].with_user(user).create({
            "branch_id": branch.id,
            "section_id": section.id,
            "stock_date": self.service_date,
        })

    def _count_other_lines(self, daily, selected_line, user=None):
        user = user or self.stockkeeper
        (daily.line_ids - selected_line).with_user(user).write({
            "actual_closing_qty": 0.0,
            "actual_closing_state": "counted",
        })

    def test_daily_lines_split_once_between_menu_and_consumption(self):
        self._put_in_stock(self.menu_product, 6.0)
        self._put_in_stock(self.pure_product, 7.0)

        daily = self._daily()
        menu_line = daily.line_ids.filtered(
            lambda line: line.product_id == self.menu_product
        )
        consumption_line = daily.line_ids.filtered(
            lambda line: line.product_id == self.pure_product
        )

        self.assertEqual(len(menu_line), 1)
        self.assertEqual(len(consumption_line), 1)
        self.assertEqual(menu_line.operation_mode, "menu")
        self.assertEqual(consumption_line.operation_mode, "consumption")
        self.assertIn(menu_line, daily.menu_stock_line_ids)
        self.assertIn(consumption_line, daily.consumption_stock_line_ids)
        self.assertFalse(
            daily.menu_stock_line_ids & daily.consumption_stock_line_ids
        )

    def test_incomplete_menu_setup_blocks_opening(self):
        incomplete = self.env[
            "restaurant.menu.mapping.profile"
        ].with_context(menu_setup_migration=True).create({
            "name": "Incomplete Menu Setup",
            "branch_id": self.branch.id,
            "stock_product_id": self.incomplete_product.id,
        })
        self.assertFalse(incomplete.configuration_complete)
        self._put_in_stock(self.incomplete_product, 4.0)

        daily = self._daily()
        line = daily.line_ids.filtered(
            lambda current: current.product_id == self.incomplete_product
        )
        self.assertEqual(line.operation_mode, "setup_incomplete")
        self.assertNotIn(line, daily.consumption_stock_line_ids)
        with self.assertRaisesRegex(ValidationError, "incomplete setup"):
            daily.action_confirm_opening()

    def test_consumption_expected_uses_current_minus_manual_quantity(self):
        self._put_in_stock(self.pure_product, 7.0)
        daily = self._daily()
        line = daily.line_ids.filtered(
            lambda current: current.product_id == self.pure_product
        )
        daily.action_confirm_opening()
        self._count_other_lines(daily, line)

        line.with_user(self.stockkeeper).write({
            "consumption_qty": 2.0,
            "actual_closing_qty": 5.0,
            "note": "Two units prepared.",
        })
        self.assertEqual(line.expected_closing_qty, 5.0)
        self.assertEqual(line.difference_qty, 0.0)
        self.assertEqual(line.missing_qty, 0.0)
        self.assertEqual(line.surplus_qty, 0.0)
        daily.action_submit_closing()
        daily.with_user(self.manager).action_close()

        self.assertEqual(daily.state, "closed")
        self.assertEqual(line.consumption_move_id.quantity, 2.0)
        self.assertFalse(line.missing_move_id)
        move = line.consumption_move_id
        with self.assertRaises(UserError):
            daily.with_user(self.manager).action_close()
        self.assertEqual(line.consumption_move_id, move)
        self.assertEqual(
            self.env["stock.move"].search_count([("id", "=", move.id)]),
            1,
        )
        self.assertEqual(
            self.env["stock.quant"]._get_available_quantity(
                self.pure_product,
                self.warehouse.lot_stock_id,
            ),
            5.0,
        )

    def test_consumption_mismatch_blocks_until_corrected_with_note(self):
        self._put_in_stock(self.pure_product, 7.0)
        daily = self._daily()
        line = daily.line_ids.filtered(
            lambda current: current.product_id == self.pure_product
        )
        daily.action_confirm_opening()
        self._count_other_lines(daily, line)

        line.with_user(self.stockkeeper).write({
            "consumption_qty": 1.0,
            "actual_closing_qty": 5.0,
            "note": "Initial preparation review.",
        })
        with self.assertRaisesRegex(ValidationError, "Expected matches Actual"):
            daily.action_submit_closing()
        self.assertEqual(daily.state, "opened")
        self.assertFalse(line.consumption_move_id)
        self.assertFalse(line.missing_move_id)

        line.with_user(self.stockkeeper).write({
            "consumption_qty": 2.0,
            "note": False,
        })
        with self.assertRaisesRegex(ValidationError, "reason.*Note"):
            daily.action_submit_closing()

        line.with_user(self.stockkeeper).write({
            "note": "Two units prepared outside mapped sales.",
        })
        daily.action_submit_closing()
        daily.with_user(self.manager).action_close()
        self.assertEqual(line.consumption_move_id.quantity, 2.0)
        self.assertFalse(line.missing_move_id)

    def test_consumption_zero_count_and_surplus_source_guard(self):
        self._put_in_stock(self.pure_product, 2.0)
        daily = self._daily()
        line = daily.line_ids.filtered(
            lambda current: current.product_id == self.pure_product
        )
        daily.action_confirm_opening()
        self._count_other_lines(daily, line)

        line.with_user(self.stockkeeper).write({
            "consumption_qty": 2.0,
            "actual_closing_qty": 0.0,
            "note": "All two units prepared.",
        })
        self.assertEqual(line.actual_closing_state, "counted")
        self.assertEqual(line.expected_closing_qty, 0.0)
        daily.action_submit_closing()
        daily.with_user(self.manager).action_close()
        self.assertEqual(line.consumption_move_id.quantity, 2.0)
        self.assertFalse(line.missing_move_id)

        self._put_in_stock(
            self.pure_product,
            5.0,
            warehouse=self.other_warehouse,
        )
        other_daily = self._daily(
            branch=self.other_branch,
            user=self.other_stockkeeper,
        )
        other_line = other_daily.line_ids.filtered(
            lambda current: current.product_id == self.pure_product
        )
        other_daily.action_confirm_opening()
        self._count_other_lines(
            other_daily,
            other_line,
            user=self.other_stockkeeper,
        )
        other_line.with_user(self.other_stockkeeper).write({
            "actual_closing_qty": 6.0,
        })
        with self.assertRaisesRegex(
            ValidationError,
            "Actual is above Current.*do not invent negative usage",
        ):
            other_daily.action_submit_closing()
        self.assertFalse(other_line.consumption_move_id)
        self.assertFalse(other_line.missing_move_id)

    def test_consumption_rejects_negative_manual_quantity(self):
        self._put_in_stock(self.pure_product, 2.0)
        daily = self._daily()
        line = daily.line_ids.filtered(
            lambda current: current.product_id == self.pure_product
        )
        with self.assertRaisesRegex(ValidationError, "cannot be negative"):
            line.with_user(self.stockkeeper).write({"consumption_qty": -1.0})

    def test_active_sectioned_line_cannot_keep_legacy_bypass(self):
        self._put_in_stock(self.pure_product, 5.0)
        daily = self._daily()
        line = daily.line_ids.filtered(
            lambda current: current.product_id == self.pure_product
        )
        daily.action_confirm_opening()
        self._count_other_lines(daily, line)

        line.sudo().write({"operation_mode": "legacy"})
        line.with_user(self.stockkeeper).write({
            "consumption_qty": 1.0,
            "actual_closing_qty": 3.0,
            "note": "Pre-upgrade entry must still be reconciled.",
        })
        line._refresh_operation_mode_from_setup()

        self.assertEqual(line.operation_mode, "consumption")
        with self.assertRaisesRegex(ValidationError, "Expected matches Actual"):
            daily.action_submit_closing()
        self.assertFalse(line.consumption_move_id)
        self.assertFalse(line.missing_move_id)

    def test_additional_consumption_is_snapshot_and_open_sheet_is_guarded(self):
        self._put_in_stock(self.menu_product, 5.0)
        daily = self._daily()
        line = daily.line_ids.filtered(
            lambda current: current.product_id == self.menu_product
        )
        self.assertEqual(line.operation_mode, "menu")
        self.assertFalse(line.manual_consumption_enabled)
        with self.assertRaises(AccessError):
            line.with_user(self.stockkeeper).write({"consumption_qty": 1.0})

        self.menu_setup.with_user(self.manager).write({
            "allow_additional_consumption": True,
        })
        self.assertTrue(line.allow_additional_consumption)
        self.assertTrue(line.manual_consumption_enabled)
        line.with_user(self.stockkeeper).write({
            "consumption_qty": 1.0,
            "actual_closing_qty": 4.0,
            "note": "Preparation not included in Sold.",
        })
        daily.action_confirm_opening()
        self._count_other_lines(daily, line)

        with self.assertRaisesRegex(ValidationError, "Daily Stock sheets are open"):
            self.menu_setup.with_user(self.manager).write({
                "allow_additional_consumption": False,
            })

        daily.action_submit_closing()
        daily.with_user(self.manager).action_close()
        self.assertEqual(line.consumption_move_id.quantity, 1.0)
        self.assertFalse(line.sold_move_id)

    def test_additional_consumption_setting_is_branch_scoped(self):
        other_setup = self.env["restaurant.menu.mapping.profile"].create({
            "name": "Other Branch Menu Setup",
            "branch_id": self.other_branch.id,
            "category_id": self.other_category.id,
            "stock_product_id": self.menu_product.id,
        })
        self.menu_setup.write({"allow_additional_consumption": True})
        self.assertFalse(other_setup.allow_additional_consumption)

        self._put_in_stock(
            self.menu_product,
            3.0,
            warehouse=self.other_warehouse,
        )
        other_daily = self._daily(
            branch=self.other_branch,
            user=self.other_stockkeeper,
        )
        other_line = other_daily.line_ids.filtered(
            lambda current: current.product_id == self.menu_product
        )
        self.assertEqual(other_line.operation_mode, "menu")
        self.assertFalse(other_line.allow_additional_consumption)
        self.assertFalse(other_line.manual_consumption_enabled)

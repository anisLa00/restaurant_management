from datetime import timedelta

from lxml import etree

from odoo import Command, fields
from odoo.exceptions import AccessError, ValidationError
from odoo.tests.common import new_test_user

from odoo.addons.base.tests.common import BaseCommon
from odoo.addons.restaurant_stock.models.restaurant_stock_daily import (
    ALLOW_NONCURRENT_DAILY_DATE_CONTEXT,
)


class TestRestaurantStockSections(BaseCommon):
    _test_user_groups = ()

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.company = cls.env.company
        cls.warehouse, cls.other_warehouse = cls.env["stock.warehouse"].create([
            {
                "name": "Section Test Warehouse",
                "code": "STW",
                "company_id": cls.company.id,
            },
            {
                "name": "Section Other Warehouse",
                "code": "SOW",
                "company_id": cls.company.id,
            },
        ])
        cls.branch, cls.other_branch = cls.env["restaurant.branch"].create([
            {
                "name": "Section Test Branch",
                "code": "STB",
                "company_id": cls.company.id,
                "warehouse_id": cls.warehouse.id,
            },
            {
                "name": "Section Other Branch",
                "code": "SOB",
                "company_id": cls.company.id,
                "warehouse_id": cls.other_warehouse.id,
            },
        ])
        cls.stockkeeper = cls._make_user(
            "section_stockkeeper",
            "restaurant_core.group_restaurant_stockkeeper",
            cls.branch,
        )
        cls.other_stockkeeper = cls._make_user(
            "section_other_stockkeeper",
            "restaurant_core.group_restaurant_stockkeeper",
            cls.other_branch,
        )
        cls.manager = cls._make_user(
            "section_manager",
            "restaurant_core.group_restaurant_branch_manager",
            cls.branch,
        )
        cls.operations = cls._make_user(
            "section_operations",
            "restaurant_core.group_restaurant_operations_manager",
            cls.branch,
        )
        cls.central = cls._make_user(
            "section_central",
            "restaurant_core.group_restaurant_central_storekeeper",
            cls.branch,
        )
        cls.kitchen_product, cls.bar_product = cls.env[
            "product.product"
        ].create([
            {"name": "Section Kitchen Product", "is_storable": True},
            {"name": "Section Bar Product", "is_storable": True},
        ])
        cls.service_date = fields.Date.context_today(
            cls.env["restaurant.stock.daily"].with_context(
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

    def _setup_sections(self):
        Section = self.env["restaurant.stock.section"].with_user(
            self.stockkeeper
        )
        kitchen, bar = Section.create([
            {
                "name": "Kitchen",
                "section_type": "kitchen",
                "branch_id": self.branch.id,
            },
            {
                "name": "Bar",
                "section_type": "bar",
                "branch_id": self.branch.id,
            },
        ])
        self.env["restaurant.stock.section.product"].with_user(
            self.stockkeeper
        ).create([
            {
                "section_id": kitchen.id,
                "product_id": self.kitchen_product.id,
            },
            {
                "section_id": bar.id,
                "product_id": self.bar_product.id,
            },
        ])
        return kitchen, bar

    def test_stockkeeper_configures_and_manager_only_reviews(self):
        kitchen, bar = self._setup_sections()
        manager_sections = self.env["restaurant.stock.section"].with_user(
            self.manager
        ).search([("id", "in", (kitchen | bar).ids)])
        self.assertEqual(manager_sections, kitchen | bar)

        with self.assertRaises(AccessError):
            kitchen.with_user(self.manager).write({"name": "Manager Edit"})
        with self.assertRaises(AccessError):
            kitchen.with_user(self.central).write({"name": "Central Edit"})
        with self.assertRaises(AccessError):
            self.env["restaurant.stock.section"].with_user(
                self.other_stockkeeper
            ).create({
                "name": "Cross Branch Kitchen",
                "section_type": "kitchen",
                "branch_id": self.branch.id,
            })

        visible_other = self.env["restaurant.stock.section"].with_user(
            self.other_stockkeeper
        ).search([("id", "in", (kitchen | bar).ids)])
        self.assertFalse(visible_other)
        menu = self.env.ref("restaurant_stock.restaurant_stock_section_menu")
        self.assertFalse(menu.active)
        self.assertNotIn(
            menu.id,
            self.env["ir.ui.menu"].with_user(self.manager)._visible_menu_ids(),
        )

    def test_legacy_section_ui_has_no_mutating_or_open_affordances(self):
        daily_form = etree.fromstring(
            self.env.ref(
                "restaurant_stock.restaurant_stock_daily_view_form"
            ).arch_db
        )
        section_fields = daily_form.xpath("//field[@name='section_id']")
        self.assertTrue(section_fields)
        self.assertIn("'no_create': True", section_fields[0].get("options"))
        self.assertIn("'no_open': True", section_fields[0].get("options"))

        section_list = etree.fromstring(
            self.env.ref(
                "restaurant_stock.restaurant_stock_section_view_list"
            ).arch_db
        )
        for attribute in ("create", "edit", "delete"):
            self.assertEqual(section_list.get(attribute), "0")

        section_form = etree.fromstring(
            self.env.ref(
                "restaurant_stock.restaurant_stock_section_view_form"
            ).arch_db
        )
        for attribute in ("create", "edit", "delete"):
            self.assertEqual(section_form.get(attribute), "0")
        assignments = section_form.xpath(
            "//field[@name='product_assignment_ids']"
        )
        self.assertEqual(assignments[0].get("readonly"), "1")
        assignment_list = assignments[0].xpath("./list")[0]
        for attribute in ("create", "edit", "delete"):
            self.assertEqual(assignment_list.get(attribute), "0")

    def test_native_categories_auto_group_and_include_zero_stock(self):
        Section = self.env["restaurant.stock.section"]
        sections = Section._ensure_category_sections(self.branch)
        kitchen = sections.filtered(lambda section: section.section_type == "kitchen")
        bar = sections.filtered(lambda section: section.section_type == "bar")
        disposable = sections.filtered(
            lambda section: section.section_type == "disposable"
        )
        kitchen_product, bar_product, disposable_product = self.env[
            "product.product"
        ].create([
            {
                "name": "Synthetic Zero Kitchen Catalog Product",
                "is_storable": True,
                "categ_id": self.env.ref(
                    "restaurant_stock.product_category_dry_items"
                ).id,
            },
            {
                "name": "Synthetic Zero Bar Catalog Product",
                "is_storable": True,
                "categ_id": self.env.ref(
                    "restaurant_stock.product_category_bar"
                ).id,
            },
            {
                "name": "Synthetic Zero Disposable Catalog Product",
                "is_storable": True,
                "categ_id": self.env.ref(
                    "restaurant_stock.product_category_disposable"
                ).id,
            },
        ])

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
        disposable_daily = Daily.create({
            "branch_id": self.branch.id,
            "section_id": disposable.id,
            "stock_date": self.service_date,
        })

        self.assertIn(kitchen_product, kitchen_daily.line_ids.product_id)
        self.assertNotIn(bar_product, kitchen_daily.line_ids.product_id)
        self.assertIn(bar_product, bar_daily.line_ids.product_id)
        self.assertIn(disposable_product, disposable_daily.line_ids.product_id)
        for product, daily in (
            (kitchen_product, kitchen_daily),
            (bar_product, bar_daily),
            (disposable_product, disposable_daily),
        ):
            line = daily.line_ids.filtered(lambda item: item.product_id == product)
            self.assertEqual(line.opening_qty, 0)
            self.assertEqual(line.received_qty, 0)

    def test_manager_uses_one_shared_close_for_all_stock_sections(self):
        sections = self.env[
            "restaurant.stock.section"
        ]._ensure_category_sections(self.branch)
        Daily = self.env["restaurant.stock.daily"].with_user(self.stockkeeper)
        dailies = self.env["restaurant.stock.daily"]
        for section in sections:
            daily = Daily.create({
                "branch_id": self.branch.id,
                "section_id": section.id,
                "stock_date": self.service_date,
            })
            daily.action_confirm_opening()
            daily.line_ids.write({
                "actual_closing_qty": 0.0,
                "actual_closing_state": "counted",
            })
            daily.action_submit_closing()
            dailies |= daily

        leads = self.env["restaurant.stock.daily"].search([
            ("id", "in", dailies.ids),
            ("is_closing_group_lead", "=", True),
        ])
        self.assertEqual(len(leads), 1)
        self.assertEqual(leads.combined_section_count, len(sections))
        self.assertTrue(leads.combined_closing_ready)
        self.assertEqual(
            leads.combined_closing_line_ids,
            dailies.line_ids,
        )

        leads.with_user(self.manager).action_close()
        self.assertEqual(set(dailies.mapped("state")), {"closed"})

        manager_action = self.env[
            "restaurant.stock.daily"
        ].with_user(self.manager).action_open_daily_stock_workspace()
        stockkeeper_action = self.env[
            "restaurant.stock.daily"
        ].with_user(self.stockkeeper).action_open_daily_stock_workspace()
        self.assertEqual(
            manager_action["id"],
            self.env.ref(
                "restaurant_stock.restaurant_stock_daily_close_action"
            ).id,
        )
        self.assertEqual(
            stockkeeper_action["id"],
            self.env.ref(
                "restaurant_stock.restaurant_stock_daily_action"
            ).id,
        )
        # ir.actions.server evaluates its Python code with a sudoed
        # environment.  The router must still respect the original user's
        # management role and keep the consolidated one-row-per-day action.
        sudo_manager_action = self.env[
            "restaurant.stock.daily"
        ].with_user(self.manager).sudo().action_open_daily_stock_workspace()
        self.assertEqual(
            sudo_manager_action["id"],
            self.env.ref(
                "restaurant_stock.restaurant_stock_daily_close_action"
            ).id,
        )
        sudo_operations_action = self.env[
            "restaurant.stock.daily"
        ].with_user(self.operations).sudo().action_open_daily_stock_workspace()
        self.assertEqual(
            sudo_operations_action["id"],
            self.env.ref(
                "restaurant_stock.restaurant_stock_daily_close_action"
            ).id,
        )
        # Read-only management roles must also be allowed to execute the
        # menu's server action.  Without explicit action groups Odoo requires
        # write access to the routed model before evaluating the code.
        operations_menu_action = self.env.ref(
            "restaurant_stock.restaurant_stock_daily_router_action"
        ).with_user(self.operations).run()
        self.assertEqual(
            operations_menu_action["id"],
            self.env.ref(
                "restaurant_stock.restaurant_stock_daily_close_action"
            ).id,
        )
        lead = leads[:1]
        by_type = {
            sheet.section_id.section_type: sheet
            for sheet in dailies
        }
        self.assertEqual(
            lead.with_user(self.manager).action_open_kitchen_section()["res_id"],
            by_type["kitchen"].id,
        )
        self.assertEqual(
            lead.with_user(self.manager).action_open_bar_section()["res_id"],
            by_type["bar"].id,
        )
        self.assertEqual(
            lead.with_user(self.manager).action_open_disposable_section()["res_id"],
            by_type["disposable"].id,
        )

    def test_refresh_adds_new_zero_stock_catalog_product_only_to_active_sheet(self):
        sections = self.env[
            "restaurant.stock.section"
        ]._ensure_category_sections(self.branch)
        kitchen = sections.filtered(lambda section: section.section_type == "kitchen")
        Daily = self.env["restaurant.stock.daily"].with_user(self.stockkeeper)
        daily = Daily.create({
            "branch_id": self.branch.id,
            "section_id": kitchen.id,
            "stock_date": self.service_date,
        })
        product = self.env["product.product"].create({
            "name": "Synthetic Late Zero Kitchen Product",
            "is_storable": True,
            "categ_id": self.env.ref(
                "restaurant_stock.product_category_vegetables"
            ).id,
        })

        self.assertNotIn(product, daily.line_ids.product_id)
        daily._sync_received_from_central()
        self.assertIn(product, daily.line_ids.product_id)

        daily.action_cancel()
        after_cancel = self.env["product.product"].create({
            "name": "Synthetic Post-Cancel Kitchen Product",
            "is_storable": True,
            "categ_id": self.env.ref(
                "restaurant_stock.product_category_dairy_items"
            ).id,
        })
        daily._ensure_catalog_lines()
        self.assertNotIn(after_cancel, daily.line_ids.product_id)

    def test_product_assignment_is_unique_and_locked_during_active_day(self):
        kitchen, bar = self._setup_sections()
        with self.assertRaises(ValidationError):
            self.env["restaurant.stock.section.product"].with_user(
                self.stockkeeper
            ).create({
                "section_id": bar.id,
                "product_id": self.kitchen_product.id,
            })

        daily = self.env["restaurant.stock.daily"].with_user(
            self.stockkeeper
        ).create({
            "branch_id": self.branch.id,
            "section_id": kitchen.id,
            "stock_date": self.service_date,
        })
        with self.assertRaisesRegex(ValidationError, "Finish or cancel"):
            kitchen.product_assignment_ids.with_user(
                self.stockkeeper
            ).write({"note": "Unsafe mid-day change"})
        daily.action_cancel()
        kitchen.product_assignment_ids.with_user(self.stockkeeper).write({
            "note": "Safe between service days",
        })

    def test_daily_opening_is_separate_and_filtered_by_section(self):
        kitchen, bar = self._setup_sections()
        self.env["stock.quant"]._update_available_quantity(
            self.kitchen_product,
            self.warehouse.lot_stock_id,
            10.0,
        )
        self.env["stock.quant"]._update_available_quantity(
            self.bar_product,
            self.warehouse.lot_stock_id,
            5.0,
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
        kitchen_line = kitchen_daily.line_ids.filtered(
            lambda line: line.product_id == self.kitchen_product
        )
        bar_line = bar_daily.line_ids.filtered(
            lambda line: line.product_id == self.bar_product
        )
        self.assertTrue(kitchen_line)
        self.assertEqual(kitchen_line.opening_qty, 10.0)
        self.assertTrue(bar_line)
        self.assertEqual(bar_line.opening_qty, 5.0)

        with self.assertRaises(ValidationError):
            Daily.create({
                "branch_id": self.branch.id,
                "stock_date": self.service_date,
            })
        with self.assertRaises(ValidationError):
            Daily.create({
                "branch_id": self.branch.id,
                "section_id": kitchen.id,
                "stock_date": self.service_date,
            })

    def test_prior_close_carries_only_within_same_section(self):
        kitchen, bar = self._setup_sections()
        yesterday = self.service_date - timedelta(days=1)
        Daily = self.env["restaurant.stock.daily"].with_user(self.stockkeeper)

        historical = self.env["restaurant.stock.daily"]
        for section, product, closing_qty in (
            (kitchen, self.kitchen_product, 7.0),
            (bar, self.bar_product, 3.0),
        ):
            daily = Daily.sudo().with_context(**{
                ALLOW_NONCURRENT_DAILY_DATE_CONTEXT: True,
            }).create({
                "branch_id": self.branch.id,
                "section_id": section.id,
                "stock_date": yesterday,
                "line_ids": [Command.create({
                    "product_id": product.id,
                    "opening_qty": 10.0,
                    "consumption_qty": 10.0 - closing_qty,
                    "actual_closing_qty": closing_qty,
                    "actual_closing_state": "counted",
                    "note": "Historical section carry-forward test.",
                })],
            })
            daily.action_confirm_opening()
            daily.line_ids.filtered(
                lambda line: line.actual_closing_state != "counted"
            ).write({"actual_closing_state": "counted"})
            daily.with_user(self.stockkeeper).action_submit_closing()
            historical |= daily

        historical[:1].with_user(self.manager).action_close()
        self.assertEqual(set(historical.mapped("state")), {"closed"})

        kitchen_today = Daily.create({
            "branch_id": self.branch.id,
            "section_id": kitchen.id,
            "stock_date": self.service_date,
        })
        bar_today = Daily.create({
            "branch_id": self.branch.id,
            "section_id": bar.id,
            "stock_date": self.service_date,
        })
        kitchen_line = kitchen_today.line_ids.filtered(
            lambda line: line.product_id == self.kitchen_product
        )
        bar_line = bar_today.line_ids.filtered(
            lambda line: line.product_id == self.bar_product
        )
        self.assertEqual(kitchen_line.opening_qty, 7.0)
        self.assertEqual(bar_line.opening_qty, 3.0)

from odoo import api, fields, models
from odoo.exceptions import AccessError, ValidationError


class RestaurantReceptionSoldMenuLine(models.Model):
    _name = "restaurant.reception.sold.menu.line"
    _description = "Reception Sold Menu Item"
    _inherit = "restaurant.reception.closing.entry"
    _order = "menu_item_id, variant_id, id"

    menu_item_id = fields.Many2one(
        "restaurant.menu.item",
        string="Menu Item",
        ondelete="restrict",
        index=True,
        help=(
            "Select the configured product-specific Menu Item (Product — Flavor) "
            "once. Its saved stock mapping and Standard or Half / Full mode are "
            "inherited automatically; Reception only enters the counts."
        ),
    )
    size_applicable = fields.Boolean(
        related="menu_item_id.size_applicable",
        string="Half / Full Portions",
        readonly=True,
    )
    standard_count = fields.Float(
        string="Standard Count", default=0.0, aggregator=None,
    )
    half_count = fields.Float(
        string="Half Count", default=0.0, aggregator=None,
    )
    full_count = fields.Float(
        string="Full Count", default=0.0, aggregator=None,
    )

    variant_id = fields.Many2one(
        "restaurant.menu.variant",
        string="Legacy Sold Menu Item / Portion",
        ondelete="restrict",
        index=True,
        help="Preserved for historical Reception Sold rows created before 1.8.",
    )
    menu_qty = fields.Float(
        string="Legacy Sold Quantity",
        default=0.0,
        aggregator=None,
        help="Preserved for historical Reception Sold rows created before 1.8.",
    )
    component_ids = fields.One2many(
        "restaurant.reception.sold.menu.component",
        "sale_id",
        string="Frozen Stock Conversion",
        readonly=True,
    )
    note = fields.Char()

    _closing_variant_unique = models.UniqueIndex(
        "(closing_id, variant_id) WHERE variant_id IS NOT NULL",
        "Enter each sold Menu Item / size once per Reception Closing.",
    )
    _closing_item_unique = models.UniqueIndex(
        "(closing_id, menu_item_id) WHERE menu_item_id IS NOT NULL",
        "Enter each sold Menu Item once per Reception Closing.",
    )

    @api.model
    def _reception_editable_fields(self):
        return super()._reception_editable_fields() | {
            "menu_item_id",
            "standard_count",
            "half_count",
            "full_count",
            "variant_id",
            "menu_qty",
            "note",
        }

    def _count_for_size(self, size):
        self.ensure_one()
        if not self.menu_item_id:
            return self.menu_qty
        return {
            "standard": self.standard_count,
            "half": self.half_count,
            "full": self.full_count,
        }[size]

    def _variants_for_snapshot(self):
        """Return each internal portion variant and its count for this row."""
        self.ensure_one()
        if self.menu_item_id:
            sizes = (
                ("half", "full")
                if self.menu_item_id.size_applicable
                else ("standard",)
            )
            active_variants = self.menu_item_id.variant_ids.filtered("active")
            result = []
            for size in sizes:
                variant = active_variants.filtered(
                    lambda current, wanted=size: current.size == wanted
                )[:1]
                if not variant:
                    raise ValidationError(
                        self.env._(
                            "Missing Mapping: %(menu_item)s has no active %(size)s "
                            "portion configuration.",
                            menu_item=self.menu_item_id.display_name,
                            size=size.title(),
                        )
                    )
                result.append((variant, self._count_for_size(size)))
            return result
        if self.variant_id:
            return [(self.variant_id, self.menu_qty)]
        raise ValidationError(self.env._("Choose one sold Menu Item."))

    @api.constrains(
        "menu_item_id", "variant_id", "menu_qty",
        "standard_count", "half_count", "full_count",
    )
    def _check_menu_quantities(self):
        for sale in self:
            if sale.menu_item_id and sale.variant_id:
                raise ValidationError(
                    self.env._(
                        "Use the Menu Item count row or the preserved legacy "
                        "variant row, not both."
                    )
                )
            if sale.menu_item_id:
                counts = (
                    sale.standard_count,
                    sale.half_count,
                    sale.full_count,
                )
                if any(count < 0 for count in counts):
                    raise ValidationError(
                        self.env._("Sold Menu Item counts cannot be negative.")
                    )
                if sale.menu_item_id.size_applicable:
                    if sale.standard_count or not (
                        sale.half_count or sale.full_count
                    ):
                        raise ValidationError(
                            self.env._(
                                "Enter a Half Count, a Full Count, or both for "
                                "this portion-based Menu Item."
                            )
                        )
                elif (
                    sale.half_count
                    or sale.full_count
                    or sale.standard_count <= 0
                ):
                    raise ValidationError(
                        self.env._(
                            "Enter one positive Standard Count for this Menu Item; "
                            "Half and Full do not apply."
                        )
                    )
            elif not sale.variant_id or sale.menu_qty <= 0:
                raise ValidationError(
                    self.env._("Sold menu quantity must be greater than zero.")
                )

    @api.constrains("menu_item_id", "variant_id", "closing_id")
    def _check_variant_scope(self):
        Section = self.env["restaurant.stock.section"].sudo()
        for sale in self:
            subject = sale.menu_item_id or sale.variant_id
            if (
                subject.branch_id != sale.branch_id
                or subject.company_id != sale.company_id
            ):
                raise ValidationError(
                    self.env._(
                        "The sold menu item belongs to another branch or company."
                    )
                )
            mappings = self.env["restaurant.menu.stock.mapping"]
            for variant, _quantity in sale._variants_for_snapshot():
                if not variant.mapping_ids:
                    raise ValidationError(
                        self.env._(
                            "Missing Mapping: %s has no explicit stock conversion.",
                            variant.display_name,
                        )
                    )
                mappings |= variant.mapping_ids
            sections = Section.search([
                ("branch_id", "=", sale.branch_id.id),
                ("active", "=", True),
            ])
            if sections:
                missing = mappings.stock_product_id.filtered(
                    lambda product: not Section._section_for_product(
                        sale.branch_id,
                        product,
                    )
                )
                if missing:
                    raise ValidationError(
                        self.env._(
                            "Choose an approved Kitchen, Bar, or Disposable "
                            "Product Category before Reception records the sale: %s",
                            ", ".join(missing.mapped("display_name")),
                        )
                    )

    def _related_open_dailies(self):
        Daily = self.env["restaurant.stock.daily"].sudo()
        result = Daily.browse()
        for sale in self:
            result |= Daily.search([
                ("company_id", "=", sale.company_id.id),
                ("branch_id", "=", sale.branch_id.id),
                ("stock_date", "=", sale.business_date),
                ("state", "not in", ["closed", "cancelled"]),
            ])
        return result

    def _ensure_stock_open_for_change(self):
        Daily = self.env["restaurant.stock.daily"].sudo()
        for sale in self:
            closed = Daily.search([
                ("company_id", "=", sale.company_id.id),
                ("branch_id", "=", sale.branch_id.id),
                ("stock_date", "=", sale.business_date),
                ("state", "=", "closed"),
            ], limit=1)
            if closed:
                raise ValidationError(
                    self.env._(
                        "Daily Stock for %s is already closed. Sold menu "
                        "quantities cannot be changed without a controlled "
                        "stock correction.",
                        sale.business_date,
                    )
                )

    def _check_no_legacy_daily_source(self):
        LegacySale = self.env["restaurant.stock.daily.sale"].sudo()
        for sale in self:
            legacy = LegacySale.search([
                ("company_id", "=", sale.company_id.id),
                ("branch_id", "=", sale.branch_id.id),
                ("stock_date", "=", sale.business_date),
            ], limit=1)
            if legacy:
                raise ValidationError(
                    self.env._(
                        "This branch and date already contain historical Daily "
                        "Stock Sold entries. Do not mix them with Reception Sold "
                        "Menu Items; choose one audited source before continuing."
                    )
                )

    def _create_component_snapshot(self):
        Component = self.env[
            "restaurant.reception.sold.menu.component"
        ].sudo()
        Section = self.env["restaurant.stock.section"].sudo()
        values = []
        for sale in self:
            sections = Section.search([
                ("branch_id", "=", sale.branch_id.id),
                ("active", "=", True),
            ])
            for variant, sold_count in sale._variants_for_snapshot():
                for mapping in variant.mapping_ids:
                    section = self.env["restaurant.stock.section"]
                    if sections:
                        section = Section._section_for_product(
                            sale.branch_id,
                            mapping.stock_product_id,
                        )
                    quantity_per_unit = mapping.stock_uom_id._compute_quantity(
                        mapping.stock_qty,
                        mapping.stock_product_id.uom_id,
                        round=False,
                    )
                    values.append({
                        "sale_id": sale.id,
                        "mapping_id": mapping.id,
                        "stock_product_id": mapping.stock_product_id.id,
                        "section_id": section.id or False,
                        "uom_id": mapping.stock_product_id.uom_id.id,
                        "quantity_per_menu_unit": quantity_per_unit,
                        "sold_qty": quantity_per_unit * sold_count,
                    })
        if values:
            Component.create(values)

    def _sync_daily_stock(self, extra_dailies=None):
        dailies = self._related_open_dailies() | (extra_dailies or self.env[
            "restaurant.stock.daily"
        ])
        if dailies:
            dailies.sudo()._sync_mapped_sold_quantities()

    @api.model_create_multi
    def create(self, vals_list):
        for values in vals_list:
            closing_id = values.get("closing_id")
            menu_item_id = values.get("menu_item_id")
            if closing_id and menu_item_id and self.sudo().search([
                ("closing_id", "=", closing_id),
                ("menu_item_id", "=", menu_item_id),
            ], limit=1):
                raise ValidationError(
                    self.env._(
                        "Enter each sold flavor once per Reception Closing."
                    )
                )
        records = super().create(vals_list)
        records._ensure_stock_open_for_change()
        records._check_no_legacy_daily_source()
        records._create_component_snapshot()
        records._sync_daily_stock()
        return records

    def write(self, vals):
        self._ensure_stock_open_for_change()
        dailies = self._related_open_dailies()
        result = super().write(vals)
        if set(vals) & {"menu_item_id", "variant_id"}:
            self.component_ids.sudo().unlink()
            self._create_component_snapshot()
        elif set(vals) & {
            "menu_qty", "standard_count", "half_count", "full_count",
        }:
            for component in self.component_ids.sudo():
                component.write({
                    "sold_qty": (
                        component.quantity_per_menu_unit
                        * component.sale_id._count_for_size(
                            component.mapping_id.variant_id.size
                        )
                    ),
                })
        self._sync_daily_stock(extra_dailies=dailies)
        return result

    def unlink(self):
        self._ensure_stock_open_for_change()
        dailies = self._related_open_dailies()
        result = super().unlink()
        if dailies:
            dailies.sudo()._sync_mapped_sold_quantities()
        return result


class RestaurantReceptionSoldMenuComponent(models.Model):
    _name = "restaurant.reception.sold.menu.component"
    _description = "Frozen Reception Sold Stock Conversion"
    _order = "sale_id, stock_product_id, id"

    sale_id = fields.Many2one(
        "restaurant.reception.sold.menu.line",
        required=True,
        ondelete="cascade",
        index=True,
    )
    closing_id = fields.Many2one(
        related="sale_id.closing_id", store=True, readonly=True, index=True,
    )
    branch_id = fields.Many2one(
        related="sale_id.branch_id", store=True, readonly=True, index=True,
    )
    business_date = fields.Date(
        related="sale_id.business_date", store=True, readonly=True, index=True,
    )
    company_id = fields.Many2one(
        related="sale_id.company_id", store=True, readonly=True, index=True,
    )
    section_id = fields.Many2one(
        "restaurant.stock.section", readonly=True, ondelete="restrict", index=True,
    )
    mapping_id = fields.Many2one(
        "restaurant.menu.stock.mapping",
        required=True,
        readonly=True,
        ondelete="restrict",
    )
    stock_product_id = fields.Many2one(
        "product.product",
        required=True,
        readonly=True,
        ondelete="restrict",
    )
    uom_id = fields.Many2one("uom.uom", required=True, readonly=True)
    quantity_per_menu_unit = fields.Float(required=True, readonly=True)
    sold_qty = fields.Float(required=True, readonly=True, aggregator=None)

    _sale_mapping_unique = models.UniqueIndex(
        "(sale_id, mapping_id)",
        "This Reception Sold conversion is already captured.",
    )

    @api.model_create_multi
    def create(self, vals_list):
        if not self.env.su:
            raise AccessError(
                self.env._("Reception Sold conversion snapshots are system-controlled.")
            )
        return super().create(vals_list)

    def write(self, vals):
        if not self.env.su:
            raise AccessError(
                self.env._("Reception Sold conversion snapshots are system-controlled.")
            )
        return super().write(vals)

    def unlink(self):
        if not self.env.su:
            raise AccessError(
                self.env._("Reception Sold conversion snapshots are system-controlled.")
            )
        return super().unlink()


class RestaurantDailyClosingSoldMenu(models.Model):
    _inherit = "restaurant.daily.closing"

    sold_menu_line_ids = fields.One2many(
        "restaurant.reception.sold.menu.line",
        "closing_id",
        string="Sold Menu Items",
        copy=True,
    )

    @api.model
    def _draft_editable_fields(self):
        return super()._draft_editable_fields() | {"sold_menu_line_ids"}

    def write(self, vals):
        if set(vals) & {"branch_id", "closing_date"} and any(
            closing.sold_menu_line_ids for closing in self
        ):
            raise ValidationError(
                self.env._(
                    "Remove Sold Menu Items before changing the branch or "
                    "business date."
                )
            )
        return super().write(vals)

    def action_cancel(self):
        sales = self.sold_menu_line_ids
        sales._ensure_stock_open_for_change()
        dailies = sales._related_open_dailies()
        result = super().action_cancel()
        if dailies:
            dailies.sudo()._sync_mapped_sold_quantities()
        return result

    def unlink(self):
        sales = self.sold_menu_line_ids
        sales._ensure_stock_open_for_change()
        dailies = sales._related_open_dailies()
        result = super().unlink()
        if dailies:
            dailies.sudo()._sync_mapped_sold_quantities()
        return result


class RestaurantStockDailyReceptionSold(models.Model):
    _inherit = "restaurant.stock.daily"

    reception_sold_menu_line_ids = fields.Many2many(
        "restaurant.reception.sold.menu.line",
        compute="_compute_reception_sold_menu_lines",
        string="Reception Sold Menu Items",
    )

    @api.depends("branch_id", "stock_date", "section_id")
    def _compute_reception_sold_menu_lines(self):
        Sale = self.env["restaurant.reception.sold.menu.line"].sudo()
        for daily in self:
            sales = Sale.search([
                ("company_id", "=", daily.company_id.id),
                ("branch_id", "=", daily.branch_id.id),
                ("business_date", "=", daily.stock_date),
                ("closing_id.state", "!=", "cancelled"),
            ])
            if daily.section_id:
                sales = sales.filtered(
                    lambda sale: daily.section_id in sale.component_ids.section_id
                )
            daily.reception_sold_menu_line_ids = sales

    @api.model_create_multi
    def create(self, vals_list):
        records = super().create(vals_list)
        records.sudo()._sync_mapped_sold_quantities()
        return records

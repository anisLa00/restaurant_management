from odoo import api, fields, models
from odoo.exceptions import AccessError, ValidationError

from odoo.addons.restaurant_stock.models.restaurant_stock_daily import (
    INVENTORY_RECEIPT_SYNC_CONTEXT,
    SOLD_QUANTITY_SYNC_CONTEXT,
)

from .restaurant_stock_event import (
    MANAGER_GROUP,
    STOCKKEEPER_GROUP,
    require_assigned_branches,
)


def _require_sale_editor(record):
    record.daily_id.check_access("write")
    require_assigned_branches(record.branch_id)
    user = record.env.user
    if user.has_group(STOCKKEEPER_GROUP):
        if record.daily_id.state not in ("draft", "opened"):
            raise AccessError(
                record.env._(
                    "The Branch Stock Keeper can edit Sold entries only while "
                    "Daily Stock is Draft or Opened."
                )
            )
        return
    if user.has_group(MANAGER_GROUP):
        if record.daily_id.state != "closing_review":
            raise AccessError(
                record.env._(
                    "The Branch Manager can correct Sold entries only during "
                    "Closing Review."
                )
            )
        return
    raise AccessError(record.env._("Your role does not allow Sold entry."))


class RestaurantStockDailySale(models.Model):
    _name = "restaurant.stock.daily.sale"
    _description = "Daily Sold Menu Entry"
    _order = "variant_id, id"

    daily_id = fields.Many2one(
        "restaurant.stock.daily",
        required=True,
        ondelete="cascade",
        index=True,
    )
    branch_id = fields.Many2one(
        related="daily_id.branch_id",
        store=True,
        readonly=True,
        index=True,
    )
    section_id = fields.Many2one(
        related="daily_id.section_id",
        store=True,
        readonly=True,
        index=True,
    )
    stock_date = fields.Date(
        related="daily_id.stock_date",
        store=True,
        readonly=True,
        index=True,
    )
    company_id = fields.Many2one(
        related="daily_id.company_id",
        store=True,
        readonly=True,
        index=True,
    )
    variant_id = fields.Many2one(
        "restaurant.menu.variant",
        string="Sold Menu Item / Size",
        required=True,
        ondelete="restrict",
        index=True,
    )
    menu_qty = fields.Float(
        string="Sold Menu Quantity",
        required=True,
        default=1.0,
        aggregator=None,
    )
    component_ids = fields.One2many(
        "restaurant.stock.daily.sale.component",
        "sale_id",
        string="Frozen Stock Conversion",
        readonly=True,
    )
    note = fields.Char()

    _daily_variant_unique = models.UniqueIndex(
        "(daily_id, variant_id)",
        "Enter each sold menu variant once per Daily Stock sheet.",
    )

    @api.constrains("menu_qty")
    def _check_menu_qty(self):
        for sale in self:
            if sale.menu_qty <= 0:
                raise ValidationError(
                    self.env._("Sold menu quantity must be greater than zero.")
                )

    def _check_variant_scope(self):
        Section = self.env["restaurant.stock.section"].sudo()
        for sale in self:
            variant = sale.variant_id
            if (
                variant.branch_id != sale.branch_id
                or variant.company_id != sale.company_id
            ):
                raise ValidationError(
                    self.env._(
                        "The sold menu variant belongs to another branch or company."
                    )
                )
            mappings = variant.mapping_ids
            if not mappings:
                raise ValidationError(
                    self.env._(
                        "Missing Mapping: %s has no explicit stock conversion.",
                        variant.display_name,
                    )
                )
            if sale.section_id:
                products = mappings.stock_product_id.filtered(
                    lambda product: (
                        Section._section_for_product(sale.branch_id, product)
                        != sale.section_id
                    )
                )
                if products:
                    raise ValidationError(
                        self.env._(
                            "Sold item %s cannot be entered in %s. These mapped "
                            "stock products use another Product Category group: %s",
                            variant.display_name,
                            sale.section_id.display_name,
                            ", ".join(products.mapped("display_name")),
                        )
                    )

    def _create_component_snapshot(self):
        Component = self.env[
            "restaurant.stock.daily.sale.component"
        ].sudo()
        values = []
        for sale in self:
            for mapping in sale.variant_id.mapping_ids:
                quantity_per_unit = mapping.stock_uom_id._compute_quantity(
                    mapping.stock_qty,
                    mapping.stock_product_id.uom_id,
                    round=False,
                )
                values.append({
                    "sale_id": sale.id,
                    "mapping_id": mapping.id,
                    "stock_product_id": mapping.stock_product_id.id,
                    "uom_id": mapping.stock_product_id.uom_id.id,
                    "quantity_per_menu_unit": quantity_per_unit,
                    "sold_qty": quantity_per_unit * sale.menu_qty,
                })
        if values:
            Component.create(values)

    @api.model_create_multi
    def create(self, vals_list):
        if not self.env.su:
            raise AccessError(
                self.env._(
                    "Reception Daily Closing owns Sold Menu Items. Branch "
                    "Stock Keepers must not re-enter them in Daily Stock."
                )
            )
        Daily = self.env["restaurant.stock.daily"]
        ReceptionSale = self.env["restaurant.reception.sold.menu.line"].sudo()
        defaults = self.default_get(["daily_id"])
        for values in vals_list:
            daily = Daily.browse(values.get("daily_id", defaults.get("daily_id")))
            if daily and ReceptionSale.search_count([
                ("company_id", "=", daily.company_id.id),
                ("branch_id", "=", daily.branch_id.id),
                ("business_date", "=", daily.stock_date),
                ("closing_id.state", "!=", "cancelled"),
            ]):
                raise ValidationError(
                    self.env._(
                        "Sold Menu Items for this branch and date are owned by "
                        "Reception Daily Closing. Do not enter them again in "
                        "Daily Stock."
                    )
                )
        records = super().create(vals_list)
        for record in records:
            _require_sale_editor(record)
        records._check_variant_scope()
        records._create_component_snapshot()
        records.daily_id.sudo()._sync_mapped_sold_quantities()
        return records

    def write(self, vals):
        if set(vals) - {"menu_qty", "note"}:
            raise AccessError(
                self.env._(
                    "Only Sold Menu Quantity and note can be corrected. Delete "
                    "and re-enter the row to change the menu variant."
                )
            )
        for record in self:
            _require_sale_editor(record)
        result = super().write(vals)
        self._check_menu_qty()
        if "menu_qty" in vals:
            for component in self.component_ids.sudo():
                component.write({
                    "sold_qty": (
                        component.quantity_per_menu_unit
                        * component.sale_id.menu_qty
                    ),
                })
            self.daily_id.sudo()._sync_mapped_sold_quantities()
        return result

    def unlink(self):
        dailies = self.daily_id
        for record in self:
            _require_sale_editor(record)
        result = super().unlink()
        dailies.sudo()._sync_mapped_sold_quantities()
        return result


class RestaurantStockDailySaleComponent(models.Model):
    _name = "restaurant.stock.daily.sale.component"
    _description = "Frozen Daily Sold Stock Conversion"
    _order = "sale_id, stock_product_id, id"

    sale_id = fields.Many2one(
        "restaurant.stock.daily.sale",
        required=True,
        ondelete="cascade",
        index=True,
    )
    daily_id = fields.Many2one(
        related="sale_id.daily_id",
        store=True,
        readonly=True,
        index=True,
    )
    branch_id = fields.Many2one(
        related="sale_id.branch_id",
        store=True,
        readonly=True,
        index=True,
    )
    section_id = fields.Many2one(
        related="sale_id.section_id",
        store=True,
        readonly=True,
        index=True,
    )
    stock_date = fields.Date(
        related="sale_id.stock_date",
        store=True,
        readonly=True,
        index=True,
    )
    company_id = fields.Many2one(
        related="sale_id.company_id",
        store=True,
        readonly=True,
        index=True,
    )
    mapping_id = fields.Many2one(
        "restaurant.menu.stock.mapping",
        required=True,
        ondelete="restrict",
    )
    stock_product_id = fields.Many2one(
        "product.product",
        required=True,
        ondelete="restrict",
        index=True,
    )
    uom_id = fields.Many2one("uom.uom", required=True, ondelete="restrict")
    quantity_per_menu_unit = fields.Float(required=True, aggregator=None)
    sold_qty = fields.Float(required=True, aggregator=None)

    _sale_mapping_unique = models.UniqueIndex(
        "(sale_id, mapping_id)",
        "This Sold mapping conversion is already captured.",
    )

    @api.model_create_multi
    def create(self, vals_list):
        if not self.env.su:
            raise AccessError(
                self.env._("Sold stock conversion snapshots are system-controlled.")
            )
        return super().create(vals_list)

    def write(self, vals):
        if not self.env.su:
            raise AccessError(
                self.env._("Sold stock conversion snapshots are system-controlled.")
            )
        return super().write(vals)

    def unlink(self):
        if not self.env.su:
            raise AccessError(
                self.env._("Sold stock conversion snapshots are system-controlled.")
            )
        return super().unlink()


class RestaurantStockDaily(models.Model):
    _inherit = "restaurant.stock.daily"

    sold_menu_line_ids = fields.One2many(
        "restaurant.stock.daily.sale",
        "daily_id",
        string="Sold Menu Items",
        copy=True,
    )

    def _sync_mapped_sold_quantities(self):
        Component = self.env[
            "restaurant.stock.daily.sale.component"
        ].sudo()
        ReceptionComponent = self.env[
            "restaurant.reception.sold.menu.component"
        ].sudo()
        Line = self.env["restaurant.stock.daily.line"].sudo()
        for daily in self:
            if daily.state in ("closed", "cancelled"):
                continue
            components = Component.search([("daily_id", "=", daily.id)])
            reception_components = ReceptionComponent.search([
                ("company_id", "=", daily.company_id.id),
                ("branch_id", "=", daily.branch_id.id),
                ("business_date", "=", daily.stock_date),
                ("closing_id.state", "!=", "cancelled"),
            ] + (
                [("section_id", "=", daily.section_id.id)]
                if daily.section_id
                else []
            ))
            totals = {}
            for component in list(components) + list(reception_components):
                totals[component.stock_product_id.id] = (
                    totals.get(component.stock_product_id.id, 0.0)
                    + component.sold_qty
                )
            remaining = dict(totals)
            for line in daily.line_ids:
                quantity = remaining.pop(line.product_id.id, 0.0)
                line.with_context(**{
                    SOLD_QUANTITY_SYNC_CONTEXT: True,
                }).write({"sold_qty": quantity})
            for product_id, quantity in remaining.items():
                line = Line.with_context(**{
                    INVENTORY_RECEIPT_SYNC_CONTEXT: True,
                }).create({
                    "daily_id": daily.id,
                    "product_id": product_id,
                    "opening_qty": 0.0,
                    "received_qty": 0.0,
                })
                line.with_context(**{
                    SOLD_QUANTITY_SYNC_CONTEXT: True,
                }).write({"sold_qty": quantity})
        return True

    def action_confirm_opening(self):
        self.sudo()._sync_mapped_sold_quantities()
        return super().action_confirm_opening()

    def action_submit_closing(self):
        self.sudo()._sync_mapped_sold_quantities()
        return super().action_submit_closing()

    def action_close(self):
        self.sudo()._sync_mapped_sold_quantities()
        return super().action_close()

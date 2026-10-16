from odoo import api, fields, models
from odoo.exceptions import ValidationError


class RestaurantReceptionDiscount(models.Model):
    _inherit = "restaurant.reception.discount"

    menu_item_id = fields.Many2one(
        "restaurant.menu.item",
        string="Menu Item",
        ondelete="restrict",
        index=True,
        help=(
            "Choose an active Menu Item from the same branch. The displayed "
            "name is saved on the discount as a historical snapshot."
        ),
    )
    menu_variant_id = fields.Many2one(
        "restaurant.menu.variant",
        string="Size / Variant",
        ondelete="restrict",
        index=True,
        help="Choose the exact Standard, Half, or Full menu variant.",
    )
    discount_method = fields.Selection(
        [
            ("fixed", "Fixed Amount"),
            ("percentage", "Percentage"),
        ],
        required=True,
        default="fixed",
        index=True,
    )
    unit_price = fields.Monetary(
        string="Unit Price (VAT Included)",
        currency_field="currency_id",
        default=0.0,
        copy=False,
        help=(
            "Historical VAT-inclusive price copied from the selected Menu "
            "Variant when this discount is entered."
        ),
    )
    discount_percentage = fields.Float(
        string="Discount %",
        digits=(16, 2),
        default=0.0,
    )
    line_subtotal = fields.Monetary(
        string="Original Total",
        currency_field="currency_id",
        compute="_compute_discount_totals",
        store=True,
    )
    final_price = fields.Monetary(
        string="Price After Discount",
        currency_field="currency_id",
        compute="_compute_discount_totals",
        store=True,
    )

    @api.model
    def _reception_editable_fields(self):
        return super()._reception_editable_fields() | {
            "menu_item_id",
            "menu_variant_id",
            "discount_method",
            "unit_price",
            "discount_percentage",
        }

    @api.depends("quantity", "unit_price", "amount")
    def _compute_discount_totals(self):
        for discount in self:
            subtotal = discount.currency_id.round(
                discount.quantity * discount.unit_price
            )
            discount.line_subtotal = subtotal
            discount.final_price = max(
                discount.currency_id.round(subtotal - discount.amount),
                0.0,
            )

    @api.model
    def _active_item_variants(self, item):
        return item.variant_ids.filtered("active").sorted("id")

    @api.model
    def _prepare_calculated_values(self, incoming, record=None):
        values = dict(incoming)
        previous_item_id = record.menu_item_id.id if record else False
        item_id = values.get("menu_item_id", previous_item_id)
        item = self.env["restaurant.menu.item"].browse(item_id).exists()
        if self.env.context.get("discount_menu_catalog_ui") and not item:
            raise ValidationError(self.env._(
                "Choose the discounted Item from the branch Menu Catalog."
            ))
        closing = (
            self.env["restaurant.daily.closing"].browse(
                values.get("closing_id")
            ).exists()
            if values.get("closing_id")
            else record.closing_id if record else False
        )
        if item and closing and (
            item.branch_id != closing.branch_id
            or item.company_id != closing.company_id
        ):
            raise ValidationError(self.env._(
                "Choose an active Menu Item from the same branch as the "
                "Daily Closing."
            ))

        item_changed = "menu_item_id" in values and item_id != previous_item_id
        if "menu_variant_id" in values:
            variant_id = values.get("menu_variant_id")
        elif record and not item_changed:
            variant_id = record.menu_variant_id.id
        else:
            variant_id = False
        variant = self.env["restaurant.menu.variant"].browse(
            variant_id
        ).exists()

        if item and not variant:
            variants = self._active_item_variants(item)
            if len(variants) == 1:
                variant = variants
                values["menu_variant_id"] = variant.id
            elif self.env.context.get("discount_menu_catalog_ui"):
                raise ValidationError(self.env._(
                    "Choose the exact Size / Variant for the discounted Item."
                ))

        if variant:
            if not item or variant.item_id != item:
                raise ValidationError(self.env._(
                    "The selected Size / Variant must belong to the discounted Item."
                ))
            values["unit_price"] = (
                record.unit_price
                if record and not item_changed
                and "menu_variant_id" not in values
                else variant.sales_price
            )
        elif record and "unit_price" in values:
            values["unit_price"] = record.unit_price

        if item:
            values["item_description"] = item.display_name

        method = values.get(
            "discount_method",
            record.discount_method if record else "fixed",
        )
        quantity = values.get("quantity", record.quantity if record else 1.0)
        unit_price = values.get(
            "unit_price", record.unit_price if record else 0.0
        )
        percentage = values.get(
            "discount_percentage",
            record.discount_percentage if record else 0.0,
        )
        if method == "percentage":
            if percentage < 0.0 or percentage > 100.0:
                raise ValidationError(self.env._(
                    "Discount Percentage must be between 0 and 100."
                ))
            currency = closing.currency_id or self.env.company.currency_id
            values["amount"] = currency.round(
                quantity * unit_price * percentage / 100.0
            )
        return values

    @api.onchange("menu_item_id")
    def _onchange_menu_item_id(self):
        for discount in self:
            if discount.menu_item_id:
                discount.item_description = discount.menu_item_id.display_name
                variants = discount._active_item_variants(discount.menu_item_id)
                discount.menu_variant_id = variants if len(variants) == 1 else False
                discount.unit_price = (
                    discount.menu_variant_id.sales_price
                    if discount.menu_variant_id
                    else 0.0
                )

    @api.onchange("menu_variant_id")
    def _onchange_menu_variant_id(self):
        for discount in self:
            if discount.menu_variant_id:
                discount.menu_item_id = discount.menu_variant_id.item_id
                discount.item_description = discount.menu_item_id.display_name
                discount.unit_price = discount.menu_variant_id.sales_price

    @api.onchange(
        "quantity", "unit_price", "discount_method", "discount_percentage"
    )
    def _onchange_discount_calculation(self):
        for discount in self.filtered(
            lambda current: current.discount_method == "percentage"
        ):
            discount.amount = discount.currency_id.round(
                discount.quantity
                * discount.unit_price
                * discount.discount_percentage
                / 100.0
            )

    @api.constrains(
        "menu_item_id",
        "menu_variant_id",
        "closing_id",
        "quantity",
        "unit_price",
        "discount_method",
        "discount_percentage",
        "amount",
    )
    def _check_menu_item_scope(self):
        for discount in self:
            if discount.menu_item_id and (
                discount.menu_item_id.branch_id != discount.branch_id
                or discount.menu_item_id.company_id != discount.company_id
            ):
                raise ValidationError(self.env._(
                    "Choose an active Menu Item from the same branch as the "
                    "Daily Closing."
                ))
            if discount.menu_variant_id:
                if discount.menu_variant_id.item_id != discount.menu_item_id:
                    raise ValidationError(self.env._(
                        "The selected Size / Variant must belong to the "
                        "discounted Item."
                    ))
                if discount.unit_price <= 0.0:
                    raise ValidationError(self.env._(
                        "Configure a positive VAT-inclusive Sales Price for "
                        "the selected Menu Variant first."
                    ))
                if discount.amount > discount.line_subtotal:
                    raise ValidationError(self.env._(
                        "The Discount Amount cannot exceed the Original Total."
                    ))
            if discount.discount_method == "percentage":
                if not 0.0 <= discount.discount_percentage <= 100.0:
                    raise ValidationError(self.env._(
                        "Discount Percentage must be between 0 and 100."
                    ))
                if not discount.menu_variant_id:
                    raise ValidationError(self.env._(
                        "Choose a priced Size / Variant before using a "
                        "Percentage discount."
                    ))

    @api.model_create_multi
    def create(self, vals_list):
        prepared = [
            self._prepare_calculated_values(values)
            for values in vals_list
        ]
        return super().create(prepared)

    def write(self, values):
        result = True
        for discount in self:
            prepared = self._prepare_calculated_values(values, record=discount)
            result = (
                super(RestaurantReceptionDiscount, discount).write(prepared)
                and result
            )
        return result

from odoo import api, fields, models
from odoo.exceptions import AccessError, ValidationError

from .reception_security import (
    RECEPTION_GROUP,
    lock_records,
    require_assigned_branches,
    require_draft,
    require_role,
)


class RestaurantReceptionClosingEntry(models.AbstractModel):
    _name = "restaurant.reception.closing.entry"
    _description = "Reception Closing Entry"

    closing_id = fields.Many2one(
        "restaurant.daily.closing",
        required=True,
        ondelete="cascade",
        index=True,
    )
    branch_id = fields.Many2one(
        related="closing_id.branch_id",
        store=True,
        readonly=True,
        index=True,
    )
    business_date = fields.Date(
        related="closing_id.closing_date",
        store=True,
        readonly=True,
        index=True,
    )
    state = fields.Selection(
        related="closing_id.state",
        store=True,
        readonly=True,
        index=True,
    )
    company_id = fields.Many2one(
        related="closing_id.company_id",
        store=True,
        readonly=True,
        index=True,
    )
    currency_id = fields.Many2one(
        related="closing_id.currency_id",
        store=True,
        readonly=True,
    )
    entry_source = fields.Selection(
        [
            ("manual", "Manual"),
            ("foodics", "Foodics"),
            ("import", "Imported"),
        ],
        required=True,
        default="manual",
        readonly=True,
        index=True,
    )
    external_reference = fields.Char(
        readonly=True,
        copy=False,
        index=True,
        help="Reserved for a future POS/Foodics transaction identifier.",
    )
    entered_by_id = fields.Many2one(
        "res.users",
        required=True,
        readonly=True,
        default=lambda self: self.env.user,
    )
    entered_at = fields.Datetime(
        required=True,
        readonly=True,
        default=fields.Datetime.now,
    )

    @api.model
    def _reception_editable_fields(self):
        return {"closing_id"}

    @api.model_create_multi
    def create(self, vals_list):
        if self.env.su:
            return super().create(vals_list)

        require_role(self.env, RECEPTION_GROUP)
        editable = self._reception_editable_fields()
        if any(set(values) - editable for values in vals_list):
            raise AccessError(
                self.env._(
                    "Reception closing audit and integration fields are "
                    "system-controlled."
                )
            )

        defaults = self.default_get(["closing_id"])
        closings = self.env["restaurant.daily.closing"].browse([
            values.get("closing_id", defaults.get("closing_id"))
            for values in vals_list
            if values.get("closing_id", defaults.get("closing_id"))
        ])
        lock_records(closings)
        require_draft(closings)
        require_assigned_branches(closings.branch_id)
        records = super().create(vals_list)
        records._post_closing_change("added")
        return records

    def write(self, vals):
        if self.env.su:
            return super().write(vals)

        self.check_access("write")
        require_role(self.env, RECEPTION_GROUP)
        if "closing_id" in vals or set(vals) - self._reception_editable_fields():
            raise AccessError(
                self.env._(
                    "Reception entries cannot be moved or their audit fields "
                    "overwritten."
                )
            )
        lock_records(self.closing_id)
        require_draft(self.closing_id)
        require_assigned_branches(self.branch_id)
        result = super().write(vals)
        self._post_closing_change("updated")
        return result

    def unlink(self):
        if self.env.su:
            return super().unlink()

        self.check_access("unlink")
        require_role(self.env, RECEPTION_GROUP)
        closings = self.closing_id
        lock_records(closings)
        require_draft(closings)
        require_assigned_branches(closings.branch_id)
        result = super().unlink()
        for closing in closings:
            closing.message_post(
                body=self.env._("Reception closing entries removed.")
            )
        return result

    def _post_closing_change(self, operation):
        labels = {
            "added": self.env._("added"),
            "updated": self.env._("updated"),
        }
        for closing in self.closing_id:
            closing.message_post(
                body=self.env._(
                    "Reception closing entries %s.",
                    labels.get(operation, operation),
                )
            )


class RestaurantReceptionPaymentLine(models.Model):
    _name = "restaurant.reception.payment.line"
    _description = "Reception Daily Payment Breakdown"
    _inherit = "restaurant.reception.closing.entry"
    _order = "payment_type, method_name, id"

    payment_type = fields.Selection(
        [
            ("cash", "Cash"),
            ("card", "Card"),
            ("local", "Local / Other Payment"),
            ("delivery", "Delivery Platform"),
            ("other", "Other"),
        ],
        required=True,
        default="cash",
        index=True,
    )
    method_name = fields.Char(
        help="For example Visa, Talabat, Careem, local wallet, or voucher."
    )
    amount = fields.Monetary(required=True, default=0.0)
    note = fields.Char()

    _amount_nonnegative = models.Constraint(
        "CHECK (amount >= 0)",
        "Payment amounts must not be negative.",
    )

    @api.model
    def _reception_editable_fields(self):
        return super()._reception_editable_fields() | {
            "payment_type",
            "method_name",
            "amount",
            "note",
        }

    @api.constrains("payment_type", "method_name")
    def _check_method_name(self):
        for line in self:
            if (
                line.payment_type in ("local", "delivery", "other")
                and not (line.method_name or "").strip()
            ):
                raise ValidationError(
                    self.env._(
                        "Enter the payment method or platform name for %s.",
                        dict(self._fields["payment_type"].selection)[
                            line.payment_type
                        ],
                    )
                )


class RestaurantReceptionTipLine(models.Model):
    _name = "restaurant.reception.tip.line"
    _description = "Reception Daily Tip Breakdown"
    _inherit = "restaurant.reception.closing.entry"
    _order = "tip_source, source_name, id"

    tip_source = fields.Selection(
        [
            ("cash", "Cash"),
            ("card", "Card"),
            ("local", "Local / Other Payment"),
            ("delivery", "Delivery Platform"),
            ("other", "Other"),
        ],
        required=True,
        default="cash",
        index=True,
    )
    source_name = fields.Char(
        help="Optional card terminal, platform, wallet, or other source name."
    )
    amount = fields.Monetary(required=True, default=0.0)
    note = fields.Char()

    _amount_nonnegative = models.Constraint(
        "CHECK (amount >= 0)",
        "Tip amounts must not be negative.",
    )

    @api.model
    def _reception_editable_fields(self):
        return super()._reception_editable_fields() | {
            "tip_source",
            "source_name",
            "amount",
            "note",
        }

    @api.model_create_multi
    def create(self, vals_list):
        defaults = self.default_get(["closing_id"])
        closings = self.env["restaurant.daily.closing"].browse([
            values.get("closing_id", defaults.get("closing_id"))
            for values in vals_list
            if values.get("closing_id", defaults.get("closing_id"))
        ])
        if any(
            closing.service_tracking_mode != "legacy"
            for closing in closings
        ):
            raise ValidationError(
                self.env._(
                    "Tip Sources are preserved for legacy closings only. "
                    "On new closings, enter cash and card tips once on each bill."
                )
            )
        return super().create(vals_list)

    def write(self, vals):
        if any(
            closing.service_tracking_mode != "legacy"
            for closing in self.closing_id
        ):
            raise ValidationError(
                self.env._(
                    "Tip Sources cannot be changed on a Service Entries closing."
                )
            )
        return super().write(vals)

    def unlink(self):
        if any(
            closing.service_tracking_mode != "legacy"
            for closing in self.closing_id
        ):
            raise ValidationError(
                self.env._(
                    "Tip Sources cannot be changed on a Service Entries closing."
                )
            )
        return super().unlink()


class RestaurantReceptionDiscount(models.Model):
    _name = "restaurant.reception.discount"
    _description = "Reception Daily Discount Entry"
    _inherit = "restaurant.reception.closing.entry"
    _order = "business_date desc, order_reference, id"

    order_reference = fields.Char(
        string="Order / Check Number",
        required=True,
        index=True,
    )
    item_description = fields.Char(string="Item", required=True)
    quantity = fields.Float(required=True, default=1.0)
    amount = fields.Monetary(
        string="Discount Amount",
        required=True,
        default=0.0,
    )
    reason = fields.Char(required=True)
    note = fields.Text()
    attachment_ids = fields.Many2many(
        "ir.attachment",
        "restaurant_reception_discount_attachment_rel",
        "discount_id",
        "attachment_id",
        string="Receipt / Report Proof",
        copy=False,
    )
    separate_complimentary_event_confirmed = fields.Boolean(
        string="Separate Discount and Complimentary Events Confirmed",
        help=(
            "Use only when the same order legitimately has a distinct "
            "discount and a distinct complimentary event."
        ),
    )

    _positive_quantity = models.Constraint(
        "CHECK (quantity > 0)",
        "Discount quantity must be greater than zero.",
    )
    _amount_nonnegative = models.Constraint(
        "CHECK (amount >= 0)",
        "Discount amounts must not be negative.",
    )

    @api.model
    def _reception_editable_fields(self):
        return super()._reception_editable_fields() | {
            "order_reference",
            "item_description",
            "quantity",
            "amount",
            "reason",
            "note",
            "attachment_ids",
            "separate_complimentary_event_confirmed",
        }

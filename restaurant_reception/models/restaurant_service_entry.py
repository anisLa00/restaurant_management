from collections import defaultdict

from odoo import api, fields, models
from odoo.exceptions import ValidationError

from .reception_security import check_employee_company


class RestaurantWaiterServiceEntry(models.Model):
    _name = "restaurant.waiter.service.entry"
    _description = "Reception Bill Service Entry"
    _inherit = "restaurant.reception.closing.entry"
    _order = "id"

    service_type = fields.Selection(
        [
            ("dine_in", "Dine-in"),
            ("pickup", "Pickup"),
            ("delivery", "Delivery"),
        ],
        required=True,
        default="dine_in",
        index=True,
    )
    employee_id = fields.Many2one(
        "hr.employee",
        string="Waiter",
        ondelete="restrict",
        index=True,
    )
    bill_reference = fields.Char(
        string="Bill / Check",
        required=True,
        index=True,
    )
    table_reference = fields.Char(string="Table")
    guest_count = fields.Integer(string="Guests", default=0)
    bill_amount = fields.Monetary(string="Bill Amount", required=True, default=0)
    cash_tip_amount = fields.Monetary(string="Cash Tip", required=True, default=0)
    card_tip_amount = fields.Monetary(string="Card Tip", required=True, default=0)
    tip_total = fields.Monetary(
        string="Pooled Tip Contribution",
        compute="_compute_tip_total",
        store=True,
    )
    review_count = fields.Integer(string="Reviews", default=0)
    note = fields.Char()

    _bill_reference_unique = models.UniqueIndex(
        "(closing_id, bill_reference)",
        "This bill/check is already recorded in the closing.",
    )
    _nonnegative_values = models.Constraint(
        "CHECK (guest_count >= 0 AND bill_amount >= 0 "
        "AND cash_tip_amount >= 0 AND card_tip_amount >= 0 "
        "AND review_count >= 0)",
        "Guests, bills, tips, and reviews cannot be negative.",
    )

    @api.depends("cash_tip_amount", "card_tip_amount")
    def _compute_tip_total(self):
        for entry in self:
            entry.tip_total = entry.cash_tip_amount + entry.card_tip_amount

    @api.model
    def _reception_editable_fields(self):
        return super()._reception_editable_fields() | {
            "service_type",
            "employee_id",
            "bill_reference",
            "table_reference",
            "guest_count",
            "bill_amount",
            "cash_tip_amount",
            "card_tip_amount",
            "review_count",
            "note",
        }

    @api.onchange("service_type")
    def _onchange_service_type(self):
        if self.service_type in ("pickup", "delivery"):
            self.employee_id = False
            self.table_reference = False
            self.guest_count = 0

    @api.constrains(
        "service_type",
        "employee_id",
        "table_reference",
        "guest_count",
        "closing_id",
        "bill_reference",
    )
    def _check_service_details(self):
        for entry in self:
            if entry.closing_id.service_tracking_mode != "service_entries":
                raise ValidationError(
                    self.env._(
                        "Bill service entries are only available on closings "
                        "using Service Entries mode."
                    )
                )
            if not (entry.bill_reference or "").strip():
                raise ValidationError(self.env._("Enter the bill/check reference."))
            if entry.service_type == "dine_in":
                if not entry.employee_id:
                    raise ValidationError(
                        self.env._("Select the waiter for every dine-in bill.")
                    )
                if not (entry.table_reference or "").strip():
                    raise ValidationError(
                        self.env._("Enter the table for every dine-in bill.")
                    )
                check_employee_company(entry)
            elif entry.employee_id or (entry.table_reference or "").strip():
                raise ValidationError(
                    self.env._(
                        "Pickup and delivery bills stay separate: do not assign "
                        "a waiter or table."
                    )
                )
            elif entry.guest_count:
                raise ValidationError(
                    self.env._(
                        "Guest count is recorded on dine-in bills only; keep "
                        "pickup and delivery guests at zero."
                    )
                )

    @api.model_create_multi
    def create(self, vals_list):
        values_list = []
        defaults = self.default_get(["closing_id"])
        for values in vals_list:
            values = dict(values)
            closing_id = values.get("closing_id", defaults.get("closing_id"))
            if closing_id:
                closing = self.env["restaurant.daily.closing"].browse(closing_id)
                if closing.service_tracking_mode != "service_entries":
                    raise ValidationError(
                        self.env._(
                            "Bill service entries cannot be added to a legacy closing."
                        )
                    )
            if "bill_reference" in values:
                values["bill_reference"] = (values["bill_reference"] or "").strip()
            if "table_reference" in values:
                values["table_reference"] = (values["table_reference"] or "").strip()
            values_list.append(values)
        records = super().create(values_list)
        records._sync_waiter_summaries()
        return records

    def write(self, vals):
        old_pairs = {
            (entry.closing_id.id, entry.employee_id.id)
            for entry in self
            if entry.service_type == "dine_in" and entry.employee_id
        }
        values = dict(vals)
        if "bill_reference" in values:
            values["bill_reference"] = (values["bill_reference"] or "").strip()
        if "table_reference" in values:
            values["table_reference"] = (values["table_reference"] or "").strip()
        result = super().write(values)
        self._sync_waiter_summaries(extra_pairs=old_pairs)
        return result

    def unlink(self):
        pairs = {
            (entry.closing_id.id, entry.employee_id.id)
            for entry in self
            if entry.service_type == "dine_in" and entry.employee_id
        }
        result = super().unlink()
        self._sync_waiter_summary_pairs(pairs)
        return result

    def _sync_waiter_summaries(self, extra_pairs=None):
        pairs = set(extra_pairs or ())
        pairs.update({
            (entry.closing_id.id, entry.employee_id.id)
            for entry in self
            if entry.service_type == "dine_in" and entry.employee_id
        })
        self._sync_waiter_summary_pairs(pairs)

    def _sync_waiter_summary_pairs(self, pairs):
        if not pairs:
            return

        by_closing = defaultdict(set)
        for closing_id, employee_id in pairs:
            if closing_id and employee_id:
                by_closing[closing_id].add(employee_id)

        entry_model = self.env["restaurant.waiter.service.entry"].sudo()
        line_model = self.env["restaurant.waiter.daily.line"].sudo().with_context(
            waiter_summary_sync=True,
        )
        for closing_id, employee_ids in by_closing.items():
            closing = self.env["restaurant.daily.closing"].sudo().browse(closing_id)
            if not closing.exists() or closing.service_tracking_mode != "service_entries":
                continue
            for employee_id in employee_ids:
                entries = entry_model.search([
                    ("closing_id", "=", closing_id),
                    ("service_type", "=", "dine_in"),
                    ("employee_id", "=", employee_id),
                ])
                lines = line_model.search([
                    ("closing_id", "=", closing_id),
                    ("employee_id", "=", employee_id),
                ])
                values = {
                    "sales_amount": sum(entries.mapped("bill_amount")),
                    "tips_amount": sum(entries.mapped("tip_total")),
                    "cash_tip_amount": sum(entries.mapped("cash_tip_amount")),
                    "card_tip_amount": sum(entries.mapped("card_tip_amount")),
                    "order_count": len(entries),
                    "table_count": len(entries),
                    "guest_count": sum(entries.mapped("guest_count")),
                    "review_count": sum(entries.mapped("review_count")),
                }
                if entries:
                    if lines:
                        lines[0].write(values)
                        if len(lines) > 1:
                            lines[1:].unlink()
                    else:
                        line_model.create({
                            "closing_id": closing_id,
                            "employee_id": employee_id,
                            **values,
                        })
                elif lines:
                    lines.unlink()

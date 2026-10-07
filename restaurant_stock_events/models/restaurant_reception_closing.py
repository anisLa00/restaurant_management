from odoo import api, fields, models
from odoo.exceptions import ValidationError

from .restaurant_stock_event import SYSTEM_EVENT_WRITE_CONTEXT


class RestaurantDailyClosing(models.Model):
    _inherit = "restaurant.daily.closing"

    stock_event_ids = fields.One2many(
        "restaurant.stock.event",
        "closing_id",
        string="Cancelled / Complimentary Entries",
    )
    complimentary_item_qty = fields.Float(
        compute="_compute_stock_event_totals",
        store=True,
        readonly=True,
        aggregator=None,
    )
    total_complimentary_amount = fields.Monetary(
        compute="_compute_stock_event_totals",
        store=True,
        readonly=True,
    )
    cancelled_item_qty = fields.Float(
        compute="_compute_stock_event_totals",
        store=True,
        readonly=True,
        aggregator=None,
    )
    total_cancelled_amount = fields.Monetary(
        compute="_compute_stock_event_totals",
        store=True,
        readonly=True,
    )
    required_stock_section_count = fields.Integer(
        string="Required Stock Sections",
        compute="_compute_stock_section_readiness",
        compute_sudo=True,
    )
    closed_stock_section_count = fields.Integer(
        string="Closed Stock Sections",
        compute="_compute_stock_section_readiness",
        compute_sudo=True,
    )
    stock_section_readiness = fields.Selection(
        [
            ("legacy", "No Section Setup"),
            ("pending", "Waiting for Stock Sections"),
            ("ready", "All Stock Sections Closed"),
        ],
        compute="_compute_stock_section_readiness",
        compute_sudo=True,
        string="Stock Group Readiness",
    )
    stock_section_status = fields.Char(
        compute="_compute_stock_section_readiness",
        compute_sudo=True,
        string="Stock Group Status",
    )

    @api.depends("branch_id", "closing_date")
    def _compute_stock_section_readiness(self):
        Section = self.env["restaurant.stock.section"].sudo()
        Daily = self.env["restaurant.stock.daily"].sudo()
        for closing in self:
            legacy_daily = Daily.search([
                ("branch_id", "=", closing.branch_id.id),
                ("stock_date", "=", closing.closing_date),
                ("section_id", "=", False),
                ("state", "!=", "cancelled"),
            ], limit=1) if closing.branch_id else Daily.browse()
            if legacy_daily:
                is_closed = legacy_daily.state == "closed"
                closing.required_stock_section_count = 1
                closing.closed_stock_section_count = int(is_closed)
                closing.stock_section_readiness = (
                    "ready" if is_closed else "pending"
                )
                closing.stock_section_status = (
                    self.env._("Legacy Daily Stock is closed.")
                    if is_closed
                    else self.env._("Waiting for the existing Daily Stock sheet.")
                )
                continue
            required = Section.search([
                ("branch_id", "=", closing.branch_id.id),
                ("active", "=", True),
                ("required_for_daily_close", "=", True),
            ]) if closing.branch_id else Section.browse()
            closed = Daily.search([
                ("branch_id", "=", closing.branch_id.id),
                ("stock_date", "=", closing.closing_date),
                ("section_id", "in", required.ids),
                ("state", "=", "closed"),
            ]).section_id
            missing = required - closed
            closing.required_stock_section_count = len(required)
            closing.closed_stock_section_count = len(closed)
            if not required:
                closing.stock_section_readiness = "legacy"
                closing.stock_section_status = self.env._(
                    "This branch has no stock category groups."
                )
            elif missing:
                closing.stock_section_readiness = "pending"
                closing.stock_section_status = self.env._(
                    "Waiting for: %s",
                    ", ".join(missing.mapped("display_name")),
                )
            else:
                closing.stock_section_readiness = "ready"
                closing.stock_section_status = self.env._(
                    "All required stock category groups are closed."
                )

    def _check_required_stock_sections_closed(self):
        for closing in self:
            legacy_daily = self.env["restaurant.stock.daily"].sudo().search([
                ("branch_id", "=", closing.branch_id.id),
                ("stock_date", "=", closing.closing_date),
                ("section_id", "=", False),
                ("state", "!=", "cancelled"),
            ], limit=1)
            if legacy_daily:
                if legacy_daily.state != "closed":
                    raise ValidationError(
                        self.env._(
                            "Close the existing Daily Stock sheet before "
                            "reviewing Reception Closing."
                        )
                    )
                continue
            sections = self.env["restaurant.stock.section"].sudo().search([
                ("branch_id", "=", closing.branch_id.id),
                ("active", "=", True),
                ("required_for_daily_close", "=", True),
            ])
            if not sections:
                continue
            closed_sections = self.env["restaurant.stock.daily"].sudo().search([
                ("branch_id", "=", closing.branch_id.id),
                ("stock_date", "=", closing.closing_date),
                ("section_id", "in", sections.ids),
                ("state", "=", "closed"),
            ]).section_id
            missing = sections - closed_sections
            if missing:
                raise ValidationError(
                    self.env._(
                        "Close every required Kitchen, Bar, and Disposable "
                        "Daily Stock sheet "
                        "before reviewing Reception Closing. Still waiting for: %s",
                        ", ".join(missing.mapped("display_name")),
                    )
                )

    @api.model_create_multi
    def create(self, vals_list):
        closings = super().create(vals_list)
        Event = self.env["restaurant.stock.event"].sudo().with_context(**{
            SYSTEM_EVENT_WRITE_CONTEXT: True,
        })
        for closing in closings:
            unlinked = Event.search([
                ("closing_id", "=", False),
                ("branch_id", "=", closing.branch_id.id),
                ("company_id", "=", closing.company_id.id),
                ("service_date", "=", closing.closing_date),
                ("state", "not in", ["rejected", "cancelled"]),
            ])
            if unlinked:
                unlinked.write({"closing_id": closing.id})
        return closings

    @api.depends(
        "stock_event_ids.event_type",
        "stock_event_ids.state",
        "stock_event_ids.quantity",
        "stock_event_ids.reported_amount",
    )
    def _compute_stock_event_totals(self):
        for closing in self:
            reportable = closing.stock_event_ids.filtered(
                lambda event: event.state not in ("rejected", "cancelled")
            )
            complimentary = reportable.filtered(
                lambda event: event.event_type == "complimentary"
            )
            cancelled = reportable.filtered(
                lambda event: event.event_type == "cancellation"
            )
            closing.complimentary_item_qty = sum(
                complimentary.mapped("quantity")
            )
            closing.total_complimentary_amount = sum(
                complimentary.mapped("reported_amount")
            )
            closing.cancelled_item_qty = sum(cancelled.mapped("quantity"))
            closing.total_cancelled_amount = sum(
                cancelled.mapped("reported_amount")
            )

    @api.model
    def _draft_editable_fields(self):
        return super()._draft_editable_fields() | {"stock_event_ids"}

    def _check_service_events_for_submission(self):
        self.ensure_one()
        draft_events = self.stock_event_ids.filtered(
            lambda event: event.state == "draft"
        )
        if draft_events:
            raise ValidationError(
                self.env._(
                    "Submit or cancel every linked Draft service event before "
                    "submitting the Reception Closing."
                )
            )

        complimentary_refs = {
            (event.order_reference or "").strip().casefold()
            for event in self.stock_event_ids
            if (
                event.event_type == "complimentary"
                and event.state not in ("rejected", "cancelled")
                and (event.order_reference or "").strip()
            )
        }
        conflicting = self.discount_entry_ids.filtered(
            lambda discount: (
                (discount.order_reference or "").strip().casefold()
                in complimentary_refs
                and not discount.separate_complimentary_event_confirmed
            )
        )
        if conflicting:
            raise ValidationError(
                self.env._(
                    "Order / Check %s is recorded as both Discount and "
                    "Complimentary. Confirm that they are separate events or "
                    "correct the entries.",
                    conflicting[0].order_reference,
                )
            )

    def action_submit(self):
        for closing in self:
            closing._check_service_events_for_submission()
        return super().action_submit()

    def action_confirm(self):
        for closing in self:
            pending = closing.stock_event_ids.filtered(
                lambda event: event.state not in (
                    "approved", "rejected", "cancelled"
                )
            )
            if pending:
                raise ValidationError(
                    self.env._(
                        "All linked service events must be approved, rejected, "
                        "or cancelled before the Reception Closing is reviewed."
                    )
                )
            closing._check_required_stock_sections_closed()
        return super().action_confirm()

    def action_cancel(self):
        for closing in self:
            active_events = closing.stock_event_ids.filtered(
                lambda event: event.state not in ("rejected", "cancelled")
            )
            if active_events:
                raise ValidationError(
                    self.env._(
                        "Cancel or reject linked service events before "
                        "cancelling the Reception Closing."
                    )
                )
        return super().action_cancel()

    def _get_whatsapp_summary_extra_lines(self):
        self.ensure_one()
        lines = super()._get_whatsapp_summary_extra_lines()
        return lines + [
            self.env._(
                "Complimentary: %s items | %s",
                self.complimentary_item_qty,
                self._format_summary_amount(
                    self.total_complimentary_amount
                ),
            ),
            self.env._(
                "Cancelled: %s items | %s",
                self.cancelled_item_qty,
                self._format_summary_amount(self.total_cancelled_amount),
            ),
        ]

from dateutil.relativedelta import relativedelta

from odoo import api, fields, models
from odoo.exceptions import AccessError, UserError, ValidationError

from .restaurant_purchase_invoice_handoff import (
    ACCOUNTANT_GROUP,
    OWNER_GROUP,
    require_group,
)


PERIOD_WORKFLOW_CONTEXT = "restaurant_accounting_period_workflow"


class RestaurantAccountingPeriodClose(models.Model):
    _name = "restaurant.accounting.period.close"
    _description = "Restaurant Monthly Accounting Close"
    _inherit = ["mail.thread", "mail.activity.mixin"]
    _order = "month_start desc, company_id"
    _mail_post_access = "read"

    name = fields.Char(required=True, readonly=True, copy=False, default="New", index=True)
    company_id = fields.Many2one(
        "res.company", required=True, default=lambda self: self.env.company,
        ondelete="restrict", index=True, tracking=True,
    )
    currency_id = fields.Many2one(related="company_id.currency_id", readonly=True)
    month_start = fields.Date(
        required=True, default=lambda self: fields.Date.start_of(fields.Date.today(), "month"),
        index=True, tracking=True,
    )
    month_end = fields.Date(compute="_compute_month_end", store=True, readonly=True)
    state = fields.Selection(
        [
            ("draft", "Accounting Preparation"),
            ("awaiting_owner", "Awaiting Owner Approval"),
            ("closed", "Closed"),
        ],
        required=True, default="draft", readonly=True, copy=False,
        tracking=True, index=True,
    )
    daily_sales_count = fields.Integer(compute="_compute_completion")
    daily_sales_pending_count = fields.Integer(compute="_compute_completion")
    confirmed_closing_missing_count = fields.Integer(compute="_compute_completion")
    supplier_invoice_count = fields.Integer(compute="_compute_completion")
    supplier_invoice_pending_count = fields.Integer(compute="_compute_completion")
    payroll_batch_count = fields.Integer(compute="_compute_completion")
    payroll_batch_pending_count = fields.Integer(compute="_compute_completion")
    daily_sales_complete = fields.Boolean(compute="_compute_completion")
    supplier_invoices_complete = fields.Boolean(compute="_compute_completion")
    payroll_complete = fields.Boolean(compute="_compute_completion")
    bank_reconciled = fields.Boolean(tracking=True)
    cash_reconciled = fields.Boolean(tracking=True)
    inventory_reviewed = fields.Boolean(tracking=True)
    tax_reviewed = fields.Boolean(tracking=True)
    accounting_note = fields.Text(tracking=True)
    owner_note = fields.Text(tracking=True)
    submitted_by_id = fields.Many2one("res.users", readonly=True, copy=False)
    submitted_at = fields.Datetime(readonly=True, copy=False)
    approved_by_id = fields.Many2one("res.users", readonly=True, copy=False)
    approved_at = fields.Datetime(readonly=True, copy=False)

    _company_month_unique = models.Constraint(
        "UNIQUE(company_id, month_start)",
        "A monthly Accounting close already exists for this company and month.",
    )

    @api.depends("month_start")
    def _compute_month_end(self):
        for period in self:
            start = fields.Date.to_date(period.month_start)
            period.month_end = start + relativedelta(months=1, days=-1) if start else False

    @api.depends("company_id", "month_start", "month_end")
    def _compute_completion(self):
        Daily = self.env["restaurant.accounting.daily.sales"]
        Closing = self.env["restaurant.daily.closing"]
        Handoff = self.env["restaurant.purchase.invoice.handoff"]
        Payroll = self.env["restaurant.payroll.input.batch"]
        for period in self:
            base = [
                ("company_id", "=", period.company_id.id),
            ]
            dates = [
                ("business_date", ">=", period.month_start),
                ("business_date", "<=", period.month_end),
            ]
            daily = Daily.search(base + dates)
            period.daily_sales_count = len(daily)
            period.daily_sales_pending_count = len(daily.filtered(lambda record: record.state != "posted"))
            missing_closings = Closing.search_count(base + [
                ("closing_date", ">=", period.month_start),
                ("closing_date", "<=", period.month_end),
                ("state", "=", "confirmed"),
                ("accounting_daily_sales_id", "=", False),
            ])
            period.confirmed_closing_missing_count = missing_closings
            period.daily_sales_complete = bool(daily) and not (
                period.daily_sales_pending_count or missing_closings
            )

            handoffs = Handoff.search(base + [
                ("invoice_date", ">=", period.month_start),
                ("invoice_date", "<=", period.month_end),
                ("state", "!=", "cancelled"),
            ])
            period.supplier_invoice_count = len(handoffs)
            period.supplier_invoice_pending_count = len(handoffs.filtered(
                lambda record: record.state not in ("posted", "paid")
            ))
            period.supplier_invoices_complete = not period.supplier_invoice_pending_count

            payroll = Payroll.search(base + [
                ("month_start", "=", period.month_start),
            ])
            period.payroll_batch_count = len(payroll)
            period.payroll_batch_pending_count = len(payroll.filtered(
                lambda record: record.state != "processed"
            ))
            period.payroll_complete = bool(payroll) and not period.payroll_batch_pending_count

    @api.model_create_multi
    def create(self, vals_list):
        require_group(self.env, ACCOUNTANT_GROUP)
        prepared = []
        for vals in vals_list:
            values = dict(vals)
            company = self.env["res.company"].browse(
                values.get("company_id", self.env.company.id)
            )
            start = fields.Date.to_date(values.get("month_start") or fields.Date.today())
            if start.day != 1:
                raise ValidationError(self.env._("Accounting month must start on the first day."))
            if company not in self.env.companies:
                raise AccessError(self.env._("Switch to the selected company first."))
            values.update({
                "name": self.env["ir.sequence"].next_by_code(
                    "restaurant.accounting.period.close"
                ) or "New",
                "state": "draft",
            })
            prepared.append(values)
        return super().create(prepared)

    def write(self, vals):
        if self.env.context.get(PERIOD_WORKFLOW_CONTEXT):
            return super().write(vals)
        self.check_access("write")
        if "state" in vals:
            raise AccessError(self.env._("Use the workflow buttons to change status."))
        if all(period.state == "draft" for period in self):
            require_group(self.env, ACCOUNTANT_GROUP)
            editable = {
                "company_id", "month_start",
                "bank_reconciled", "cash_reconciled", "inventory_reviewed",
                "tax_reviewed", "accounting_note",
            }
            if set(vals) - editable:
                raise AccessError(self.env._("Only the monthly checklist can be edited."))
            if "company_id" in vals:
                company = self.env["res.company"].browse(vals["company_id"])
                if company not in self.env.companies:
                    raise AccessError(self.env._("Switch to the selected company first."))
            if "month_start" in vals:
                start = fields.Date.to_date(vals["month_start"])
                if not start or start.day != 1:
                    raise ValidationError(self.env._("Accounting month must start on the first day."))
            return super().write(vals)
        if set(vals) <= {"owner_note"} and all(
            period.state == "awaiting_owner" for period in self
        ):
            require_group(self.env, OWNER_GROUP)
            return super().write(vals)
        raise AccessError(self.env._("This Accounting period is locked."))

    def unlink(self):
        require_group(self.env, ACCOUNTANT_GROUP)
        if any(period.state != "draft" for period in self):
            raise AccessError(self.env._("Only draft Accounting periods can be deleted."))
        return super().unlink()

    def _workflow_write(self, vals):
        return self.with_context(**{PERIOD_WORKFLOW_CONTEXT: True}).write(vals)

    def action_submit_owner(self):
        self.ensure_one()
        require_group(self.env, ACCOUNTANT_GROUP)
        self.check_access("write")
        self.lock_for_update()
        if self.state != "draft":
            raise UserError(self.env._("Only draft periods can be submitted."))
        self.invalidate_recordset()
        blockers = []
        if not self.daily_sales_complete:
            blockers.append(self.env._("daily sales are not fully posted"))
        if not self.supplier_invoices_complete:
            blockers.append(self.env._("supplier invoices are still pending"))
        if not self.payroll_complete:
            blockers.append(self.env._("payroll inputs are not fully processed"))
        manual = {
            "bank_reconciled": self.env._("bank reconciliation"),
            "cash_reconciled": self.env._("cash reconciliation"),
            "inventory_reviewed": self.env._("inventory review"),
            "tax_reviewed": self.env._("tax review"),
        }
        blockers.extend(label for field, label in manual.items() if not self[field])
        if blockers:
            raise ValidationError(self.env._(
                "Complete the monthly close first: %s.",
                "; ".join(blockers),
            ))
        self._workflow_write({
            "state": "awaiting_owner",
            "submitted_by_id": self.env.uid,
            "submitted_at": fields.Datetime.now(),
        })
        return True

    def action_approve_close(self):
        self.ensure_one()
        require_group(self.env, OWNER_GROUP)
        self.check_access("write")
        self.lock_for_update()
        if self.state != "awaiting_owner":
            raise UserError(self.env._("This period is not awaiting Owner approval."))
        self._workflow_write({
            "state": "closed",
            "approved_by_id": self.env.uid,
            "approved_at": fields.Datetime.now(),
        })
        return True

    def action_return_to_accounting(self):
        self.ensure_one()
        require_group(self.env, OWNER_GROUP)
        self.check_access("write")
        self.lock_for_update()
        if self.state != "awaiting_owner":
            raise UserError(self.env._("This period is not awaiting Owner approval."))
        if not (self.owner_note or "").strip():
            raise ValidationError(self.env._("Enter the reason for returning the period."))
        self._workflow_write({"state": "draft"})
        return True

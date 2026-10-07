from collections import defaultdict

from odoo import api, fields, models
from odoo.exceptions import AccessError, UserError, ValidationError

from .reception_security import (
    MANAGER_GROUP,
    RECEPTION_GROUP,
    check_employee_company,
    lock_records,
    require_assigned_branches,
    require_draft,
    require_role,
)


class RestaurantWaiterSalesClosing(models.Model):
    _name = "restaurant.waiter.sales.closing"
    _description = "Waiter Sales and Tips Closing"
    _inherit = ["mail.thread", "mail.activity.mixin"]
    _order = "business_date desc, id desc"
    _mail_post_access = "read"

    name = fields.Char(
        string="Reference",
        required=True,
        readonly=True,
        copy=False,
        default="New",
        index=True,
    )
    branch_id = fields.Many2one(
        "restaurant.branch",
        required=True,
        ondelete="restrict",
        index=True,
        tracking=True,
    )
    business_date = fields.Date(
        required=True,
        default=fields.Date.context_today,
        index=True,
        tracking=True,
    )
    state = fields.Selection(
        [
            ("draft", "Draft"),
            ("submitted", "Submitted"),
            ("closed", "Closed"),
            ("cancelled", "Cancelled"),
        ],
        required=True,
        default="draft",
        readonly=True,
        copy=False,
        tracking=True,
        index=True,
    )
    reception_user_id = fields.Many2one(
        "res.users",
        string="Prepared By",
        required=True,
        readonly=True,
        default=lambda self: self.env.user,
    )
    bill_ids = fields.One2many(
        "restaurant.waiter.sales.bill",
        "waiter_closing_id",
        string="Bills",
        copy=True,
    )
    summary_line_ids = fields.One2many(
        "restaurant.waiter.sales.summary",
        "waiter_closing_id",
        string="Waiter Summary",
        readonly=True,
        copy=False,
    )
    total_sales = fields.Monetary(
        compute="_compute_totals", store=True, readonly=True, tracking=True,
    )
    dine_in_sales = fields.Monetary(
        compute="_compute_totals", store=True, readonly=True,
    )
    pickup_sales = fields.Monetary(
        compute="_compute_totals", store=True, readonly=True,
    )
    delivery_sales = fields.Monetary(
        compute="_compute_totals", store=True, readonly=True,
    )
    pickup_delivery_sales = fields.Monetary(
        compute="_compute_totals", store=True, readonly=True,
    )
    waiter_sales = fields.Monetary(
        string="Waiter Sales",
        compute="_compute_totals",
        store=True,
        readonly=True,
    )
    cash_tips = fields.Monetary(
        compute="_compute_totals", store=True, readonly=True,
    )
    card_tips = fields.Monetary(
        compute="_compute_totals", store=True, readonly=True,
    )
    total_tips = fields.Monetary(
        compute="_compute_totals", store=True, readonly=True, tracking=True,
    )
    waiter_tip_contribution = fields.Monetary(
        compute="_compute_totals", store=True, readonly=True,
    )
    guest_count = fields.Integer(
        compute="_compute_totals", store=True, readonly=True,
    )
    bill_count = fields.Integer(
        compute="_compute_totals", store=True, readonly=True,
    )
    dine_in_bill_count = fields.Integer(
        compute="_compute_totals", store=True, readonly=True,
    )
    pickup_bill_count = fields.Integer(
        compute="_compute_totals", store=True, readonly=True,
    )
    delivery_bill_count = fields.Integer(
        compute="_compute_totals", store=True, readonly=True,
    )
    review_count = fields.Integer(
        string="Reviews",
        compute="_compute_totals",
        store=True,
        readonly=True,
    )
    notes = fields.Text(tracking=True)
    submitted_by_id = fields.Many2one(
        "res.users", string="Submitted By", readonly=True, copy=False,
    )
    submitted_at = fields.Datetime(readonly=True, copy=False)
    closed_by_id = fields.Many2one(
        "res.users", string="Closed By", readonly=True, copy=False,
    )
    closed_at = fields.Datetime(readonly=True, copy=False)
    correction_reason = fields.Text(
        copy=False,
        help="Required before management returns a submitted closing to Draft.",
    )
    last_reopened_by_id = fields.Many2one(
        "res.users", string="Last Reopened By", readonly=True, copy=False,
    )
    last_reopened_at = fields.Datetime(readonly=True, copy=False)
    reopen_count = fields.Integer(readonly=True, copy=False)
    cancelled_by_id = fields.Many2one(
        "res.users", string="Cancelled By", readonly=True, copy=False,
    )
    cancelled_at = fields.Datetime(readonly=True, copy=False)
    company_id = fields.Many2one(
        related="branch_id.company_id", store=True, index=True,
    )
    currency_id = fields.Many2one(
        related="company_id.currency_id", store=True,
    )

    _active_waiter_closing_unique = models.UniqueIndex(
        "(branch_id, business_date) WHERE state != 'cancelled'",
        "An active Waiter Sales & Tips closing already exists for this branch and date.",
    )

    @api.depends(
        "bill_ids.service_type",
        "bill_ids.bill_amount",
        "bill_ids.guest_count",
        "bill_ids.cash_tip_amount",
        "bill_ids.card_tip_amount",
        "bill_ids.review_count",
    )
    def _compute_totals(self):
        for closing in self:
            bills = closing.bill_ids
            dine_in = bills.filtered(lambda bill: bill.service_type == "dine_in")
            pickup = bills.filtered(lambda bill: bill.service_type == "pickup")
            delivery = bills.filtered(lambda bill: bill.service_type == "delivery")
            closing.dine_in_sales = sum(dine_in.mapped("bill_amount"))
            closing.pickup_sales = sum(pickup.mapped("bill_amount"))
            closing.delivery_sales = sum(delivery.mapped("bill_amount"))
            closing.pickup_delivery_sales = (
                closing.pickup_sales + closing.delivery_sales
            )
            closing.waiter_sales = closing.dine_in_sales
            closing.total_sales = sum(bills.mapped("bill_amount"))
            closing.cash_tips = sum(bills.mapped("cash_tip_amount"))
            closing.card_tips = sum(bills.mapped("card_tip_amount"))
            closing.total_tips = closing.cash_tips + closing.card_tips
            closing.waiter_tip_contribution = sum(dine_in.mapped("tip_total"))
            closing.guest_count = sum(dine_in.mapped("guest_count"))
            closing.bill_count = len(bills)
            closing.dine_in_bill_count = len(dine_in)
            closing.pickup_bill_count = len(pickup)
            closing.delivery_bill_count = len(delivery)
            closing.review_count = sum(bills.mapped("review_count"))

    @api.model_create_multi
    def create(self, vals_list):
        require_role(self.env, RECEPTION_GROUP)
        protected = {
            "name", "state", "reception_user_id", "summary_line_ids",
            "total_sales", "dine_in_sales", "pickup_sales",
            "delivery_sales", "pickup_delivery_sales", "waiter_sales",
            "cash_tips", "card_tips", "total_tips",
            "waiter_tip_contribution", "guest_count", "bill_count",
            "dine_in_bill_count", "pickup_bill_count",
            "delivery_bill_count", "review_count", "submitted_by_id",
            "submitted_at", "closed_by_id", "closed_at",
            "last_reopened_by_id", "last_reopened_at", "reopen_count",
            "cancelled_by_id", "cancelled_at", "company_id", "currency_id",
        }
        prepared = []
        for values in vals_list:
            if set(values) & protected:
                raise AccessError(
                    self.env._("Workflow, audit, and calculated fields are system-controlled.")
                )
            values = dict(values)
            values.update({
                "name": self.env["ir.sequence"].next_by_code(
                    "restaurant.waiter.sales.closing"
                ),
                "state": "draft",
                "reception_user_id": self.env.uid,
            })
            prepared.append(values)
        records = super().create(prepared)
        require_assigned_branches(records.branch_id)
        records._sync_daily_closing_links()
        return records

    def write(self, vals):
        lock_records(self)
        if "state" in vals:
            if set(vals) != {"state"}:
                raise AccessError(
                    self.env._("Save your changes before changing the workflow state.")
                )
            target = vals["state"]
            for closing in self:
                closing._check_transition(target)
            for closing in self:
                values = {"state": target}
                now = fields.Datetime.now()
                correction_reason = closing.correction_reason
                if target == "submitted":
                    values.update(submitted_by_id=self.env.uid, submitted_at=now)
                elif target == "closed":
                    values.update(closed_by_id=self.env.uid, closed_at=now)
                elif target == "draft":
                    values.update(
                        last_reopened_by_id=self.env.uid,
                        last_reopened_at=now,
                        reopen_count=closing.reopen_count + 1,
                        correction_reason=False,
                    )
                elif target == "cancelled":
                    values.update(cancelled_by_id=self.env.uid, cancelled_at=now)
                super(RestaurantWaiterSalesClosing, closing).write(values)
                if target == "draft":
                    closing.message_post(body=self.env._(
                        "Waiter closing reopened by %s. Reason: %s",
                        self.env.user.name,
                        correction_reason,
                    ))
                closing.message_post(body=self.env._(
                    "Workflow changed to %s by %s.",
                    dict(self._fields["state"].selection)[target],
                    self.env.user.name,
                ))
            self.flush_recordset(["state"])
            self._sync_daily_closing_links()
            return True

        if (
            all(closing.state == "submitted" for closing in self)
            and self.env.user.has_group(MANAGER_GROUP)
        ):
            if set(vals) - {"correction_reason"}:
                raise AccessError(
                    self.env._("Management can only enter the correction reason.")
                )
            require_assigned_branches(self.branch_id)
            return super().write(vals)

        if set(vals) - {"branch_id", "business_date", "bill_ids", "notes"}:
            raise AccessError(
                self.env._("Workflow, audit, and calculated fields cannot be changed manually.")
            )
        require_role(self.env, RECEPTION_GROUP)
        require_draft(self)
        require_assigned_branches(self.branch_id)
        if "branch_id" in vals:
            require_assigned_branches(
                self.env["restaurant.branch"].browse(vals["branch_id"])
            )
        linked_daily_closings = self.env["restaurant.daily.closing"].sudo().search([
            ("waiter_sales_closing_id", "in", self.ids),
        ])
        result = super().write(vals)
        if "branch_id" in vals:
            self.bill_ids.filtered("employee_id")._check_service_details()
        if set(vals) & {"branch_id", "business_date"}:
            linked_daily_closings.with_context(waiter_closing_sync=True).write({
                "waiter_sales_closing_id": False,
            })
            self._sync_daily_closing_links()
        return result

    def unlink(self):
        lock_records(self, "unlink")
        require_role(self.env, RECEPTION_GROUP)
        require_draft(self)
        require_assigned_branches(self.branch_id)
        linked = self.env["restaurant.daily.closing"].sudo().search([
            ("waiter_sales_closing_id", "in", self.ids),
        ])
        linked.with_context(waiter_closing_sync=True).write({
            "waiter_sales_closing_id": False,
        })
        return super().unlink()

    def _check_transition(self, target):
        self.ensure_one()
        transitions = {
            ("draft", "submitted"): RECEPTION_GROUP,
            ("submitted", "closed"): MANAGER_GROUP,
            ("submitted", "draft"): MANAGER_GROUP,
            ("draft", "cancelled"): RECEPTION_GROUP,
            ("submitted", "cancelled"): MANAGER_GROUP,
        }
        group = transitions.get((self.state, target))
        if not group:
            raise UserError(self.env._("This waiter closing transition is not allowed."))
        require_role(self.env, group)
        require_assigned_branches(self.branch_id)
        if self.state == "draft" and target == "submitted" and not self.bill_ids:
            raise ValidationError(
                self.env._("Enter at least one bill before submitting the waiter closing.")
            )
        if (
            self.state == "submitted"
            and target == "draft"
            and not (self.correction_reason or "").strip()
        ):
            raise ValidationError(
                self.env._("Enter a correction reason before returning to Draft.")
            )

    def action_submit(self):
        self.ensure_one()
        return self.write({"state": "submitted"})

    def action_close(self):
        self.ensure_one()
        return self.write({"state": "closed"})

    def action_return_to_draft(self):
        self.ensure_one()
        return self.write({"state": "draft"})

    def action_cancel(self):
        self.ensure_one()
        return self.write({"state": "cancelled"})

    def _rebuild_summaries(self):
        summary_model = self.env["restaurant.waiter.sales.summary"].sudo()
        bill_model = self.env["restaurant.waiter.sales.bill"]
        for closing in self:
            summary_model.search([
                ("waiter_closing_id", "=", closing.id),
            ]).unlink()
            bills = closing.bill_ids.sorted("id")
            by_waiter = defaultdict(lambda: bill_model)
            for bill in bills.filtered(
                lambda item: item.service_type == "dine_in"
            ):
                by_waiter[bill.employee_id.id] |= bill

            values_list = []
            for employee_id, waiter_bills in by_waiter.items():
                sales = sum(waiter_bills.mapped("bill_amount"))
                tips = sum(waiter_bills.mapped("tip_total"))
                values_list.append({
                    "waiter_closing_id": closing.id,
                    "summary_type": "waiter",
                    "summary_name": waiter_bills.employee_id[:1].display_name,
                    "employee_id": employee_id,
                    "table_references": ", ".join(
                        waiter_bills.mapped("table_reference")
                    ),
                    "guest_counts": ", ".join(
                        str(value) for value in waiter_bills.mapped("guest_count")
                    ),
                    "bill_count": len(waiter_bills),
                    "table_count": len(waiter_bills),
                    "guest_count": sum(waiter_bills.mapped("guest_count")),
                    "sales_amount": sales,
                    "cash_tip_amount": sum(
                        waiter_bills.mapped("cash_tip_amount")
                    ),
                    "card_tip_amount": sum(
                        waiter_bills.mapped("card_tip_amount")
                    ),
                    "tips_amount": tips,
                    "tip_percentage": 100 * tips / sales if sales else 0,
                    "review_count": sum(waiter_bills.mapped("review_count")),
                })

            takeaway = bills.filtered(
                lambda item: item.service_type in ("pickup", "delivery")
            )
            if takeaway:
                sales = sum(takeaway.mapped("bill_amount"))
                tips = sum(takeaway.mapped("tip_total"))
                values_list.append({
                    "waiter_closing_id": closing.id,
                    "summary_type": "pickup_delivery",
                    "summary_name": self.env._("Pickup / Delivery"),
                    "bill_count": len(takeaway),
                    "sales_amount": sales,
                    "cash_tip_amount": sum(
                        takeaway.mapped("cash_tip_amount")
                    ),
                    "card_tip_amount": sum(
                        takeaway.mapped("card_tip_amount")
                    ),
                    "tips_amount": tips,
                    "tip_percentage": 100 * tips / sales if sales else 0,
                    "review_count": sum(takeaway.mapped("review_count")),
                })
            if values_list:
                summary_model.create(values_list)

    def _sync_daily_closing_links(self):
        daily_model = self.env["restaurant.daily.closing"].sudo().with_context(
            waiter_closing_sync=True,
        )
        for closing in self:
            linked = daily_model.search([
                ("waiter_sales_closing_id", "=", closing.id),
            ])
            if closing.state == "cancelled":
                linked.write({"waiter_sales_closing_id": False})
                continue
            daily = daily_model.search([
                ("branch_id", "=", closing.branch_id.id),
                ("closing_date", "=", closing.business_date),
                ("state", "!=", "cancelled"),
                ("service_tracking_mode", "=", "standalone"),
            ], limit=1)
            (linked - daily).write({"waiter_sales_closing_id": False})
            if daily and daily.waiter_sales_closing_id != closing:
                daily.write({"waiter_sales_closing_id": closing.id})


class RestaurantWaiterSalesBill(models.Model):
    _name = "restaurant.waiter.sales.bill"
    _description = "Waiter Sales and Tips Bill"
    _order = "id"

    waiter_closing_id = fields.Many2one(
        "restaurant.waiter.sales.closing",
        required=True,
        ondelete="cascade",
        index=True,
    )
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
        "hr.employee", string="Waiter", ondelete="restrict", index=True,
    )
    bill_reference = fields.Char(string="Bill / Check", required=True, index=True)
    table_reference = fields.Char(string="Table")
    guest_count = fields.Integer(string="Guests", default=0)
    bill_amount = fields.Monetary(required=True, default=0)
    cash_tip_amount = fields.Monetary(string="Cash Tip", required=True, default=0)
    card_tip_amount = fields.Monetary(string="Card Tip", required=True, default=0)
    tip_total = fields.Monetary(
        string="Total Tip", compute="_compute_tip_total", store=True,
    )
    review_count = fields.Integer(string="Reviews", default=0)
    note = fields.Char()
    entered_by_id = fields.Many2one(
        "res.users", required=True, readonly=True, default=lambda self: self.env.user,
    )
    entered_at = fields.Datetime(
        required=True, readonly=True, default=fields.Datetime.now,
    )
    branch_id = fields.Many2one(
        related="waiter_closing_id.branch_id", store=True, index=True,
    )
    business_date = fields.Date(
        related="waiter_closing_id.business_date", store=True, index=True,
    )
    state = fields.Selection(
        related="waiter_closing_id.state", store=True, index=True,
    )
    company_id = fields.Many2one(
        related="waiter_closing_id.company_id", store=True, index=True,
    )
    currency_id = fields.Many2one(
        related="waiter_closing_id.currency_id", store=True,
    )

    _bill_reference_unique = models.UniqueIndex(
        "(waiter_closing_id, bill_reference)",
        "This bill/check is already recorded in the waiter closing.",
    )
    _nonnegative_values = models.Constraint(
        "CHECK (guest_count >= 0 AND bill_amount >= 0 "
        "AND cash_tip_amount >= 0 AND card_tip_amount >= 0 "
        "AND review_count >= 0)",
        "Guests, bills, tips, and reviews cannot be negative.",
    )

    @api.depends("cash_tip_amount", "card_tip_amount")
    def _compute_tip_total(self):
        for bill in self:
            bill.tip_total = bill.cash_tip_amount + bill.card_tip_amount

    @api.onchange("service_type")
    def _onchange_service_type(self):
        if self.service_type in ("pickup", "delivery"):
            self.employee_id = False
            self.table_reference = False
            self.guest_count = 0

    @api.constrains(
        "service_type", "employee_id", "table_reference", "guest_count",
        "waiter_closing_id", "bill_reference",
    )
    def _check_service_details(self):
        for bill in self:
            if not (bill.bill_reference or "").strip():
                raise ValidationError(self.env._("Enter the bill/check reference."))
            if bill.service_type == "dine_in":
                if not bill.employee_id:
                    raise ValidationError(
                        self.env._("Select the waiter for every dine-in bill.")
                    )
                if not (bill.table_reference or "").strip():
                    raise ValidationError(
                        self.env._("Enter the table for every dine-in bill.")
                    )
                check_employee_company(bill)
            elif bill.employee_id or (bill.table_reference or "").strip():
                raise ValidationError(self.env._(
                    "Pickup and delivery bills stay separate: do not assign a waiter or table."
                ))
            elif bill.guest_count:
                raise ValidationError(self.env._(
                    "Guest count is recorded on dine-in bills only."
                ))

    @api.model_create_multi
    def create(self, vals_list):
        require_role(self.env, RECEPTION_GROUP)
        editable = {
            "waiter_closing_id", "service_type", "employee_id",
            "bill_reference", "table_reference", "guest_count",
            "bill_amount", "cash_tip_amount", "card_tip_amount",
            "review_count", "note",
        }
        if any(set(values) - editable for values in vals_list):
            raise AccessError(
                self.env._("Bill audit and calculated fields are system-controlled.")
            )
        defaults = self.default_get(["waiter_closing_id"])
        closing_ids = [
            values.get("waiter_closing_id", defaults.get("waiter_closing_id"))
            for values in vals_list
        ]
        if not all(closing_ids):
            raise ValidationError(self.env._("Select the Waiter Sales & Tips closing."))
        closings = self.env["restaurant.waiter.sales.closing"].browse(closing_ids)
        lock_records(closings)
        require_draft(closings)
        require_assigned_branches(closings.branch_id)
        prepared = []
        for values in vals_list:
            values = dict(values)
            if "bill_reference" in values:
                values["bill_reference"] = (values["bill_reference"] or "").strip()
            if "table_reference" in values:
                values["table_reference"] = (values["table_reference"] or "").strip()
            prepared.append(values)
        bills = super().create(prepared)
        bills.waiter_closing_id._rebuild_summaries()
        return bills

    def write(self, vals):
        self.check_access("write")
        require_role(self.env, RECEPTION_GROUP)
        editable = {
            "service_type", "employee_id", "bill_reference", "table_reference",
            "guest_count", "bill_amount", "cash_tip_amount",
            "card_tip_amount", "review_count", "note",
        }
        if set(vals) - editable:
            raise AccessError(
                self.env._("Bills cannot be moved or their audit fields overwritten.")
            )
        closings = self.waiter_closing_id
        lock_records(closings)
        require_draft(closings)
        require_assigned_branches(closings.branch_id)
        values = dict(vals)
        if "bill_reference" in values:
            values["bill_reference"] = (values["bill_reference"] or "").strip()
        if "table_reference" in values:
            values["table_reference"] = (values["table_reference"] or "").strip()
        result = super().write(values)
        closings._rebuild_summaries()
        return result

    def unlink(self):
        self.check_access("unlink")
        require_role(self.env, RECEPTION_GROUP)
        closings = self.waiter_closing_id
        lock_records(closings)
        require_draft(closings)
        require_assigned_branches(closings.branch_id)
        result = super().unlink()
        closings._rebuild_summaries()
        return result


class RestaurantWaiterSalesSummary(models.Model):
    _name = "restaurant.waiter.sales.summary"
    _description = "Generated Waiter Sales and Tips Summary"
    _order = "summary_type, employee_id, id"
    _rec_name = "summary_name"

    waiter_closing_id = fields.Many2one(
        "restaurant.waiter.sales.closing",
        required=True,
        ondelete="cascade",
        index=True,
    )
    summary_type = fields.Selection(
        [("waiter", "Waiter"), ("pickup_delivery", "Pickup / Delivery")],
        required=True,
        readonly=True,
        index=True,
    )
    summary_name = fields.Char(string="Name", required=True, readonly=True)
    employee_id = fields.Many2one(
        "hr.employee", string="Waiter", ondelete="restrict", readonly=True,
    )
    table_references = fields.Char(string="Tables", readonly=True)
    guest_counts = fields.Char(string="Guests per Bill", readonly=True)
    bill_count = fields.Integer(string="Bills", readonly=True)
    table_count = fields.Integer(string="Tables Served", readonly=True)
    guest_count = fields.Integer(string="Total Guests", readonly=True)
    sales_amount = fields.Monetary(string="Total", readonly=True)
    cash_tip_amount = fields.Monetary(string="Cash Tips", readonly=True)
    card_tip_amount = fields.Monetary(string="Card Tips", readonly=True)
    tips_amount = fields.Monetary(string="Tips", readonly=True)
    tip_percentage = fields.Float(string="Tips %", readonly=True, aggregator=False)
    review_count = fields.Integer(string="Reviews", readonly=True)
    branch_id = fields.Many2one(
        related="waiter_closing_id.branch_id", store=True, index=True,
    )
    business_date = fields.Date(
        related="waiter_closing_id.business_date", store=True, index=True,
    )
    state = fields.Selection(
        related="waiter_closing_id.state", store=True, index=True,
    )
    company_id = fields.Many2one(
        related="waiter_closing_id.company_id", store=True, index=True,
    )
    currency_id = fields.Many2one(
        related="waiter_closing_id.currency_id", store=True,
    )

    @api.model_create_multi
    def create(self, vals_list):
        if not self.env.su:
            raise AccessError(self.env._("Waiter summaries are generated from bills."))
        return super().create(vals_list)

    def write(self, vals):
        if not self.env.su:
            raise AccessError(self.env._("Waiter summaries are generated from bills."))
        return super().write(vals)

    def unlink(self):
        if not self.env.su:
            raise AccessError(self.env._("Waiter summaries are generated from bills."))
        return super().unlink()

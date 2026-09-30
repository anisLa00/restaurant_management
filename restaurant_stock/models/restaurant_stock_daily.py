from odoo import api, fields, models
from odoo.exceptions import AccessError, UserError, ValidationError

from .stock_security import (
    MANAGER_GROUP,
    STOCKKEEPER_GROUP,
    lock_records,
    require_assigned_branches,
    require_role,
)


class RestaurantStockDaily(models.Model):
    _name = "restaurant.stock.daily"
    _description = "Restaurant Daily Stock"
    _inherit = ["mail.thread", "mail.activity.mixin"]
    _order = "stock_date desc, id desc"
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

    stock_date = fields.Date(
        required=True,
        default=fields.Date.context_today,
        index=True,
        tracking=True,
    )

    stockkeeper_id = fields.Many2one(
        "res.users",
        required=True,
        readonly=True,
        default=lambda self: self.env.user,
        tracking=True,
    )

    opened_by_id = fields.Many2one(
        "res.users",
        string="Opening Confirmed By",
        readonly=True,
        copy=False,
    )

    opened_at = fields.Datetime(
        readonly=True,
        copy=False,
    )

    reviewed_by_id = fields.Many2one(
        "res.users",
        string="Closing Reviewed By",
        readonly=True,
        copy=False,
    )

    reviewed_at = fields.Datetime(
        readonly=True,
        copy=False,
    )

    state = fields.Selection(
        [
            ("draft", "Draft"),
            ("opened", "Opened"),
            ("closing_review", "Closing Review"),
            ("closed", "Closed"),
            ("cancelled", "Cancelled"),
        ],
        default="draft",
        required=True,
        readonly=True,
        copy=False,
        tracking=True,
        index=True,
    )

    line_ids = fields.One2many(
        "restaurant.stock.daily.line",
        "daily_id",
        string="Stock Lines",
        copy=True,
    )

    note = fields.Text(tracking=True)

    company_id = fields.Many2one(
        "res.company",
        related="branch_id.company_id",
        store=True,
        readonly=True,
        index=True,
    )

    _active_daily_stock_unique = models.UniqueIndex(
        "(branch_id, stock_date) WHERE state != 'cancelled'",
        "An active daily stock sheet already exists for this branch and date.",
    )

    @api.model_create_multi
    def create(self, vals_list):
        require_role(self.env, STOCKKEEPER_GROUP)

        prepared = []

        for values in vals_list:
            values = dict(values)

            if values.get("state", "draft") != "draft":
                raise AccessError(
                    self.env._("Daily stock sheets must be created in draft.")
                )

            protected = {
                "stockkeeper_id",
                "opened_by_id",
                "opened_at",
                "reviewed_by_id",
                "reviewed_at",
                "company_id",
            }

            if set(values) & protected:
                raise AccessError(
                    self.env._(
                        "System and review fields cannot be supplied manually."
                    )
                )

            values.update({
                "state": "draft",
                "stockkeeper_id": self.env.uid,
                "opened_by_id": False,
                "opened_at": False,
                "reviewed_by_id": False,
                "reviewed_at": False,
            })

            prepared.append(values)

        records = super().create(prepared)

        require_assigned_branches(records.branch_id)

        return records

    def write(self, vals):
        lock_records(self)

        if "state" in vals:
            if set(vals) != {"state"}:
                raise AccessError(
                    self.env._(
                        "Save your changes before changing the workflow state."
                    )
                )

            target = vals["state"]

            for record in self:
                record._check_transition(target)

            values = dict(vals)

            if target == "opened":
                values.update({
                    "opened_by_id": self.env.uid,
                    "opened_at": fields.Datetime.now(),
                })

            if target in ("closed", "opened", "cancelled"):
                if any(record.state == "closing_review" for record in self):
                    values.update({
                        "reviewed_by_id": self.env.uid,
                        "reviewed_at": fields.Datetime.now(),
                    })

            result = super().write(values)

            self.flush_recordset(["state"])

            for record in self:
                label = dict(self._fields["state"].selection)[target]

                record.message_post(
                    body=self.env._(
                        "Stock workflow changed to %s by %s.",
                        label,
                        self.env.user.name,
                    )
                )

            return result

        require_role(self.env, STOCKKEEPER_GROUP)
        require_assigned_branches(self.branch_id)

        if any(record.state not in ("draft", "opened") for record in self):
            raise AccessError(
                self.env._(
                    "Only draft or opened stock sheets can be edited."
                )
            )

        editable = {"branch_id", "stock_date", "line_ids", "note"}

        if set(vals) - editable:
            raise AccessError(
                self.env._(
                    "System and review fields cannot be modified manually."
                )
            )

        if "branch_id" in vals:
            if any(record.state != "draft" for record in self):
                raise AccessError(
                    self.env._(
                        "The branch can only be changed while the sheet is draft."
                    )
                )

            require_assigned_branches(
                self.env["restaurant.branch"].browse(vals["branch_id"])
            )

        if "stock_date" in vals and any(
            record.state != "draft" for record in self
        ):
            raise AccessError(
                self.env._(
                    "The stock date can only be changed while the sheet is draft."
                )
            )

        return super().write(vals)

    def unlink(self):
        lock_records(self, "unlink")

        require_role(self.env, STOCKKEEPER_GROUP)
        require_assigned_branches(self.branch_id)

        if any(record.state != "draft" for record in self):
            raise AccessError(
                self.env._(
                    "Only draft daily stock sheets can be deleted."
                )
            )

        return super().unlink()

    def _check_transition(self, target):
        self.ensure_one()

        transitions = {
            ("draft", "opened"): STOCKKEEPER_GROUP,
            ("opened", "closing_review"): STOCKKEEPER_GROUP,

            ("closing_review", "closed"): MANAGER_GROUP,
            ("closing_review", "opened"): MANAGER_GROUP,

            ("draft", "cancelled"): STOCKKEEPER_GROUP,
            ("opened", "cancelled"): MANAGER_GROUP,
            ("closing_review", "cancelled"): MANAGER_GROUP,
        }

        group = transitions.get((self.state, target))

        if not group:
            raise UserError(
                self.env._(
                    "This stock workflow transition is not allowed."
                )
            )

        require_role(self.env, group)
        require_assigned_branches(self.branch_id)

    def action_confirm_opening(self):
        self.ensure_one()
        return self.write({"state": "opened"})

    def action_submit_closing(self):
        self.ensure_one()
        return self.write({"state": "closing_review"})

    def action_close(self):
        self.ensure_one()
        return self.write({"state": "closed"})

    def action_return_to_open(self):
        self.ensure_one()
        return self.write({"state": "opened"})

    def action_cancel(self):
        self.ensure_one()
        return self.write({"state": "cancelled"})


class RestaurantStockDailyLine(models.Model):
    _name = "restaurant.stock.daily.line"
    _description = "Restaurant Daily Stock Line"
    _order = "product_id"

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
    )

    stock_date = fields.Date(
        related="daily_id.stock_date",
        store=True,
        readonly=True,
    )

    company_id = fields.Many2one(
        related="daily_id.company_id",
        store=True,
        readonly=True,
    )

    product_id = fields.Many2one(
        "product.product",
        required=True,
        index=True,
    )

    uom_id = fields.Many2one(
        "uom.uom",
        related="product_id.uom_id",
        readonly=True,
    )

    opening_qty = fields.Float(string="Opening Qty", default=0.0)

    received_qty = fields.Float(
        string="Received Qty",
        default=0.0,
    )

    incoming_transfer_qty = fields.Float(
        string="Incoming Transfer",
        default=0.0,
    )

    outgoing_transfer_qty = fields.Float(
        string="Outgoing Transfer",
        default=0.0,
    )

    consumption_qty = fields.Float(
        string="Consumption Qty",
        default=0.0,
        help=(
            "Manual for now. Later this will be integrated "
            "with recipes and POS consumption."
        ),
    )

    waste_qty = fields.Float(
        string="Waste Qty",
        default=0.0,
    )

    damaged_qty = fields.Float(
        string="Damaged Qty",
        default=0.0,
    )

    expected_closing_qty = fields.Float(
        string="Expected Closing",
        compute="_compute_expected_closing",
        store=True,
    )

    actual_closing_qty = fields.Float(
        string="Actual Closing",
        default=0.0,
    )

    difference_qty = fields.Float(
        string="Difference",
        compute="_compute_difference",
        store=True,
    )

    note = fields.Char()

    _daily_product_unique = models.Constraint(
        "UNIQUE(daily_id, product_id)",
        "A product can only appear once in the same daily stock sheet.",
    )
    @api.model_create_multi
    def create(self, vals_list):
        require_role(self.env, STOCKKEEPER_GROUP)

        records = super().create(vals_list)

        require_assigned_branches(records.branch_id)

        if any(record.daily_id.state not in ("draft", "opened") for record in records):
            raise AccessError(
                self.env._(
                    "Stock lines can only be added to draft or opened stock sheets."
                )
            )

        return records


    def write(self, vals):
        require_role(self.env, STOCKKEEPER_GROUP)
        require_assigned_branches(self.branch_id)

        if any(record.daily_id.state not in ("draft", "opened") for record in self):
            raise AccessError(
                self.env._(
                    "Stock lines can only be edited while the stock sheet is draft or opened."
                )
            )

        if "daily_id" in vals:
            target_daily = self.env["restaurant.stock.daily"].browse(vals["daily_id"])
            require_assigned_branches(target_daily.branch_id)

            if target_daily.state not in ("draft", "opened"):
                raise AccessError(
                    self.env._(
                        "Stock lines cannot be moved to a locked stock sheet."
                    )
                )

        return super().write(vals)


    def unlink(self):
        require_role(self.env, STOCKKEEPER_GROUP)
        require_assigned_branches(self.branch_id)

        if any(record.daily_id.state not in ("draft", "opened") for record in self):
            raise AccessError(
                self.env._(
                    "Stock lines can only be deleted while the stock sheet is draft or opened."
                )
            )

        return super().unlink()

    @api.depends(
        "opening_qty",
        "received_qty",
        "incoming_transfer_qty",
        "outgoing_transfer_qty",
        "consumption_qty",
        "waste_qty",
        "damaged_qty",
    )
    def _compute_expected_closing(self):
        for line in self:
            line.expected_closing_qty = (
                line.opening_qty
                + line.received_qty
                + line.incoming_transfer_qty
                - line.outgoing_transfer_qty
                - line.consumption_qty
                - line.waste_qty
                - line.damaged_qty
            )

    @api.depends(
        "actual_closing_qty",
        "expected_closing_qty",
    )
    def _compute_difference(self):
        for line in self:
            line.difference_qty = (
                line.actual_closing_qty
                - line.expected_closing_qty
            )

    @api.constrains(
        "opening_qty",
        "received_qty",
        "incoming_transfer_qty",
        "outgoing_transfer_qty",
        "consumption_qty",
        "waste_qty",
        "damaged_qty",
        "actual_closing_qty",
    )
    def _check_non_negative_quantities(self):
        quantity_fields = (
            "opening_qty",
            "received_qty",
            "incoming_transfer_qty",
            "outgoing_transfer_qty",
            "consumption_qty",
            "waste_qty",
            "damaged_qty",
            "actual_closing_qty",
        )

        for line in self:
            if any(
                getattr(line, field_name) < 0
                for field_name in quantity_fields
            ):
                raise ValidationError(
                    self.env._(
                        "Stock quantities cannot be negative."
                    )
                )
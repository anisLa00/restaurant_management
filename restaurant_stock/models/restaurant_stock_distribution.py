from odoo import Command, api, fields, models
from odoo.exceptions import AccessError, UserError, ValidationError

from .stock_security import (
    CENTRAL_STOREKEEPER_GROUP,
    STOCKKEEPER_GROUP,
    lock_records,
    require_assigned_branches,
    require_role,
)


class RestaurantStockDistribution(models.Model):
    _name = "restaurant.stock.distribution"
    _description = "Restaurant Central Stock Distribution"
    _inherit = ["mail.thread", "mail.activity.mixin"]
    _order = "distribution_date desc, id desc"

    name = fields.Char(
        string="Reference",
        required=True,
        readonly=True,
        copy=False,
        default="New",
        tracking=True,
    )

    distribution_date = fields.Date(
        string="Date",
        required=True,
        default=fields.Date.context_today,
        tracking=True,
    )

    branch_id = fields.Many2one(
        "restaurant.branch",
        string="Branch",
        required=True,
        ondelete="restrict",
        tracking=True,
    )

    company_id = fields.Many2one(
        "res.company",
        related="branch_id.company_id",
        store=True,
        readonly=True,
    )

    state = fields.Selection(
        [
            ("draft", "Draft"),
            ("dispatched", "Dispatched"),
            ("received", "Received"),
            ("cancelled", "Cancelled"),
        ],
        default="draft",
        required=True,
        readonly=True,
        tracking=True,
    )

    line_ids = fields.One2many(
        "restaurant.stock.distribution.line",
        "distribution_id",
        string="Items",
    )

    dispatched_by_id = fields.Many2one(
        "res.users",
        string="Dispatched By",
        readonly=True,
        copy=False,
    )

    dispatched_at = fields.Datetime(
        string="Dispatched At",
        readonly=True,
        copy=False,
    )

    received_by_id = fields.Many2one(
        "res.users",
        string="Received By",
        readonly=True,
        copy=False,
    )

    received_at = fields.Datetime(
        string="Received At",
        readonly=True,
        copy=False,
    )

    dispatch_picking_id = fields.Many2one(
        "stock.picking",
        string="Dispatch Transfer",
        readonly=True,
        copy=False,
    )

    receipt_picking_id = fields.Many2one(
        "stock.picking",
        string="Branch Receipt",
        readonly=True,
        copy=False,
    )

    note = fields.Text()

    @api.model_create_multi
    def create(self, vals_list):
        require_role(
            self.env,
            CENTRAL_STOREKEEPER_GROUP,
        )

        prepared = []

        for vals in vals_list:
            vals = dict(vals)

            if vals.get("state", "draft") != "draft":
                raise AccessError(
                    self.env._(
                        "Central distributions must be created in draft."
                    )
                )

            vals["state"] = "draft"

            if vals.get("name", "New") == "New":
                vals["name"] = (
                    self.env["ir.sequence"].next_by_code(
                        "restaurant.stock.distribution"
                    )
                    or "New"
                )

            prepared.append(vals)

        records = super().create(prepared)

        for record in records:
            if record.company_id != self.env.company:
                raise UserError(
                    self.env._(
                        "Switch to the distribution company first."
                    )
                )

        return records

    def write(self, vals):
        lock_records(self)

        if "state" in vals:
            raise AccessError(
                self.env._(
                    "Distribution state can only be changed "
                    "by workflow actions."
                )
            )

        for record in self:
            if record.state == "draft":
                require_role(
                    self.env,
                    CENTRAL_STOREKEEPER_GROUP,
                )

                allowed = {
                    "distribution_date",
                    "branch_id",
                    "line_ids",
                    "note",
                }

                if set(vals) - allowed:
                    raise AccessError(
                        self.env._(
                            "These distribution fields cannot "
                            "be modified manually."
                        )
                    )

            elif record.state == "dispatched":
                require_role(
                    self.env,
                    STOCKKEEPER_GROUP,
                )

                require_assigned_branches(
                    record.branch_id
                )

                allowed = {
                    "line_ids",
                    "note",
                }

                if set(vals) - allowed:
                    raise AccessError(
                        self.env._(
                            "Only receipt quantities may be "
                            "updated after dispatch."
                        )
                    )

            else:
                raise AccessError(
                    self.env._(
                        "This distribution can no longer be edited."
                    )
                )

        return super().write(vals)

    def unlink(self):
        lock_records(
            self,
            "unlink",
        )

        require_role(
            self.env,
            CENTRAL_STOREKEEPER_GROUP,
        )

        if any(
            record.state != "draft"
            for record in self
        ):
            raise AccessError(
                self.env._(
                    "Only draft distributions can be deleted."
                )
            )

        return super().unlink()

    def action_dispatch(self):
        self.ensure_one()

        lock_records(self)

        require_role(
            self.env,
            CENTRAL_STOREKEEPER_GROUP,
        )

        if self.state != "draft":
            raise UserError(
                self.env._(
                    "Only draft distributions can be dispatched."
                )
            )

        if self.dispatch_picking_id:
            raise UserError(
                self.env._(
                    "This distribution has already been dispatched."
                )
            )

        company = self.env.company

        if self.company_id != company:
            raise UserError(
                self.env._(
                    "Switch to the distribution company "
                    "before dispatching."
                )
            )

        central_warehouse = self.env.ref(
            "stock.warehouse0",
            raise_if_not_found=False,
        )

        if not central_warehouse:
            raise UserError(
                self.env._(
                    "Central Warehouse is not configured."
                )
            )

        if central_warehouse.company_id != company:
            raise UserError(
                self.env._(
                    "Central Warehouse belongs to another company."
                )
            )

        transit_location = (
            company.internal_transit_location_id
        )

        if not transit_location:
            raise UserError(
                self.env._(
                    "The inter-warehouse transit location "
                    "is not configured."
                )
            )

        lines = self.line_ids.filtered(
            lambda line: not line.uom_id.is_zero(
                line.sent_qty
            )
        )

        if not lines:
            raise ValidationError(
                self.env._(
                    "Enter a sent quantity for at least one item."
                )
            )

        picking = (
            self.env["stock.picking"]
            .sudo()
            .with_company(company)
            .create({
                "picking_type_id":
                    central_warehouse.int_type_id.id,

                "location_id":
                    central_warehouse.lot_stock_id.id,

                "location_dest_id":
                    transit_location.id,

                "origin":
                    self.name,

                "company_id":
                    company.id,

                "move_ids": [
                    Command.create({
                        "product_id":
                            line.product_id.id,

                        "product_uom_qty":
                            line.sent_qty,

                        "uom_id":
                            line.uom_id.id,

                        "location_id":
                            central_warehouse.lot_stock_id.id,

                        "location_dest_id":
                            transit_location.id,

                        "company_id":
                            company.id,
                    })
                    for line in lines
                ],
            })
        )

        picking.action_confirm()
        picking.action_assign()

        active_moves = picking.move_ids.filtered(
            lambda move:
                move.state not in ("cancel", "done")
        )

        if any(
            move.uom_id.compare(
                move.quantity,
                move.product_uom_qty,
            ) < 0
            for move in active_moves
        ):
            raise UserError(
                self.env._(
                    "Central Warehouse does not have enough "
                    "available stock for the complete distribution."
                )
            )

        result = (
            picking
            .with_context(skip_backorder=True)
            .button_validate()
        )

        if (
            result is not True
            or picking.state != "done"
        ):
            raise UserError(
                self.env._(
                    "The Central dispatch could not "
                    "be validated automatically."
                )
            )

        super().write({
            "dispatch_picking_id":
                picking.id,

            "dispatched_by_id":
                self.env.uid,

            "dispatched_at":
                fields.Datetime.now(),

            "state":
                "dispatched",
        })

        stockkeepers = self.branch_id.sudo().user_ids.filtered(
            lambda user: user.active and user.has_group(STOCKKEEPER_GROUP)
        )
        if stockkeepers:
            message = self.env._(
                "Delivery %s is ready for receipt confirmation.",
                self.name,
            )
            self.message_post(
                body=message,
                partner_ids=stockkeepers.partner_id.ids,
                message_type="notification",
            )
            for stockkeeper in stockkeepers:
                self.activity_schedule(
                    "mail.mail_activity_data_todo",
                    user_id=stockkeeper.id,
                    summary=self.env._("Incoming stock delivery"),
                    note=message,
                )

        return True

    def action_confirm_receipt(self):
        self.ensure_one()

        lock_records(self)

        require_role(
            self.env,
            STOCKKEEPER_GROUP,
        )

        require_assigned_branches(
            self.branch_id
        )

        if self.state != "dispatched":
            raise UserError(
                self.env._(
                    "Only dispatched distributions can be received."
                )
            )

        if self.receipt_picking_id:
            raise UserError(
                self.env._(
                    "This distribution has already been received."
                )
            )

        if (
            not self.dispatch_picking_id
            or self.dispatch_picking_id.sudo().state != "done"
        ):
            raise UserError(
                self.env._(
                    "The Central dispatch transfer has not "
                    "been completed."
                )
            )

        company = self.env.company

        if self.company_id != company:
            raise UserError(
                self.env._(
                    "Switch to the distribution company "
                    "before receiving it."
                )
            )

        transit_location = (
            company.internal_transit_location_id
        )

        if not transit_location:
            raise UserError(
                self.env._(
                    "The inter-warehouse transit location "
                    "is not configured."
                )
            )

        branch_warehouse = (
            self.branch_id
            .sudo()
            .warehouse_id
        )

        if (
            not branch_warehouse
            or branch_warehouse.company_id != company
        ):
            raise UserError(
                self.env._(
                    "A warehouse for this company is not "
                    "configured on the distribution branch."
                )
            )

        receipt_lines = self.line_ids.filtered(
            lambda line:
                not line.uom_id.is_zero(
                    line.received_qty
                )
        )

        if not receipt_lines:
            raise ValidationError(
                self.env._(
                    "Enter a received quantity for "
                    "at least one item."
                )
            )

        for line in receipt_lines:
            if line.uom_id.compare(
                line.received_qty,
                line.sent_qty,
            ) > 0:
                raise ValidationError(
                    self.env._(
                        "Received quantity cannot exceed sent quantity for %s."
                    )
                    % line.product_id.display_name
                )

        shortage_lines = self.line_ids.filtered(
            lambda line: line.uom_id.compare(
                line.received_qty,
                line.sent_qty,
            ) < 0
        )

        for line in shortage_lines:
            if not line.shortage_reason:
                raise ValidationError(
                    self.env._(
                        "Select a shortage reason for %s."
                    )
                    % line.product_id.display_name
                )

            if (
                line.shortage_reason == "other"
                and not line.shortage_note
            ):
                raise ValidationError(
                    self.env._(
                        "Enter a shortage note for %s."
                    )
                    % line.product_id.display_name
                )
            

        picking = (
            self.env["stock.picking"]
            .sudo()
            .with_company(company)
            .create({
                "picking_type_id":
                    branch_warehouse.int_type_id.id,

                "location_id":
                    transit_location.id,

                "location_dest_id":
                    branch_warehouse.lot_stock_id.id,

                "origin":
                    self.name,

                "company_id":
                    company.id,

                "move_ids": [
                    Command.create({
                        "product_id":
                            line.product_id.id,

                        "product_uom_qty":
                            line.received_qty,

                        "uom_id":
                            line.uom_id.id,

                        "location_id":
                            transit_location.id,

                        "location_dest_id":
                            branch_warehouse.lot_stock_id.id,

                        "company_id":
                            company.id,
                    })
                    for line in receipt_lines
                ],
            })
        )

        picking.action_confirm()

        for move in picking.move_ids:
            move.quantity = (
                move.product_uom_qty
            )

        result = picking.button_validate()

        if (
            result is not True
            or picking.state != "done"
        ):
            raise UserError(
                self.env._(
                    "The branch receipt could not "
                    "be validated automatically."
                )
            )

        super().write({
            "receipt_picking_id":
                picking.id,

            "received_by_id":
                self.env.uid,

            "received_at":
                fields.Datetime.now(),

            "state":
                "received",
        })

        self.env[
            "restaurant.stock.daily"
        ]._sync_completed_branch_receipt(
            picking,
            self.branch_id,
            self,
        )

        if shortage_lines:
            reason_labels = dict(
                self.env[
                    "restaurant.stock.distribution.line"
                ]._fields["shortage_reason"].selection
            )

            shortage_details = []

            for line in shortage_lines:
                shortage_qty = (
                    line.sent_qty
                    - line.received_qty
                )

                reason = reason_labels.get(
                    line.shortage_reason,
                    line.shortage_reason,
                )

                detail = (
                    f"{line.product_id.display_name}: "
                    f"Sent {line.sent_qty:g}, "
                    f"Received {line.received_qty:g}, "
                    f"Missing {shortage_qty:g}, "
                    f"Reason: {reason}"
                )

                if line.shortage_note:
                    detail += (
                        f" ({line.shortage_note})"
                    )

                shortage_details.append(detail)

            message = (
                "<b>Branch receipt shortage reported.</b><br/>"
                + "<br/>".join(shortage_details)
            )

            partner_ids = []

            if self.dispatched_by_id.partner_id:
                partner_ids.append(
                    self.dispatched_by_id.partner_id.id
                )

            self.message_post(
                body=message,
                partner_ids=partner_ids,
                message_type="notification",
            )

            if self.dispatched_by_id:
                self.activity_schedule(
                    "mail.mail_activity_data_todo",
                    user_id=self.dispatched_by_id.id,
                    summary="Distribution shortage reported",
                    note=message,
                )

        return True

    def action_cancel(self):
        self.ensure_one()

        lock_records(self)

        require_role(
            self.env,
            CENTRAL_STOREKEEPER_GROUP,
        )

        if self.state != "draft":
            raise UserError(
                self.env._(
                    "Only draft distributions can be cancelled."
                )
            )

        super().write({
            "state": "cancelled",
        })

        return True


class RestaurantStockDistributionLine(models.Model):
    _name = "restaurant.stock.distribution.line"
    _description = "Restaurant Central Distribution Line"
    _order = "product_id"

    distribution_id = fields.Many2one(
        "restaurant.stock.distribution",
        required=True,
        ondelete="cascade",
        index=True,
    )

    branch_id = fields.Many2one(
        related="distribution_id.branch_id",
        store=True,
        readonly=True,
    )

    company_id = fields.Many2one(
        related="distribution_id.company_id",
        store=True,
        readonly=True,
    )

    product_id = fields.Many2one(
        "product.product",
        string="Product",
        required=True,
        ondelete="restrict",
    )

    uom_id = fields.Many2one(
        "uom.uom",
        string="UoM",
        required=True,
    )

    sent_qty = fields.Float(
        string="Sent Qty",
        required=True,
        default=0.0,
    )

    received_qty = fields.Float(
        string="Received Qty",
        default=0.0,
    )

    difference_qty = fields.Float(
        string="Difference",
        compute="_compute_difference_qty",
    )

    shortage_reason = fields.Selection(
    [
        ("missing", "Missing in Transfer"),
        ("damaged", "Damaged"),
        ("counting_error", "Counting Error"),
        ("not_delivered", "Not Delivered"),
        ("other", "Other"),
    ],
        string="Shortage Reason",
)

    shortage_note = fields.Char(
        string="Shortage Note",
)

    @api.onchange("product_id")
    def _onchange_product_id(self):
        if self.product_id:
            self.uom_id = (
                self.product_id.uom_id
            )

    @api.model_create_multi
    def create(self, vals_list):
        require_role(
            self.env,
            CENTRAL_STOREKEEPER_GROUP,
        )

        prepared = []

        for vals in vals_list:
            vals = dict(vals)

            # Always derive the UoM from the selected product.
            # This prevents readonly UI fields from producing
            # database rows without uom_id.
            if vals.get("product_id"):
                product = (
                    self.env["product.product"]
                    .browse(vals["product_id"])
                )

                vals["uom_id"] = (
                    product.uom_id.id
                )

            prepared.append(vals)

        distribution_ids = {
            vals.get("distribution_id")
            for vals in prepared
            if vals.get("distribution_id")
        }

        distributions = self.env[
            "restaurant.stock.distribution"
        ].browse(distribution_ids)

        if any(
            distribution.state != "draft"
            for distribution in distributions
        ):
            raise AccessError(
                self.env._(
                    "Distribution items can only be added "
                    "while the distribution is draft."
                )
            )

        return super().create(
            prepared
        )

    def write(self, vals):
        vals = dict(vals)

        for line in self:
            distribution = (
                line.distribution_id
            )

            if distribution.state == "draft":
                require_role(
                    self.env,
                    CENTRAL_STOREKEEPER_GROUP,
                )

                allowed = {
                    "product_id",
                    "uom_id",
                    "sent_qty",
                }

            elif distribution.state == "dispatched":
                require_role(
                    self.env,
                    STOCKKEEPER_GROUP,
                )

                require_assigned_branches(
                    distribution.branch_id
                )

                allowed = {
                        "received_qty",
                        "shortage_reason",
                        "shortage_note",
                    }

            else:
                raise AccessError(
                    self.env._(
                        "Distribution items can no longer "
                        "be edited."
                    )
                )

            if set(vals) - allowed:
                raise AccessError(
                    self.env._(
                        "You cannot modify these "
                        "distribution item fields."
                    )
                )

        # If Central changes the product,
        # automatically update its UoM too.
        if vals.get("product_id"):
            product = (
                self.env["product.product"]
                .browse(vals["product_id"])
            )

            vals["uom_id"] = (
                product.uom_id.id
            )

        return super().write(
            vals
        )

    def unlink(self):
        for line in self:
            require_role(
                self.env,
                CENTRAL_STOREKEEPER_GROUP,
            )

            if (
                line.distribution_id.state
                != "draft"
            ):
                raise AccessError(
                    self.env._(
                        "Distribution items can only "
                        "be deleted while draft."
                    )
                )

        return super().unlink()

    @api.constrains("product_id")
    def _check_product(self):
        for line in self:
            if (
                line.product_id
                and not line.product_id.is_storable
            ):
                raise ValidationError(
                    self.env._(
                        "Distribution items must "
                        "be storable products."
                    )
                )

    _product_unique = models.Constraint(
        "UNIQUE(distribution_id, product_id)",
        "The same product cannot appear twice "
        "in one distribution.",
    )

    @api.depends(
        "sent_qty",
        "received_qty",
    )
    def _compute_difference_qty(self):
        for line in self:
            line.difference_qty = (
                line.received_qty
                - line.sent_qty
            )

    @api.constrains(
        "sent_qty",
        "received_qty",
    )
    def _check_quantities(self):
        for line in self:
            if (
                line.sent_qty < 0
                or line.received_qty < 0
            ):
                raise ValidationError(
                    self.env._(
                        "Distribution quantities "
                        "cannot be negative."
                    )
                )

            if (
                line.received_qty
                > line.sent_qty
            ):
                raise ValidationError(
                    self.env._(
                        "Received quantity cannot "
                        "exceed sent quantity."
                    )
                )

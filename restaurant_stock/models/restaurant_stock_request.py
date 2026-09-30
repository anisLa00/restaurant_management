from odoo import Command, api, fields, models
from odoo.exceptions import AccessError, UserError, ValidationError

from .stock_security import (
    CENTRAL_STOREKEEPER_GROUP,
    OPERATIONS_GROUP,
    OWNER_GROUP,
    PURCHASING_GROUP,
    STOCKKEEPER_GROUP,
    lock_records,
    require_assigned_branches,
    require_role,
)


class RestaurantStockRequest(models.Model):
    _name = "restaurant.stock.request"
    _description = "Restaurant Stock Request"
    _inherit = ["mail.thread", "mail.activity.mixin"]
    _order = "request_date desc, id desc"
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

    request_date = fields.Date(
        required=True,
        default=fields.Date.context_today,
        index=True,
        tracking=True,
    )

    requested_by_id = fields.Many2one(
        "res.users",
        required=True,
        readonly=True,
        default=lambda self: self.env.user,
        tracking=True,
    )

    central_storekeeper_id = fields.Many2one(
        "res.users",
        string="Central Storekeeper",
        readonly=True,
        copy=False,
        tracking=True,
    )

    purchasing_officer_id = fields.Many2one(
        "res.users",
        string="Purchasing Officer",
        readonly=True,
        copy=False,
        tracking=True,
    )

    dispatched_by_id = fields.Many2one(
        "res.users",
        readonly=True,
        copy=False,
        tracking=True,
    )

    branch_received_by_id = fields.Many2one(
        "res.users",
        readonly=True,
        copy=False,
        tracking=True,
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

    vendor_id = fields.Many2one(
        "res.partner",
        string="Vendor",
        copy=False,
        tracking=True,
        domain=[("supplier_rank", ">", 0)],
    )

    purchase_order_id = fields.Many2one(
        "purchase.order",
        string="Purchase Order",
        readonly=True,
        copy=False,
        check_company=True,
    )

    purchase_receipt_ids = fields.Many2many(
        related="purchase_order_id.picking_ids",
        string="Supplier Receipts",
        readonly=True,
    )

    state = fields.Selection(
        [
            ("draft", "Draft"),
            ("submitted", "Submitted to Central Store"),
            ("transfer_ready", "Ready for Transfer"),
            ("purchase_required", "Purchase Required"),
            ("purchasing", "Purchasing"),
            ("central_received", "Received at Central Store"),
            ("dispatched", "Dispatched to Branch"),
            ("received", "Received by Branch"),
            ("rejected", "Rejected"),
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
        "restaurant.stock.request.line",
        "request_id",
        string="Requested Items",
        copy=True,
    )

    branch_note = fields.Text()
    central_note = fields.Text()
    purchasing_note = fields.Text()
    receipt_note = fields.Text()

    company_id = fields.Many2one(
        "res.company",
        related="branch_id.company_id",
        store=True,
        readonly=True,
        index=True,
    )

    @api.model_create_multi
    def create(self, vals_list):
        require_role(self.env, STOCKKEEPER_GROUP)

        prepared = []

        for values in vals_list:
            values = dict(values)

            if values.get("state", "draft") != "draft":
                raise AccessError(
                    self.env._("Stock requests must be created in draft.")
                )

            protected = {
                "requested_by_id",
                "central_storekeeper_id",
                "purchasing_officer_id",
                "dispatched_by_id",
                "branch_received_by_id",
                "company_id",
            }

            if set(values) & protected:
                raise AccessError(
                    self.env._(
                        "System and workflow fields cannot be supplied manually."
                    )
                )

            values.update({
                "name": self.env["ir.sequence"].next_by_code(
                    "restaurant.stock.request"
                ) or "New",
                "requested_by_id": self.env.uid,
                "state": "draft",
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
                        "Save changes before changing the workflow state."
                    )
                )

            target = vals["state"]

            for request in self:
                request._check_transition(target)

            values = dict(vals)

            if target in (
                "transfer_ready",
                "purchase_required",
                "rejected",
            ):
                values["central_storekeeper_id"] = self.env.uid

            if target == "purchasing":
                values["purchasing_officer_id"] = self.env.uid

            if target == "dispatched":
                values["dispatched_by_id"] = self.env.uid

            if target == "received":
                values["branch_received_by_id"] = self.env.uid

            result = super().write(values)

            for request in self:
                label = dict(self._fields["state"].selection)[target]
                request.message_post(
                    body=self.env._(
                        "Stock request changed to %s by %s.",
                        label,
                        self.env.user.name,
                    )
                )

            return result

        editable_by_stockkeeper = {
            "branch_id",
            "request_date",
            "line_ids",
            "branch_note",
        }

        editable_by_central = {
            "central_note",
            "line_ids",
        }

        editable_by_purchasing = {
            "purchasing_note",
            "vendor_id",
        }

        if self.env.user.has_group(STOCKKEEPER_GROUP):
            require_assigned_branches(self.branch_id)

            if all(request.state == "draft" for request in self):
                if set(vals) - editable_by_stockkeeper:
                    raise AccessError(
                        self.env._(
                            "You cannot modify these stock request fields."
                        )
                    )

                if "branch_id" in vals:
                    require_assigned_branches(
                        self.env["restaurant.branch"].browse(vals["branch_id"])
                    )

                return super().write(vals)

            if all(request.state == "dispatched" for request in self):
                allowed_receipt_fields = {
                    "line_ids",
                    "receipt_note",
                }

                if set(vals) - allowed_receipt_fields:
                    raise AccessError(
                        self.env._(
                            "At delivery, you can only record received quantities and receipt notes."
                        )
                    )

                return super().write(vals)

            raise AccessError(
                self.env._(
                    "The Branch Stockkeeper cannot edit this request in its current state."
                )
            )

            

        if self.env.user.has_group(CENTRAL_STOREKEEPER_GROUP):
            if any(
                request.state not in (
                    "submitted",
                    "transfer_ready",
                    "purchase_required",
                    "central_received",
                    "purchasing",
                )
                for request in self
            ):
                raise AccessError(
                    self.env._(
                        "This request cannot be edited by the Central Storekeeper in its current state."
                    )
                )

            if set(vals) - editable_by_central:
                raise AccessError(
                    self.env._(
                        "You cannot modify these stock request fields."
                    )
                )

            return super().write(vals)

        if self.env.user.has_group(PURCHASING_GROUP):
            if any(
                request.state not in ("purchase_required", "purchasing")
                for request in self
            ):
                raise AccessError(
                    self.env._(
                        "Purchasing can only edit requests that require purchasing."
                    )
                )

            if set(vals) - editable_by_purchasing:
                raise AccessError(
                    self.env._(
                        "You cannot modify these stock request fields."
                    )
                )

            return super().write(vals)

        raise AccessError(
            self.env._("Your role does not allow editing stock requests.")
        )

    def unlink(self):
        lock_records(self, "unlink")
        require_role(self.env, STOCKKEEPER_GROUP)
        require_assigned_branches(self.branch_id)

        if any(request.state != "draft" for request in self):
            raise AccessError(
                self.env._("Only draft stock requests can be deleted.")
            )

        return super().unlink()

    def _check_transition(self, target):
        self.ensure_one()

        transitions = {
            ("draft", "submitted"): STOCKKEEPER_GROUP,
            ("draft", "cancelled"): STOCKKEEPER_GROUP,

            ("submitted", "transfer_ready"): CENTRAL_STOREKEEPER_GROUP,
            ("submitted", "purchase_required"): CENTRAL_STOREKEEPER_GROUP,
            ("submitted", "rejected"): CENTRAL_STOREKEEPER_GROUP,

            ("purchase_required", "purchasing"): PURCHASING_GROUP,

            ("purchasing", "central_received"): CENTRAL_STOREKEEPER_GROUP,

            ("transfer_ready", "dispatched"): CENTRAL_STOREKEEPER_GROUP,
            ("central_received", "dispatched"): CENTRAL_STOREKEEPER_GROUP,

            ("dispatched", "received"): STOCKKEEPER_GROUP,
        }

        group = transitions.get((self.state, target))

        if not group:
            raise UserError(
                self.env._(
                    "This stock request workflow transition is not allowed."
                )
            )

        require_role(self.env, group)

        if group == STOCKKEEPER_GROUP:
            require_assigned_branches(self.branch_id)

    def action_submit(self):
        self.ensure_one()

        if not self.line_ids:
            raise ValidationError(
                self.env._("Add at least one item before submitting.")
            )

        return self.write({"state": "submitted"})

    def action_prepare_transfer(self):
        self.ensure_one()
        return self.write({"state": "transfer_ready"})

    def action_require_purchase(self):
        self.ensure_one()

        if not any(
            not line.uom_id.is_zero(line.purchase_qty)
            for line in self.line_ids
        ):
            raise ValidationError(
                self.env._("Enter a quantity to purchase for at least one item.")
            )

        return self.write({"state": "purchase_required"})

    def action_reject(self):
        self.ensure_one()
        return self.write({"state": "rejected"})

    def action_start_purchasing(self):
        self.ensure_one()

        lock_records(self)
        self.invalidate_recordset(["state", "purchase_order_id", "vendor_id"])

        if self.purchase_order_id:
            raise UserError(
                self.env._("A purchase order already exists for this stock request.")
            )

        self._check_transition("purchasing")

        company = self.env.company

        if self.company_id != company:
            raise UserError(
                self.env._(
                    "Switch to the stock request company before creating its purchase order."
                )
            )

        if not self.vendor_id:
            raise ValidationError(
                self.env._("Select a vendor before creating the purchase order.")
            )

        central_warehouse = self.env["stock.warehouse"].search([
            ("company_id", "=", company.id),
            ("code", "=", "WH"),
        ], limit=1)

        if not central_warehouse:
            raise UserError(
                self.env._(
                    "The Central Warehouse (WH) is not configured for this company."
                )
            )

        purchase_lines = self.line_ids.filtered(
            lambda line: not line.uom_id.is_zero(line.purchase_qty)
        )

        if not purchase_lines:
            raise ValidationError(
                self.env._("Enter a quantity to purchase for at least one item.")
            )

        purchase_order = self.env["purchase.order"].with_company(company).create({
            "partner_id": self.vendor_id.id,
            "company_id": company.id,
            "user_id": self.env.uid,
            "origin": self.name,
            "picking_type_id": central_warehouse.in_type_id.id,
            "order_line": [
                Command.create({
                    "product_id": line.product_id.id,
                    "product_qty": line.purchase_qty,
                    "uom_id": line.uom_id.id,
                })
                for line in purchase_lines
            ],
        })

        super().write({"purchase_order_id": purchase_order.id})
        self.write({"state": "purchasing"})

        return {
            "type": "ir.actions.act_window",
            "res_model": "purchase.order",
            "res_id": purchase_order.id,
            "view_mode": "form",
        }

    def action_receive_central(self):
        self.ensure_one()

        lock_records(self)
        self._check_transition("central_received")

        company = self.env.company

        if self.company_id != company:
            raise UserError(
                self.env._(
                    "Switch to the stock request company before verifying its receipt."
                )
            )

        purchase_order = self.purchase_order_id.sudo().with_company(company)

        if not purchase_order:
            raise UserError(
                self.env._("No purchase order is linked to this stock request.")
            )

        if purchase_order.state != "purchase":
            raise UserError(
                self.env._("Confirm the related purchase order before verifying receipts.")
            )

        central_warehouse = self.env["stock.warehouse"].sudo().search([
            ("company_id", "=", company.id),
            ("code", "=", "WH"),
        ], limit=1)

        done_receipts = purchase_order.picking_ids.filtered(
            lambda picking: (
                picking.state == "done"
                and picking.picking_type_code == "incoming"
                and picking.location_dest_id == central_warehouse.lot_stock_id
            )
        )

        if not done_receipts:
            raise UserError(
                self.env._(
                    "Validate at least one supplier receipt into Central Warehouse (WH/Stock) first."
                )
            )

        all_received = True

        for request_line in self.line_ids:
            purchase_order_lines = purchase_order.order_line.filtered(
                lambda po_line: (
                    not po_line.display_type
                    and po_line.product_id == request_line.product_id
                )
            )
            received_qty = sum(
                po_line.uom_id._compute_quantity(
                    po_line.qty_received,
                    request_line.uom_id,
                )
                for po_line in purchase_order_lines
            )
            request_line._record_central_received_qty(received_qty)

            if request_line.uom_id.compare(
                received_qty,
                request_line.purchase_qty,
            ) < 0:
                all_received = False

        if not all_received:
            self.message_post(
                body=self.env._(
                    "The completed supplier receipts were recorded. The request remains in Purchasing until all purchase quantities are received."
                )
            )
            return {
                "type": "ir.actions.client",
                "tag": "display_notification",
                "params": {
                    "title": self.env._("Partial supplier receipt recorded"),
                    "message": self.env._(
                        "The remaining quantities stay on the native Odoo backorder."
                    ),
                    "type": "warning",
                    "sticky": False,
                },
            }

        return self.write({"state": "central_received"})

    def action_dispatch(self):
        self.ensure_one()

        lock_records(self)
        self._check_transition("dispatched")

        if self.dispatch_picking_id:
            raise UserError(
                self.env._("This stock request has already been dispatched.")
            )

        company = self.env.company

        if self.company_id != company:
            raise UserError(
                self.env._(
                    "Switch to the stock request company before dispatching it."
                )
            )

        central_warehouse = self.env["stock.warehouse"].sudo().search([
            ("company_id", "=", company.id),
            ("code", "=", "WH"),
        ], limit=1)

        if not central_warehouse:
            raise UserError(
                self.env._(
                    "The Central Warehouse (WH) is not configured for this company."
                )
            )

        transit_location = company.internal_transit_location_id

        if not transit_location:
            raise UserError(
                self.env._(
                    "The inter-warehouse transit location is not configured for this company."
                )
            )

        dispatch_lines = self.line_ids.filtered(
            lambda line: not line.uom_id.is_zero(line.dispatched_qty)
        )

        if not dispatch_lines:
            raise ValidationError(
                self.env._("Enter a dispatched quantity for at least one item.")
            )

        picking = self.env["stock.picking"].sudo().with_company(company).create({
            "picking_type_id": central_warehouse.int_type_id.id,
            "location_id": central_warehouse.lot_stock_id.id,
            "location_dest_id": transit_location.id,
            "origin": self.name,
            "company_id": company.id,
            "move_ids": [
                Command.create({
                    "product_id": line.product_id.id,
                    "product_uom_qty": line.dispatched_qty,
                    "uom_id": line.uom_id.id,
                    "location_id": central_warehouse.lot_stock_id.id,
                    "location_dest_id": transit_location.id,
                    "company_id": company.id,
                })
                for line in dispatch_lines
            ],
        })

        validation_result = picking.button_validate()

        if validation_result is not True or picking.state != "done":
            raise UserError(
                self.env._(
                    "The dispatch transfer could not be validated automatically."
                )
            )

        super().write({"dispatch_picking_id": picking.id})

        return self.write({"state": "dispatched"})

    def action_confirm_branch_receipt(self):
        self.ensure_one()

        lock_records(self)

        if self.receipt_picking_id:
            raise UserError(
                self.env._("This stock request has already been received.")
            )

        self._check_transition("received")

        company = self.env.company

        if self.company_id != company:
            raise UserError(
                self.env._(
                    "Switch to the stock request company before receiving it."
                )
            )

        transit_location = company.internal_transit_location_id

        if not transit_location:
            raise UserError(
                self.env._(
                    "The inter-warehouse transit location is not configured for this company."
                )
            )

        # The warehouse mapping is an administrator-only configuration field.
        # Branch access was already checked by lock_records/_check_transition.
        branch_warehouse = self.branch_id.sudo().warehouse_id

        if not branch_warehouse or branch_warehouse.company_id != company:
            raise UserError(
                self.env._(
                    "A warehouse for this company is not configured on the request branch."
                )
            )

        receipt_lines = self.line_ids.filtered(
            lambda line: not line.uom_id.is_zero(line.branch_received_qty)
        )

        if not receipt_lines:
            raise ValidationError(
                self.env._("Enter a received quantity for at least one item.")
            )

        picking = self.env["stock.picking"].sudo().with_company(company).create({
            "picking_type_id": branch_warehouse.int_type_id.id,
            "location_id": transit_location.id,
            "location_dest_id": branch_warehouse.lot_stock_id.id,
            "origin": self.name,
            "company_id": company.id,
            "move_ids": [
                Command.create({
                    "product_id": line.product_id.id,
                    "product_uom_qty": line.branch_received_qty,
                    "uom_id": line.uom_id.id,
                    "location_id": transit_location.id,
                    "location_dest_id": branch_warehouse.lot_stock_id.id,
                    "company_id": company.id,
                })
                for line in receipt_lines
            ],
        })

        picking.action_confirm()

        for move in picking.move_ids:
            move.quantity = move.product_uom_qty

        validation_result = picking.button_validate()

        if validation_result is not True or picking.state != "done":
            raise UserError(
                self.env._(
                    "The branch receipt transfer could not be validated automatically."
                )
            )

        super().write({"receipt_picking_id": picking.id})

        return self.write({"state": "received"})

    def action_cancel(self):
        self.ensure_one()
        return self.write({"state": "cancelled"})


class RestaurantStockRequestLine(models.Model):
    _name = "restaurant.stock.request.line"
    _description = "Restaurant Stock Request Line"
    _order = "product_id"

    request_id = fields.Many2one(
        "restaurant.stock.request",
        required=True,
        ondelete="cascade",
        index=True,
    )

    branch_id = fields.Many2one(
        related="request_id.branch_id",
        store=True,
        readonly=True,
    )

    company_id = fields.Many2one(
        related="request_id.company_id",
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

    requested_qty = fields.Float(
        string="Requested Qty",
        required=True,
        default=1.0,
    )

    transfer_qty = fields.Float(
        string="From Central Stock",
        default=0.0,
    )

    purchase_qty = fields.Float(
        string="To Purchase",
        default=0.0,
    )

    central_received_qty = fields.Float(
        string="Received at Central",
        default=0.0,
    )

    dispatched_qty = fields.Float(
        string="Dispatched Qty",
        default=0.0,
    )

    branch_received_qty = fields.Float(
        string="Branch Received Qty",
        default=0.0,
    )

    difference_qty = fields.Float(
        string="Receipt Difference",
        compute="_compute_difference",
        store=True,
    )

    note = fields.Char()

    _request_product_unique = models.Constraint(
        "UNIQUE(request_id, product_id)",
        "A product can only appear once in the same stock request.",
    )
    @api.model_create_multi
    def create(self, vals_list):
        require_role(self.env, STOCKKEEPER_GROUP)

        request_ids = [
            vals.get("request_id")
            for vals in vals_list
            if vals.get("request_id")
        ]
        requests = self.env["restaurant.stock.request"].browse(request_ids)

        require_assigned_branches(requests.branch_id)

        if any(request.state != "draft" for request in requests):
            raise AccessError(
                self.env._(
                    "Items can only be added while the stock request is draft."
                )
            )

        allowed = {
            "request_id",
            "product_id",
            "requested_qty",
            "note",
        }

        for vals in vals_list:
            if set(vals) - allowed:
                raise AccessError(
                    self.env._(
                        "Branch Stockkeepers cannot set workflow quantities manually."
                    )
                )

        return super().create(vals_list)


    def write(self, vals):
        requests = self.mapped("request_id")
        requests.lock_for_update()
        requests.invalidate_recordset(["state"])

        # Branch Stockkeeper
        if self.env.user.has_group(STOCKKEEPER_GROUP):
            require_assigned_branches(self.branch_id)

            # Preparing request
            if all(request.state == "draft" for request in requests):
                allowed = {
                    "product_id",
                    "requested_qty",
                    "note",
                }

                if set(vals) - allowed:
                    raise AccessError(
                        self.env._(
                            "You can only edit requested items and quantities in draft."
                        )
                    )

                return super().write(vals)

            # Confirming what actually arrived at the branch
            if all(request.state == "dispatched" for request in requests):
                allowed = {
                    "branch_received_qty",
                    "note",
                }

                if set(vals) - allowed:
                    raise AccessError(
                        self.env._(
                            "At delivery, you can only record the received quantity."
                        )
                    )

                return super().write(vals)

            raise AccessError(
                self.env._(
                    "The Branch Stockkeeper cannot edit request lines in this state."
                )
            )

        # Central Storekeeper
        if self.env.user.has_group(CENTRAL_STOREKEEPER_GROUP):
            allowed = {
                "transfer_qty",
                "purchase_qty",
                "dispatched_qty",
                "note",
            }

            if set(vals) - allowed:
                raise AccessError(
                    self.env._(
                        "The Central Storekeeper cannot modify these fields."
                    )
                )

            allowed_states = {
                "submitted",
                "transfer_ready",
                "purchase_required",
                "purchasing",
                "central_received",
            }

            if any(request.state not in allowed_states for request in requests):
                raise AccessError(
                    self.env._(
                        "The Central Storekeeper cannot edit quantities in this state."
                    )
                )

            return super().write(vals)

        raise AccessError(
            self.env._(
                "Your role does not allow editing stock request lines."
            )
        )

    def _record_central_received_qty(self, quantity):
        require_role(self.env, CENTRAL_STOREKEEPER_GROUP)

        if any(line.request_id.state != "purchasing" for line in self):
            raise UserError(
                self.env._(
                    "Central receipt quantities can only be recorded while purchasing."
                )
            )

        return super().write({"central_received_qty": quantity})


    def unlink(self):
        require_role(self.env, STOCKKEEPER_GROUP)
        require_assigned_branches(self.branch_id)

        if any(line.request_id.state != "draft" for line in self):
            raise AccessError(
                self.env._(
                    "Request items can only be deleted while the request is draft."
                )
            )

        return super().unlink()
    @api.depends("dispatched_qty", "branch_received_qty")
    def _compute_difference(self):
        for line in self:
            line.difference_qty = (
                line.branch_received_qty - line.dispatched_qty
            )

    @api.constrains(
        "requested_qty",
        "transfer_qty",
        "purchase_qty",
        "central_received_qty",
        "dispatched_qty",
        "branch_received_qty",
    )
    def _check_quantities(self):
        for line in self:
            quantities = (
                line.requested_qty,
                line.transfer_qty,
                line.purchase_qty,
                line.central_received_qty,
                line.dispatched_qty,
                line.branch_received_qty,
            )

            if any(quantity < 0 for quantity in quantities):
                raise ValidationError(
                    self.env._("Stock request quantities cannot be negative.")
                )

            if line.transfer_qty + line.purchase_qty > line.requested_qty:
                raise ValidationError(
                    self.env._(
                        "Transfer quantity plus purchase quantity cannot exceed the requested quantity."
                    )
                )

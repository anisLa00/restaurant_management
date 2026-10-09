from odoo import Command, api, fields, models
from odoo.exceptions import AccessError, UserError, ValidationError

from .stock_security import (
    CENTRAL_STOREKEEPER_GROUP,
    MANAGER_GROUP,
    OPERATIONS_GROUP,
    OWNER_GROUP,
    PURCHASING_GROUP,
    STOCKKEEPER_GROUP,
    lock_records,
    require_assigned_branches,
    require_role,
)


def require_branch_request_role(env):
    if not env.su and not (
        env.user.has_group(STOCKKEEPER_GROUP)
        or env.user.has_group(MANAGER_GROUP)
    ):
        raise AccessError(env._("Your role does not allow this operation."))


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

    allocation_calculated_at = fields.Datetime(
        string="Availability Calculated At",
        readonly=True,
        copy=False,
    )

    daily_stock_id = fields.Many2one(
        "restaurant.stock.daily",
        string="Source Daily Stock",
        readonly=True,
        copy=False,
        ondelete="restrict",
        index=True,
    )

    stock_section_id = fields.Many2one(
        related="daily_stock_id.section_id",
        string="Stock Section",
        store=True,
        readonly=True,
        index=True,
    )

    branch_status = fields.Selection(
        [
            ("requested", "Requested"),
            ("confirmed", "Confirmed"),
            ("delivered", "Delivered"),
            ("received", "Received"),
            ("rejected", "Rejected"),
            ("cancelled", "Cancelled"),
        ],
        string="Branch Status",
        compute="_compute_branch_status",
        store=True,
    )

    state = fields.Selection(
        [
            ("draft", "Draft"),
            ("submitted", "Submitted to Central Store"),
            ("transfer_ready", "Ready for Transfer"),
            ("purchase_required", "Purchase Required"),
            ("awaiting_operations", "Awaiting Operations Approval"),
            ("awaiting_owner", "Awaiting Owner Approval"),
            ("purchase_approved", "Purchase Approved"),
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

    _daily_stock_unique = models.Constraint(
        "UNIQUE(daily_stock_id)",
        "Only one stock request can be generated from a daily stock sheet.",
    )

    @api.depends("state")
    def _compute_branch_status(self):
        confirmed_states = {
            "transfer_ready", "purchase_required", "awaiting_operations",
            "awaiting_owner", "purchase_approved", "purchasing", "central_received",
        }
        for request in self:
            if request.state == "dispatched":
                request.branch_status = "delivered"
            elif request.state == "received":
                request.branch_status = "received"
            elif request.state in confirmed_states:
                request.branch_status = "confirmed"
            elif request.state in ("rejected", "cancelled"):
                request.branch_status = request.state
            else:
                request.branch_status = "requested"

    @api.model_create_multi
    def create(self, vals_list):
        require_branch_request_role(self.env)

        prepared = []

        for values in vals_list:
            values = dict(values)

            if values.get("state", "draft") != "draft":
                raise AccessError(
                    self.env._("Stock requests must be created in draft.")
                )

            generated = self.env.context.get("generated_from_daily_stock")
            protected = {
                "requested_by_id",
                "central_storekeeper_id",
                "purchasing_officer_id",
                "dispatched_by_id",
                "branch_received_by_id",
                "company_id",
                "daily_stock_id",
            }

            if generated:
                protected -= {"requested_by_id", "daily_stock_id"}

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
                "requested_by_id": values.get("requested_by_id", self.env.uid),
                "state": "draft",
            })

            prepared.append(values)

        records = super().create(prepared)

        require_assigned_branches(records.branch_id)

        return records

    def write(self, vals):
        if self.env.context.get("restaurant_purchase_approval_workflow"):
            return super().write(vals)

        if self.env.context.get("generated_from_daily_stock"):
            return super().write(vals)

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
            "selected_quote_id",
            "single_quote_justification",
            "approval_note",
        }

        if (
            self.env.user.has_group(STOCKKEEPER_GROUP)
            or self.env.user.has_group(MANAGER_GROUP)
        ):
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
        require_branch_request_role(self.env)
        require_assigned_branches(self.branch_id)

        if any(request.state != "draft" for request in self):
            raise AccessError(
                self.env._("Only draft stock requests can be deleted.")
            )

        return super().unlink()

    def _check_transition(self, target):
        self.ensure_one()

        if (self.state, target) in {
            ("draft", "submitted"),
            ("draft", "cancelled"),
            ("dispatched", "received"),
        }:
            require_branch_request_role(self.env)
            require_assigned_branches(self.branch_id)
            return

        transitions = {
            ("submitted", "transfer_ready"): CENTRAL_STOREKEEPER_GROUP,
            ("submitted", "purchase_required"): CENTRAL_STOREKEEPER_GROUP,
            ("submitted", "rejected"): CENTRAL_STOREKEEPER_GROUP,

            ("purchase_required", "awaiting_operations"): PURCHASING_GROUP,
            ("purchase_required", "awaiting_owner"): PURCHASING_GROUP,
            ("awaiting_operations", "purchase_approved"): OPERATIONS_GROUP,
            ("awaiting_owner", "purchase_approved"): OWNER_GROUP,
            ("awaiting_operations", "purchase_required"): OPERATIONS_GROUP,
            ("awaiting_owner", "purchase_required"): OWNER_GROUP,
            ("purchase_approved", "purchasing"): PURCHASING_GROUP,

            ("purchasing", "central_received"): CENTRAL_STOREKEEPER_GROUP,

            ("transfer_ready", "dispatched"): CENTRAL_STOREKEEPER_GROUP,
            ("central_received", "dispatched"): CENTRAL_STOREKEEPER_GROUP,

        }

        group = transitions.get((self.state, target))

        if not group:
            raise UserError(
                self.env._(
                    "This stock request workflow transition is not allowed."
                )
            )

        require_role(self.env, group)

    def action_submit(self):
        self.ensure_one()

        if not self.line_ids:
            raise ValidationError(
                self.env._("Add at least one item before submitting.")
            )

        if any(
            line.uom_id.compare(line.requested_qty, 0) <= 0
            for line in self.line_ids
        ):
            raise ValidationError(
                self.env._("Requested quantities must be greater than zero.")
            )

        return self.write({"state": "submitted"})

    def _get_central_warehouse(self, company):
        warehouse = self.env["stock.warehouse"].sudo().search([
            ("company_id", "=", company.id),
            ("code", "=", "WH"),
        ], limit=1)

        if not warehouse:
            raise UserError(
                self.env._(
                    "The Central Warehouse (WH) is not configured for this company."
                )
            )

        return warehouse

    def action_calculate_availability(self):
        """Reserve Central stock and store the resulting stock/purchase split."""
        self.ensure_one()
        require_role(self.env, CENTRAL_STOREKEEPER_GROUP)
        lock_records(self)
        self.invalidate_recordset(["state", "dispatch_picking_id"])

        if self.state != "submitted":
            raise UserError(
                self.env._(
                    "Availability can only be calculated during Central review."
                )
            )

        company = self.env.company
        if self.company_id != company:
            raise UserError(
                self.env._(
                    "Switch to the stock request company before calculating availability."
                )
            )

        warehouse = self._get_central_warehouse(company)
        transit_location = company.internal_transit_location_id
        if not transit_location:
            raise UserError(
                self.env._(
                    "The inter-warehouse transit location is not configured for this company."
                )
            )

        previous_picking = self.dispatch_picking_id.sudo()
        if previous_picking and previous_picking.state not in ("cancel", "done"):
            previous_picking.action_cancel()

        picking = self.env["stock.picking"].sudo().with_company(company).create({
            "picking_type_id": warehouse.int_type_id.id,
            "location_id": warehouse.lot_stock_id.id,
            "location_dest_id": transit_location.id,
            "origin": self.name,
            "company_id": company.id,
            "move_ids": [
                Command.create({
                    "product_id": line.product_id.id,
                    "product_uom_qty": line.requested_qty,
                    "uom_id": line.uom_id.id,
                    "location_id": warehouse.lot_stock_id.id,
                    "location_dest_id": transit_location.id,
                    "company_id": company.id,
                })
                for line in self.line_ids
            ],
        })
        picking.action_confirm()
        picking.action_assign()

        moves_by_product = {
            move.product_id.id: move
            for move in picking.move_ids
            if move.state != "cancel"
        }
        for line in self.line_ids:
            move = moves_by_product[line.product_id.id]
            reserved_qty = move.uom_id._compute_quantity(
                move.quantity,
                line.uom_id,
            )
            transfer_qty = min(line.requested_qty, reserved_qty)
            purchase_qty = line.requested_qty - transfer_qty
            line._record_allocation(transfer_qty, purchase_qty)

        super().write({
            "dispatch_picking_id": picking.id,
            "allocation_calculated_at": fields.Datetime.now(),
        })

        return {
            "type": "ir.actions.client",
            "tag": "display_notification",
            "params": {
                "title": self.env._("Central availability calculated"),
                "message": self.env._(
                    "Available stock was reserved and shortages were assigned to purchasing."
                ),
                "type": "success",
                "sticky": False,
            },
        }

    def action_prepare_transfer(self):
        self.ensure_one()

        if not self.allocation_calculated_at or not self.dispatch_picking_id:
            raise ValidationError(
                self.env._("Calculate Central availability before choosing a path.")
            )

        if any(
            not line.uom_id.is_zero(line.purchase_qty)
            for line in self.line_ids
        ):
            raise ValidationError(
                self.env._(
                    "This request has shortages and must be sent to Purchasing."
                )
            )

        for line in self.line_ids:
            line._record_dispatched_qty(line.confirmed_qty)

        return self.write({"state": "transfer_ready"})

    def action_require_purchase(self):
        self.ensure_one()

        if not self.allocation_calculated_at or not self.dispatch_picking_id:
            raise ValidationError(
                self.env._("Calculate Central availability before choosing a path.")
            )

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

        central_warehouse = self._get_central_warehouse(company)

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

    def _validated_completed_dispatch_quantities(self, picking):
        """Return immutable done-move quantities in each request line UoM.

        A Central transfer can be validated directly from native Inventory
        before the Restaurant Stock workflow records the dispatch.  Recovery
        is safe only when the linked completed picking still represents this
        exact request and route, and every completed quantity matches the
        confirmed request quantity.
        """
        self.ensure_one()
        company = self.company_id
        central_warehouse = self._get_central_warehouse(company)
        transit_location = company.internal_transit_location_id

        if (
            not picking
            or picking != self.dispatch_picking_id
            or picking.state != "done"
            or picking.company_id != company
            or picking.origin != self.name
            or picking.location_id != central_warehouse.lot_stock_id
            or picking.location_dest_id != transit_location
        ):
            raise UserError(
                self.env._(
                    "The completed Central dispatch transfer does not match "
                    "this request, company, or stock route."
                )
            )

        lines_by_product = {
            line.product_id.id: line
            for line in self.line_ids
        }
        quantities_by_line = {
            line.id: 0.0
            for line in self.line_ids
        }
        for move in picking.move_ids.filtered(
            lambda candidate: candidate.state == "done"
        ):
            line = lines_by_product.get(move.product_id.id)
            if not line:
                raise UserError(
                    self.env._(
                        "The completed Central dispatch contains an "
                        "unexpected product: %s.",
                        move.product_id.display_name,
                    )
                )
            if (
                move.company_id != company
                or move.location_id != central_warehouse.lot_stock_id
                or move.location_dest_id != transit_location
            ):
                raise UserError(
                    self.env._(
                        "The completed Central dispatch move for %s does not "
                        "use the expected WH/Stock to transit route.",
                        move.product_id.display_name,
                    )
                )
            quantities_by_line[line.id] += move.uom_id._compute_quantity(
                move.quantity,
                line.uom_id,
                round=False,
            )

        for line in self.line_ids:
            completed_qty = quantities_by_line[line.id]
            if line.uom_id.compare(
                completed_qty,
                line.confirmed_qty,
            ) != 0:
                raise UserError(
                    self.env._(
                        "The completed Central dispatch quantity for %s is "
                        "%s %s, but the request expects %s %s. No workflow "
                        "quantities were changed.",
                        line.product_id.display_name,
                        completed_qty,
                        line.uom_id.display_name,
                        line.confirmed_qty,
                        line.uom_id.display_name,
                    )
                )

        return quantities_by_line

    def _completed_dispatch_notification(self, already_synchronized=False):
        title = self.env._("Central dispatch already completed")
        if already_synchronized:
            message = self.env._(
                "The completed transfer is already synchronized with this "
                "stock request. No stock movement was created."
            )
        else:
            message = self.env._(
                "The existing completed Central transfer was validated and "
                "the request was synchronized for branch receipt. No new "
                "stock movement was created."
            )
        return {
            "type": "ir.actions.client",
            "tag": "display_notification",
            "params": {
                "title": title,
                "message": message,
                "type": "success",
                "sticky": False,
            },
        }

    def action_receive_central(self):
        self.ensure_one()

        lock_records(self)

        company = self.env.company

        if self.company_id != company:
            raise UserError(
                self.env._(
                    "Switch to the stock request company before verifying its receipt."
                )
            )

        dispatch_picking = self.dispatch_picking_id.sudo().with_company(
            company
        )
        if self.state == "dispatched" and dispatch_picking.state == "done":
            require_role(self.env, CENTRAL_STOREKEEPER_GROUP)
            completed_quantities = (
                self._validated_completed_dispatch_quantities(
                    dispatch_picking
                )
            )
            if any(
                line.uom_id.compare(
                    line.dispatched_qty,
                    completed_quantities[line.id],
                ) != 0
                for line in self.line_ids
            ):
                raise UserError(
                    self.env._(
                        "The completed Central dispatch is not synchronized "
                        "with the recorded delivery quantities."
                    )
                )
            return self._completed_dispatch_notification(
                already_synchronized=True
            )

        self._check_transition("central_received")

        purchase_order = self.purchase_order_id.sudo().with_company(company)

        if not purchase_order:
            raise UserError(
                self.env._("No purchase order is linked to this stock request.")
            )

        if purchase_order.state != "purchase":
            raise UserError(
                self.env._("Confirm the related purchase order before verifying receipts.")
            )

        central_warehouse = self._get_central_warehouse(company)

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
        received_quantities = {}

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
            received_quantities[request_line.id] = received_qty

            if request_line.uom_id.compare(
                received_qty,
                request_line.purchase_qty,
            ) < 0:
                all_received = False

        if not all_received:
            for request_line in self.line_ids:
                request_line._record_central_received_qty(
                    received_quantities[request_line.id]
                )
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

        if not dispatch_picking or dispatch_picking.state == "cancel":
            raise UserError(
                self.env._(
                    "The reserved Central dispatch transfer is missing or no longer active."
                )
            )

        if dispatch_picking.state == "done":
            completed_quantities = (
                self._validated_completed_dispatch_quantities(
                    dispatch_picking
                )
            )
            for request_line in self.line_ids:
                request_line._record_central_received_qty(
                    received_quantities[request_line.id]
                )
            self.write({"state": "central_received"})
            for request_line in self.line_ids:
                request_line._record_dispatched_qty(
                    completed_quantities[request_line.id]
                )
            self.write({"state": "dispatched"})
            self._notify_branch_delivery_ready()
            return self._completed_dispatch_notification()

        dispatch_picking.action_assign()
        if any(
            move.uom_id.compare(move.quantity, move.product_uom_qty) < 0
            for move in dispatch_picking.move_ids.filtered(
                lambda move: move.state not in ("cancel", "done")
            )
        ):
            raise UserError(
                self.env._(
                    "The complete request could not be reserved in Central stock. Replenish Central stock and verify the receipt again."
                )
            )

        for request_line in self.line_ids:
            request_line._record_central_received_qty(
                received_quantities[request_line.id]
            )
        for line in self.line_ids:
            line._record_dispatched_qty(line.confirmed_qty)

        return self.write({"state": "central_received"})

    def _notify_branch_delivery_ready(self):
        self.ensure_one()
        stockkeepers = self.branch_id.sudo().user_ids.filtered(
            lambda user: user.active and (
                user.has_group(STOCKKEEPER_GROUP)
                or user.has_group(MANAGER_GROUP)
            )
        )
        if not stockkeepers:
            return

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

    def action_dispatch(self):
        self.ensure_one()

        lock_records(self)
        self._check_transition("dispatched")

        company = self.env.company

        if self.company_id != company:
            raise UserError(
                self.env._(
                    "Switch to the stock request company before dispatching it."
                )
            )

        picking = self.dispatch_picking_id.sudo().with_company(company)
        if not picking or picking.state == "cancel":
            raise UserError(
                self.env._("Calculate Central availability before dispatching.")
            )
        if picking.state == "done":
            completed_quantities = (
                self._validated_completed_dispatch_quantities(picking)
            )
            for line in self.line_ids:
                line._record_dispatched_qty(
                    completed_quantities[line.id]
                )
            result = self.write({"state": "dispatched"})
            self._notify_branch_delivery_ready()
            return result

        delivery_lines = self.line_ids.filtered(
            lambda line: not line.uom_id.is_zero(line.dispatched_qty)
        )
        if not delivery_lines:
            raise ValidationError(
                self.env._("Enter a delivered quantity for at least one item.")
            )

        picking.do_unreserve()
        lines_by_product = {
            line.product_id.id: line
            for line in self.line_ids
        }
        for move in picking.move_ids.filtered(
            lambda move: move.state not in ("cancel", "done")
        ):
            line = lines_by_product[move.product_id.id]
            quantity = line.uom_id._compute_quantity(
                line.dispatched_qty,
                move.uom_id,
            )
            if move.uom_id.is_zero(quantity):
                move._action_cancel()
            else:
                move.product_uom_qty = quantity

        picking.action_assign()
        active_moves = picking.move_ids.filtered(
            lambda move: move.state not in ("cancel", "done")
        )
        if any(
            move.uom_id.compare(move.quantity, move.product_uom_qty) < 0
            for move in active_moves
        ):
            raise UserError(
                self.env._(
                    "The entered delivery quantities are not fully reserved in Central stock."
                )
            )

        validation_result = picking.with_context(skip_backorder=True).button_validate()

        if validation_result is not True or picking.state != "done":
            raise UserError(
                self.env._(
                    "The dispatch transfer could not be validated automatically."
                )
            )

        moves_by_product = {
            move.product_id.id: move
            for move in picking.move_ids
            if move.state == "done"
        }
        for line in self.line_ids:
            move = moves_by_product.get(line.product_id.id)
            quantity = 0.0
            if move:
                quantity = move.uom_id._compute_quantity(
                    move.quantity,
                    line.uom_id,
                )
            line._record_dispatched_qty(quantity)

        result = self.write({"state": "dispatched"})
        self._notify_branch_delivery_ready()
        return result

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

        for line in self.line_ids:
            if line.uom_id.compare(
                line.branch_received_qty, line.dispatched_qty
            ) > 0:
                raise ValidationError(self.env._(
                    "Received quantity cannot exceed delivery quantity for %s."
                ) % line.product_id.display_name)

        shortage_lines = self.line_ids.filtered(
            lambda line: line.uom_id.compare(
                line.branch_received_qty, line.dispatched_qty
            ) < 0
        )
        for line in shortage_lines:
            if not line.shortage_reason:
                raise ValidationError(self.env._(
                    "Select a shortage reason for %s."
                ) % line.product_id.display_name)
            if line.shortage_reason == "other" and not line.shortage_note:
                raise ValidationError(self.env._(
                    "Enter a shortage note for %s."
                ) % line.product_id.display_name)

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
        result = self.write({"state": "received"})

        self.env["restaurant.stock.daily"]._sync_completed_branch_receipt(
            picking,
            self.branch_id,
            self,
        )

        if shortage_lines:
            reason_labels = dict(
                self.env["restaurant.stock.request.line"]._fields[
                    "shortage_reason"
                ].selection
            )
            details = []
            for line in shortage_lines:
                detail = self.env._(
                    "%s: Delivered %s, Received %s, Difference %s, Reason: %s",
                    line.product_id.display_name,
                    line.dispatched_qty,
                    line.branch_received_qty,
                    line.difference_qty,
                    reason_labels.get(line.shortage_reason, line.shortage_reason),
                )
                if line.shortage_note:
                    detail += " (%s)" % line.shortage_note
                details.append(detail)
            message = "<b>%s</b><br/>%s" % (
                self.env._("Branch receipt shortage reported."),
                "<br/>".join(details),
            )
            partner_ids = self.dispatched_by_id.partner_id.ids
            self.message_post(
                body=message,
                partner_ids=partner_ids,
                message_type="notification",
            )
            if self.dispatched_by_id:
                self.activity_schedule(
                    "mail.mail_activity_data_todo",
                    user_id=self.dispatched_by_id.id,
                    summary=self.env._("Stock request shortage reported"),
                    note=message,
                )

        return result

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

    confirmed_qty = fields.Float(
        string="Confirmed Qty",
        compute="_compute_confirmed_qty",
        store=True,
    )

    current_on_hand_qty = fields.Float(
        string="Current On Hand",
        compute="_compute_current_on_hand_qty",
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

    shortage_note = fields.Char(string="Shortage Note")

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
        if self.env.context.get("generated_from_daily_stock") and self.env.su:
            return super().create(vals_list)

        require_branch_request_role(self.env)

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
        if (
            self.env.user.has_group(STOCKKEEPER_GROUP)
            or self.env.user.has_group(MANAGER_GROUP)
        ):
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
                    "shortage_reason",
                    "shortage_note",
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
                "note",
            }

            delivery_states = {
                "transfer_ready",
                "central_received",
            }
            if all(request.state in delivery_states for request in requests):
                allowed.add("dispatched_qty")

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

    def _record_allocation(self, transfer_qty, purchase_qty):
        require_role(self.env, CENTRAL_STOREKEEPER_GROUP)

        if any(line.request_id.state != "submitted" for line in self):
            raise UserError(
                self.env._(
                    "Availability can only be recorded during Central review."
                )
            )

        return super().write({
            "transfer_qty": transfer_qty,
            "purchase_qty": purchase_qty,
        })

    def _record_dispatched_qty(self, quantity):
        require_role(self.env, CENTRAL_STOREKEEPER_GROUP)
        return super().write({"dispatched_qty": quantity})


    def unlink(self):
        if self.env.context.get("generated_from_daily_stock") and self.env.su:
            return super().unlink()

        require_branch_request_role(self.env)
        require_assigned_branches(self.branch_id)

        if any(line.request_id.state != "draft" for line in self):
            raise AccessError(
                self.env._(
                    "Request items can only be deleted while the request is draft."
                )
            )

        return super().unlink()
    @api.depends("transfer_qty", "purchase_qty")
    def _compute_confirmed_qty(self):
        for line in self:
            line.confirmed_qty = line.transfer_qty + line.purchase_qty

    @api.depends("product_id", "request_id.branch_id")
    def _compute_current_on_hand_qty(self):
        for line in self:
            warehouse = line.branch_id.sudo().warehouse_id
            if not warehouse or not line.product_id:
                line.current_on_hand_qty = 0.0
                continue
            quants = self.env["stock.quant"].sudo().search([
                ("product_id", "=", line.product_id.id),
                ("location_id", "child_of", warehouse.lot_stock_id.id),
                ("company_id", "=", line.company_id.id),
            ])
            line.current_on_hand_qty = sum(quants.mapped("quantity"))

    @api.depends("dispatched_qty", "branch_received_qty")
    def _compute_difference(self):
        for line in self:
            line.difference_qty = (
                line.dispatched_qty - line.branch_received_qty
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

            if line.branch_received_qty > line.dispatched_qty:
                raise ValidationError(
                    self.env._(
                        "Branch received quantity cannot exceed delivery quantity."
                    )
                )

            if line.dispatched_qty > line.confirmed_qty:
                raise ValidationError(
                    self.env._(
                        "Delivered quantity cannot exceed confirmed quantity."
                    )
                )

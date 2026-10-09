from odoo import Command, api, fields, models
from odoo.exceptions import AccessError, UserError, ValidationError

from .stock_security import (
    CENTRAL_STOREKEEPER_GROUP,
    MANAGER_GROUP,
    OPERATIONS_GROUP,
    OWNER_GROUP,
    PURCHASING_GROUP,
    lock_records,
    require_assigned_branches,
    require_role,
)


REQUISITION_WORKFLOW_CONTEXT = "restaurant_purchase_requisition_workflow"


class RestaurantPurchaseRequisition(models.Model):
    _name = "restaurant.purchase.requisition"
    _description = "Restaurant Purchase Requisition"
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
    company_id = fields.Many2one(
        related="branch_id.company_id",
        store=True,
        readonly=True,
        index=True,
    )
    company_currency_id = fields.Many2one(
        related="company_id.currency_id",
        string="Company Currency",
        readonly=True,
    )
    request_date = fields.Date(
        required=True,
        default=fields.Date.context_today,
        index=True,
        tracking=True,
    )
    needed_by = fields.Date(required=True, tracking=True)
    requested_by_id = fields.Many2one(
        "res.users",
        required=True,
        readonly=True,
        default=lambda self: self.env.user,
        tracking=True,
    )
    purpose = fields.Text(required=True, tracking=True)
    state = fields.Selection(
        [
            ("draft", "Draft"),
            ("submitted", "Submitted to Purchasing"),
            ("sourcing", "Collecting Quotations"),
            ("awaiting_operations", "Awaiting Operations Approval"),
            ("awaiting_owner", "Awaiting Owner Approval"),
            ("approved", "Approved"),
            ("ordered", "Purchase Order Created"),
            ("fulfilled", "Fulfilled"),
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
        "restaurant.purchase.requisition.line",
        "requisition_id",
        string="Requested Items",
        copy=True,
    )
    offer_ids = fields.One2many(
        "restaurant.purchase.requisition.offer",
        "requisition_id",
        string="Vendor Quotations",
        copy=False,
    )
    selected_offer_id = fields.Many2one(
        "restaurant.purchase.requisition.offer",
        string="Selected Quotation",
        copy=False,
        tracking=True,
        domain="[('requisition_id', '=', id)]",
    )
    offer_count = fields.Integer(compute="_compute_offer_count")
    approval_amount = fields.Monetary(
        currency_field="company_currency_id",
        compute="_compute_approval_amount",
        store=True,
    )
    single_quote_justification = fields.Text(copy=False, tracking=True)
    purchasing_note = fields.Text(copy=False, tracking=True)
    approval_note = fields.Text(copy=False, tracking=True)
    approval_requested_by_id = fields.Many2one(
        "res.users", readonly=True, copy=False,
    )
    approval_requested_at = fields.Datetime(readonly=True, copy=False)
    approved_by_id = fields.Many2one("res.users", readonly=True, copy=False)
    approved_at = fields.Datetime(readonly=True, copy=False)
    purchase_order_id = fields.Many2one(
        "purchase.order",
        readonly=True,
        copy=False,
        check_company=True,
    )
    purchase_receipt_ids = fields.Many2many(
        related="purchase_order_id.picking_ids",
        readonly=True,
        string="Supplier Receipts",
    )
    vendor_bill_ids = fields.Many2many(
        related="purchase_order_id.invoice_ids",
        readonly=True,
        string="Vendor Bills",
    )
    goods_receipt_required = fields.Boolean(
        compute="_compute_fulfillment_requirements",
        store=True,
    )
    service_acceptance_required = fields.Boolean(
        compute="_compute_fulfillment_requirements",
        store=True,
    )
    goods_received = fields.Boolean(readonly=True, copy=False, tracking=True)
    goods_received_by_id = fields.Many2one("res.users", readonly=True, copy=False)
    goods_received_at = fields.Datetime(readonly=True, copy=False)
    services_accepted = fields.Boolean(readonly=True, copy=False, tracking=True)
    services_accepted_by_id = fields.Many2one("res.users", readonly=True, copy=False)
    services_accepted_at = fields.Datetime(readonly=True, copy=False)

    @api.depends("offer_ids")
    def _compute_offer_count(self):
        grouped = self.env["restaurant.purchase.requisition.offer"]._read_group(
            [("requisition_id", "in", self.ids)],
            ["requisition_id"],
            ["__count"],
        ) if self.ids else []
        counts = {requisition.id: count for requisition, count in grouped}
        for requisition in self:
            requisition.offer_count = counts.get(requisition.id, 0)

    @api.depends("selected_offer_id.amount_company_currency")
    def _compute_approval_amount(self):
        for requisition in self:
            requisition.approval_amount = (
                requisition.selected_offer_id.amount_company_currency
                if requisition.selected_offer_id else 0.0
            )

    @api.depends("line_ids.product_id", "line_ids.product_id.type")
    def _compute_fulfillment_requirements(self):
        for requisition in self:
            requisition.goods_receipt_required = any(
                line.product_id.type != "service" for line in requisition.line_ids
            )
            requisition.service_acceptance_required = any(
                line.product_id.type == "service" for line in requisition.line_ids
            )

    @api.model_create_multi
    def create(self, vals_list):
        require_role(self.env, MANAGER_GROUP)
        prepared = []
        for vals in vals_list:
            values = dict(vals)
            if values.get("state", "draft") != "draft":
                raise AccessError(self.env._("Purchase requisitions must be created in Draft."))
            protected = {
                "name", "requested_by_id", "company_id", "purchase_order_id",
                "approved_by_id", "approved_at", "approval_requested_by_id",
                "approval_requested_at", "goods_received", "services_accepted",
            }
            if set(values) & protected:
                raise AccessError(self.env._("Workflow fields cannot be supplied manually."))
            values.update({
                "name": self.env["ir.sequence"].next_by_code(
                    "restaurant.purchase.requisition"
                ) or "New",
                "requested_by_id": self.env.uid,
                "state": "draft",
            })
            prepared.append(values)
        records = super().create(prepared)
        require_assigned_branches(records.branch_id)
        return records

    def write(self, vals):
        if self.env.context.get(REQUISITION_WORKFLOW_CONTEXT):
            return super().write(vals)
        lock_records(self)

        if "state" in vals:
            raise AccessError(self.env._("Use the workflow buttons to change status."))

        draft_fields = {"branch_id", "request_date", "needed_by", "purpose", "line_ids"}
        sourcing_fields = {
            "selected_offer_id", "single_quote_justification", "purchasing_note",
        }
        if all(record.state == "draft" for record in self):
            require_role(self.env, MANAGER_GROUP)
            require_assigned_branches(self.branch_id)
            if set(vals) - draft_fields:
                raise AccessError(self.env._("Only draft request details can be edited."))
            if "branch_id" in vals:
                require_assigned_branches(
                    self.env["restaurant.branch"].browse(vals["branch_id"])
                )
            return super().write(vals)
        if all(record.state == "sourcing" for record in self):
            require_role(self.env, PURCHASING_GROUP)
            if set(vals) - sourcing_fields:
                raise AccessError(self.env._("Purchasing can only edit sourcing details."))
            return super().write(vals)
        if set(vals) <= {"approval_note"} and all(
            record.state in ("awaiting_operations", "awaiting_owner") for record in self
        ):
            for record in self:
                require_role(
                    self.env,
                    OPERATIONS_GROUP if record.state == "awaiting_operations" else OWNER_GROUP,
                )
            return super().write(vals)
        raise AccessError(self.env._("This requisition is locked in its current state."))

    def unlink(self):
        require_role(self.env, MANAGER_GROUP)
        require_assigned_branches(self.branch_id)
        if any(record.state != "draft" for record in self):
            raise AccessError(self.env._("Only draft requisitions can be deleted."))
        return super().unlink()

    def _workflow_write(self, values):
        return self.with_context(**{REQUISITION_WORKFLOW_CONTEXT: True}).write(values)

    def action_submit(self):
        self.ensure_one()
        require_role(self.env, MANAGER_GROUP)
        require_assigned_branches(self.branch_id)
        lock_records(self)
        if self.state != "draft":
            raise UserError(self.env._("Only draft requisitions can be submitted."))
        if not self.line_ids:
            raise ValidationError(self.env._("Add at least one requested item."))
        if any(line.quantity <= 0 for line in self.line_ids):
            raise ValidationError(self.env._("Requested quantities must be positive."))
        self._workflow_write({"state": "submitted"})
        return True

    def action_cancel(self):
        self.ensure_one()
        require_role(self.env, MANAGER_GROUP)
        require_assigned_branches(self.branch_id)
        if self.state != "draft":
            raise UserError(self.env._("Only draft requisitions can be cancelled."))
        self._workflow_write({"state": "cancelled"})
        return True

    def action_start_sourcing(self):
        self.ensure_one()
        require_role(self.env, PURCHASING_GROUP)
        lock_records(self)
        if self.state != "submitted":
            raise UserError(self.env._("Only submitted requisitions can enter sourcing."))
        self._workflow_write({"state": "sourcing"})
        return True

    def action_open_offers(self):
        self.ensure_one()
        return {
            "type": "ir.actions.act_window",
            "name": self.env._("Vendor Quotations"),
            "res_model": "restaurant.purchase.requisition.offer",
            "view_mode": "list,form",
            "domain": [("requisition_id", "=", self.id)],
            "context": {"default_requisition_id": self.id},
        }

    def action_submit_approval(self):
        self.ensure_one()
        require_role(self.env, PURCHASING_GROUP)
        lock_records(self)
        if self.state != "sourcing":
            raise UserError(self.env._("Only sourcing requisitions can be submitted."))
        if not self.selected_offer_id or self.selected_offer_id.state != "ready":
            raise ValidationError(self.env._("Select a complete vendor quotation."))
        if self.selected_offer_id.requisition_id != self:
            raise ValidationError(self.env._("The selected quotation belongs to another requisition."))
        offers = self.offer_ids.filtered(lambda offer: offer.state == "ready")
        if len(offers) < 2 and not self.single_quote_justification:
            raise ValidationError(
                self.env._("Add two quotations, or document the single-quote exception.")
            )
        threshold = self.company_id.restaurant_owner_purchase_approval_threshold
        target = (
            "awaiting_owner"
            if threshold >= 0 and self.approval_amount > threshold
            else "awaiting_operations"
        )
        self._workflow_write({
            "state": target,
            "approval_requested_by_id": self.env.uid,
            "approval_requested_at": fields.Datetime.now(),
            "approved_by_id": False,
            "approved_at": False,
            "approval_note": False,
        })
        return True

    def action_approve(self):
        self.ensure_one()
        lock_records(self)
        if self.state == "awaiting_operations":
            require_role(self.env, OPERATIONS_GROUP)
        elif self.state == "awaiting_owner":
            require_role(self.env, OWNER_GROUP)
        else:
            raise UserError(self.env._("This requisition is not awaiting approval."))
        self._workflow_write({
            "state": "approved",
            "approved_by_id": self.env.uid,
            "approved_at": fields.Datetime.now(),
        })
        return True

    def action_return_to_sourcing(self):
        self.ensure_one()
        lock_records(self)
        if self.state == "awaiting_operations":
            require_role(self.env, OPERATIONS_GROUP)
        elif self.state == "awaiting_owner":
            require_role(self.env, OWNER_GROUP)
        else:
            raise UserError(self.env._("This requisition is not awaiting approval."))
        if not self.approval_note:
            raise ValidationError(self.env._("Enter the reason for returning this requisition."))
        self._workflow_write({
            "state": "sourcing",
            "approved_by_id": False,
            "approved_at": False,
        })
        return True

    def action_create_purchase_order(self):
        self.ensure_one()
        require_role(self.env, PURCHASING_GROUP)
        lock_records(self)
        if self.state != "approved":
            raise UserError(self.env._("Approve the requisition before creating its purchase order."))
        if self.purchase_order_id:
            raise UserError(self.env._("A purchase order already exists."))
        if self.company_id != self.env.company:
            raise UserError(self.env._("Switch to the requisition company first."))
        offer = self.selected_offer_id
        warehouse = self.env["stock.warehouse"].search([
            ("company_id", "=", self.company_id.id),
            ("code", "=", "WH"),
        ], limit=1)
        values = {
            "partner_id": offer.vendor_id.id,
            "company_id": self.company_id.id,
            "currency_id": offer.currency_id.id,
            "user_id": self.env.uid,
            "origin": self.name,
            "order_line": [
                Command.create({
                    "product_id": line.product_id.id,
                    "name": line.description or line.product_id.display_name,
                    "product_qty": line.quantity,
                    "uom_id": line.uom_id.id,
                    "price_unit": line.price_unit,
                    "date_planned": fields.Datetime.to_datetime(self.needed_by),
                })
                for line in offer.line_ids
            ],
        }
        if warehouse:
            values["picking_type_id"] = warehouse.in_type_id.id
        purchase_order = self.env["purchase.order"].with_company(
            self.company_id
        ).create(values)
        self._workflow_write({
            "purchase_order_id": purchase_order.id,
            "state": "ordered",
        })
        return {
            "type": "ir.actions.act_window",
            "res_model": "purchase.order",
            "res_id": purchase_order.id,
            "view_mode": "form",
        }

    def action_verify_goods_receipt(self):
        self.ensure_one()
        require_role(self.env, CENTRAL_STOREKEEPER_GROUP)
        lock_records(self)
        if self.state != "ordered" or not self.goods_receipt_required:
            raise UserError(self.env._("No goods receipt is awaiting verification."))
        pickings = self.purchase_receipt_ids.filtered(lambda picking: picking.state != "cancel")
        if not pickings or any(picking.state != "done" for picking in pickings):
            raise ValidationError(self.env._("Complete all supplier receipts first."))
        self._workflow_write({
            "goods_received": True,
            "goods_received_by_id": self.env.uid,
            "goods_received_at": fields.Datetime.now(),
        })
        return self._complete_if_fulfilled()

    def action_accept_services(self):
        self.ensure_one()
        require_role(self.env, OPERATIONS_GROUP)
        lock_records(self)
        if self.state != "ordered" or not self.service_acceptance_required:
            raise UserError(self.env._("No service acceptance is awaiting confirmation."))
        if self.purchase_order_id.state not in ("purchase", "done"):
            raise ValidationError(self.env._("Confirm the purchase order first."))
        self._workflow_write({
            "services_accepted": True,
            "services_accepted_by_id": self.env.uid,
            "services_accepted_at": fields.Datetime.now(),
        })
        return self._complete_if_fulfilled()

    def _complete_if_fulfilled(self):
        self.ensure_one()
        goods_complete = not self.goods_receipt_required or self.goods_received
        services_complete = not self.service_acceptance_required or self.services_accepted
        if goods_complete and services_complete:
            self._workflow_write({"state": "fulfilled"})
        return True


class RestaurantPurchaseRequisitionLine(models.Model):
    _name = "restaurant.purchase.requisition.line"
    _description = "Restaurant Purchase Requisition Line"
    _order = "id"

    requisition_id = fields.Many2one(
        "restaurant.purchase.requisition", required=True, ondelete="cascade", index=True,
    )
    product_id = fields.Many2one(
        "product.product",
        required=True,
        ondelete="restrict",
        domain=[("purchase_ok", "=", True)],
    )
    description = fields.Char()
    quantity = fields.Float(required=True, default=1.0)
    uom_id = fields.Many2one("uom.uom", required=True)
    company_id = fields.Many2one(
        related="requisition_id.company_id", store=True, readonly=True, index=True,
    )
    branch_id = fields.Many2one(
        related="requisition_id.branch_id", store=True, readonly=True, index=True,
    )

    @api.onchange("product_id")
    def _onchange_product_id(self):
        if self.product_id:
            self.uom_id = self.product_id.uom_id
            self.description = self.product_id.display_name

    @api.constrains("quantity")
    def _check_quantity(self):
        if any(line.quantity <= 0 for line in self):
            raise ValidationError(self.env._("Requested quantity must be positive."))

    @api.model_create_multi
    def create(self, vals_list):
        require_role(self.env, MANAGER_GROUP)
        requisitions = self.env["restaurant.purchase.requisition"].browse(
            [values.get("requisition_id") for values in vals_list if values.get("requisition_id")]
        )
        if not requisitions or any(requisition.state != "draft" for requisition in requisitions):
            raise AccessError(self.env._("Items can only be added to draft requisitions."))
        require_assigned_branches(requisitions.branch_id)
        return super().create(vals_list)

    def write(self, vals):
        require_role(self.env, MANAGER_GROUP)
        if any(line.requisition_id.state != "draft" for line in self):
            raise AccessError(self.env._("Submitted requisition items are locked."))
        require_assigned_branches(self.requisition_id.branch_id)
        if "requisition_id" in vals:
            raise AccessError(self.env._("A requisition item cannot be moved."))
        return super().write(vals)

    def unlink(self):
        require_role(self.env, MANAGER_GROUP)
        if any(line.requisition_id.state != "draft" for line in self):
            raise AccessError(self.env._("Submitted requisition items are locked."))
        require_assigned_branches(self.requisition_id.branch_id)
        return super().unlink()


class RestaurantPurchaseRequisitionOffer(models.Model):
    _name = "restaurant.purchase.requisition.offer"
    _description = "Restaurant Purchase Requisition Vendor Quotation"
    _inherit = ["mail.thread", "mail.activity.mixin"]
    _order = "quote_date desc, id desc"

    name = fields.Char(required=True, default="New", tracking=True)
    requisition_id = fields.Many2one(
        "restaurant.purchase.requisition",
        required=True,
        ondelete="cascade",
        index=True,
        tracking=True,
    )
    company_id = fields.Many2one(
        related="requisition_id.company_id", store=True, readonly=True, index=True,
    )
    vendor_id = fields.Many2one(
        "res.partner",
        required=True,
        ondelete="restrict",
        domain=[("supplier_rank", ">", 0)],
        tracking=True,
    )
    quote_date = fields.Date(required=True, default=fields.Date.context_today)
    valid_until = fields.Date()
    currency_id = fields.Many2one(
        "res.currency", required=True, default=lambda self: self.env.company.currency_id,
    )
    company_currency_id = fields.Many2one(
        related="company_id.currency_id", string="Company Currency", readonly=True,
    )
    line_ids = fields.One2many(
        "restaurant.purchase.requisition.offer.line", "offer_id", copy=True,
    )
    amount_total = fields.Monetary(
        compute="_compute_amounts", store=True, currency_field="currency_id",
    )
    amount_company_currency = fields.Monetary(
        compute="_compute_amounts", store=True, currency_field="company_currency_id",
    )
    attachment = fields.Binary(string="Vendor Quotation", attachment=True)
    attachment_filename = fields.Char()
    state = fields.Selection(
        [("draft", "Draft"), ("ready", "Ready")],
        default="draft", required=True, readonly=True, tracking=True,
    )
    is_selected = fields.Boolean(compute="_compute_is_selected")

    @api.depends("line_ids.subtotal", "currency_id", "quote_date", "company_id")
    def _compute_amounts(self):
        for offer in self:
            total = sum(offer.line_ids.mapped("subtotal"))
            offer.amount_total = total
            offer.amount_company_currency = offer.currency_id._convert(
                total,
                offer.company_id.currency_id,
                offer.company_id,
                offer.quote_date or fields.Date.context_today(offer),
            ) if offer.currency_id and offer.company_id else total

    @api.depends("requisition_id.selected_offer_id")
    def _compute_is_selected(self):
        for offer in self:
            offer.is_selected = offer.requisition_id.selected_offer_id == offer

    @api.model_create_multi
    def create(self, vals_list):
        require_role(self.env, PURCHASING_GROUP)
        prepared = []
        for vals in vals_list:
            values = dict(vals)
            requisition = self.env["restaurant.purchase.requisition"].browse(
                values.get("requisition_id")
            )
            if not requisition or requisition.state != "sourcing":
                raise ValidationError(self.env._("Quotations require a sourcing requisition."))
            if not values.get("line_ids"):
                values["line_ids"] = [
                    Command.create({
                        "requisition_line_id": line.id,
                        "product_id": line.product_id.id,
                        "description": line.description,
                        "quantity": line.quantity,
                        "uom_id": line.uom_id.id,
                    }) for line in requisition.line_ids
                ]
            prepared.append(values)
        return super().create(prepared)

    def write(self, vals):
        require_role(self.env, PURCHASING_GROUP)
        if any(offer.requisition_id.state != "sourcing" for offer in self):
            raise AccessError(self.env._("Submitted or approved quotations cannot be edited."))
        if "requisition_id" in vals:
            raise AccessError(self.env._("A quotation cannot be moved."))
        if any(offer.state == "ready" for offer in self):
            if set(vals) != {"state"} or vals.get("state") != "draft":
                raise AccessError(self.env._("Reset a ready quotation to Draft before editing it."))
        elif "state" in vals and vals.get("state") != "ready":
            raise AccessError(self.env._("Use the quotation workflow buttons to change status."))
        return super().write(vals)

    def unlink(self):
        require_role(self.env, PURCHASING_GROUP)
        if any(offer.requisition_id.state != "sourcing" for offer in self):
            raise AccessError(self.env._("Submitted quotations cannot be deleted."))
        if any(offer.is_selected for offer in self):
            raise ValidationError(self.env._("Unselect the quotation before deleting it."))
        return super().unlink()

    def action_mark_ready(self):
        for offer in self:
            require_role(self.env, PURCHASING_GROUP)
            if offer.requisition_id.state != "sourcing" or offer.state != "draft":
                raise UserError(self.env._("Only draft sourcing quotations can be completed."))
            if not offer.attachment:
                raise ValidationError(self.env._("Upload the original vendor quotation."))
            if len(offer.line_ids) != len(offer.requisition_id.line_ids):
                raise ValidationError(self.env._("Quote every requested item."))
            requested_ids = offer.requisition_id.line_ids.ids
            quoted_ids = offer.line_ids.mapped("requisition_line_id").ids
            if len(set(quoted_ids)) != len(quoted_ids) or set(quoted_ids) != set(requested_ids):
                raise ValidationError(self.env._("Quote every requested item exactly once."))
            if any(line.quantity <= 0 or line.price_unit <= 0 for line in offer.line_ids):
                raise ValidationError(self.env._("Every item needs a quantity and unit price."))
            offer.write({"state": "ready"})
        return True

    def action_reset_draft(self):
        for offer in self:
            if offer.requisition_id.state != "sourcing":
                raise UserError(self.env._("This requisition is no longer editable."))
            offer.write({"state": "draft"})
        return True

    def action_select(self):
        self.ensure_one()
        require_role(self.env, PURCHASING_GROUP)
        if self.state != "ready" or self.requisition_id.state != "sourcing":
            raise ValidationError(self.env._("Only a ready sourcing quotation can be selected."))
        self.requisition_id.with_context(**{REQUISITION_WORKFLOW_CONTEXT: True}).write({
            "selected_offer_id": self.id,
        })
        return {"type": "ir.actions.act_window_close"}


class RestaurantPurchaseRequisitionOfferLine(models.Model):
    _name = "restaurant.purchase.requisition.offer.line"
    _description = "Restaurant Purchase Requisition Quotation Line"
    _order = "id"

    offer_id = fields.Many2one(
        "restaurant.purchase.requisition.offer", required=True, ondelete="cascade", index=True,
    )
    requisition_line_id = fields.Many2one(
        "restaurant.purchase.requisition.line", required=True, ondelete="restrict",
    )
    product_id = fields.Many2one("product.product", required=True, readonly=True)
    description = fields.Char()
    quantity = fields.Float(required=True)
    uom_id = fields.Many2one("uom.uom", required=True)
    price_unit = fields.Monetary(
        required=True, default=0.0, currency_field="currency_id",
    )
    currency_id = fields.Many2one(related="offer_id.currency_id", readonly=True)
    subtotal = fields.Monetary(
        compute="_compute_subtotal", store=True, currency_field="currency_id",
    )
    company_id = fields.Many2one(
        related="offer_id.company_id", store=True, readonly=True, index=True,
    )

    @api.depends("quantity", "price_unit")
    def _compute_subtotal(self):
        for line in self:
            line.subtotal = line.quantity * line.price_unit

    @api.model_create_multi
    def create(self, vals_list):
        require_role(self.env, PURCHASING_GROUP)
        offers = self.env["restaurant.purchase.requisition.offer"].browse(
            [vals.get("offer_id") for vals in vals_list if vals.get("offer_id")]
        )
        if any(offer.state != "draft" or offer.requisition_id.state != "sourcing" for offer in offers):
            raise AccessError(self.env._("Quotation lines require a draft sourcing quotation."))
        return super().create(vals_list)

    def write(self, vals):
        require_role(self.env, PURCHASING_GROUP)
        if any(
            line.offer_id.state != "draft"
            or line.offer_id.requisition_id.state != "sourcing"
            for line in self
        ):
            raise AccessError(self.env._("Ready quotation lines cannot be edited."))
        if set(vals) & {"offer_id", "requisition_line_id", "product_id"}:
            raise AccessError(self.env._("Quotation item links cannot be changed."))
        return super().write(vals)

    def unlink(self):
        require_role(self.env, PURCHASING_GROUP)
        if any(
            line.offer_id.state != "draft"
            or line.offer_id.requisition_id.state != "sourcing"
            for line in self
        ):
            raise AccessError(self.env._("Ready quotation lines cannot be deleted."))
        return super().unlink()

    @api.constrains("requisition_line_id", "offer_id", "quantity")
    def _check_source_and_quantity(self):
        for line in self:
            if line.requisition_line_id.requisition_id != line.offer_id.requisition_id:
                raise ValidationError(self.env._("Quoted items must belong to this requisition."))
            if line.quantity != line.requisition_line_id.quantity:
                raise ValidationError(self.env._("Quoted quantity must match the requested quantity."))

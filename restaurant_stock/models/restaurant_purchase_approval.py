from odoo import Command, api, fields, models
from odoo.exceptions import AccessError, UserError, ValidationError

from .stock_security import (
    OPERATIONS_GROUP,
    OWNER_GROUP,
    PURCHASING_GROUP,
    lock_records,
    require_role,
)


PURCHASE_APPROVAL_CONTEXT = "restaurant_purchase_approval_workflow"


class ResCompany(models.Model):
    _inherit = "res.company"

    restaurant_owner_purchase_approval_threshold = fields.Monetary(
        string="Owner Purchase Approval Threshold",
        currency_field="currency_id",
        default=5000.0,
        help=(
            "Purchases above this amount require Restaurant Owner approval. "
            "Purchases up to this amount require Operations Manager approval."
        ),
    )


class ResConfigSettings(models.TransientModel):
    _inherit = "res.config.settings"

    restaurant_owner_purchase_approval_threshold = fields.Monetary(
        related="company_id.restaurant_owner_purchase_approval_threshold",
        readonly=False,
        currency_field="company_currency_id",
    )


class RestaurantStockRequest(models.Model):
    _inherit = "restaurant.stock.request"

    quote_ids = fields.One2many(
        "restaurant.purchase.quote",
        "request_id",
        string="Vendor Quotations",
        copy=False,
    )
    selected_quote_id = fields.Many2one(
        "restaurant.purchase.quote",
        string="Selected Quotation",
        copy=False,
        tracking=True,
        domain="[('request_id', '=', id)]",
    )
    quote_count = fields.Integer(compute="_compute_quote_count")
    approval_amount = fields.Monetary(
        string="Approval Amount",
        currency_field="company_currency_id",
        compute="_compute_approval_amount",
        store=True,
    )
    company_currency_id = fields.Many2one(
        related="company_id.currency_id",
        string="Company Currency",
        readonly=True,
    )
    single_quote_justification = fields.Text(
        string="Single-Quote Justification",
        copy=False,
        tracking=True,
    )
    approval_note = fields.Text(copy=False, tracking=True)
    approval_requested_by_id = fields.Many2one(
        "res.users",
        string="Approval Requested By",
        readonly=True,
        copy=False,
    )
    approval_requested_at = fields.Datetime(readonly=True, copy=False)
    approved_by_id = fields.Many2one(
        "res.users",
        string="Approved By",
        readonly=True,
        copy=False,
    )
    approved_at = fields.Datetime(readonly=True, copy=False)

    @api.depends("quote_ids")
    def _compute_quote_count(self):
        grouped = self.env["restaurant.purchase.quote"]._read_group(
            [("request_id", "in", self.ids)],
            ["request_id"],
            ["__count"],
        ) if self.ids else []
        counts = {request.id: count for request, count in grouped}
        for request in self:
            request.quote_count = counts.get(request.id, 0)

    @api.depends("selected_quote_id.amount_company_currency")
    def _compute_approval_amount(self):
        for request in self:
            request.approval_amount = (
                request.selected_quote_id.amount_company_currency
                if request.selected_quote_id
                else 0.0
            )

    def write(self, vals):
        if self.env.context.get(PURCHASE_APPROVAL_CONTEXT):
            return super().write(vals)
        if set(vals) <= {"approval_note"} and all(
            request.state in ("awaiting_operations", "awaiting_owner")
            for request in self
        ):
            for request in self:
                group = (
                    OPERATIONS_GROUP
                    if request.state == "awaiting_operations"
                    else OWNER_GROUP
                )
                require_role(self.env, group)
            return self.with_context(**{PURCHASE_APPROVAL_CONTEXT: True}).write(vals)
        return super().write(vals)

    def action_open_quotes(self):
        self.ensure_one()
        return {
            "type": "ir.actions.act_window",
            "name": self.env._("Vendor Quotations"),
            "res_model": "restaurant.purchase.quote",
            "view_mode": "list,form",
            "domain": [("request_id", "=", self.id)],
            "context": {"default_request_id": self.id},
        }

    def action_submit_purchase_approval(self):
        self.ensure_one()
        require_role(self.env, PURCHASING_GROUP)
        lock_records(self)
        self.invalidate_recordset(["state", "selected_quote_id", "quote_ids"])

        if self.state != "purchase_required":
            raise UserError(
                self.env._("Only purchase-required requests can be submitted for approval.")
            )
        if not self.selected_quote_id:
            raise ValidationError(self.env._("Select a vendor quotation first."))

        quotations = self.quote_ids.filtered(lambda quote: quote.state == "ready")
        if self.selected_quote_id not in quotations:
            raise ValidationError(
                self.env._("The selected quotation must be complete and marked Ready.")
            )
        if len(quotations) < 2 and not self.single_quote_justification:
            raise ValidationError(
                self.env._(
                    "Add at least two ready quotations, or document why only one quotation is available."
                )
            )

        threshold = self.company_id.restaurant_owner_purchase_approval_threshold
        target = (
            "awaiting_owner"
            if threshold >= 0 and self.approval_amount > threshold
            else "awaiting_operations"
        )
        self._check_transition(target)
        self.with_context(**{PURCHASE_APPROVAL_CONTEXT: True}).write({
            "state": target,
            "approval_requested_by_id": self.env.uid,
            "approval_requested_at": fields.Datetime.now(),
            "approved_by_id": False,
            "approved_at": False,
            "approval_note": False,
        })
        self.message_post(
            body=self.env._(
                "Purchase approval requested by %(user)s for %(amount)s %(currency)s.",
                user=self.env.user.name,
                amount=self.approval_amount,
                currency=self.company_currency_id.name,
            )
        )
        return True

    def action_approve_purchase(self):
        self.ensure_one()
        lock_records(self)
        self.invalidate_recordset(["state"])
        if self.state == "awaiting_operations":
            require_role(self.env, OPERATIONS_GROUP)
        elif self.state == "awaiting_owner":
            require_role(self.env, OWNER_GROUP)
        else:
            raise UserError(self.env._("This request is not awaiting approval."))

        self._check_transition("purchase_approved")
        self.with_context(**{PURCHASE_APPROVAL_CONTEXT: True}).write({
            "state": "purchase_approved",
            "approved_by_id": self.env.uid,
            "approved_at": fields.Datetime.now(),
        })
        self.message_post(
            body=self.env._("Purchase approved by %s.", self.env.user.name)
        )
        return True

    def action_return_to_purchasing(self):
        self.ensure_one()
        lock_records(self)
        self.invalidate_recordset(["state", "approval_note"])
        if self.state == "awaiting_operations":
            require_role(self.env, OPERATIONS_GROUP)
        elif self.state == "awaiting_owner":
            require_role(self.env, OWNER_GROUP)
        else:
            raise UserError(self.env._("This request is not awaiting approval."))
        if not self.approval_note:
            raise ValidationError(
                self.env._("Enter an approval note explaining what Purchasing must revise.")
            )

        self._check_transition("purchase_required")
        self.with_context(**{PURCHASE_APPROVAL_CONTEXT: True}).write({
            "state": "purchase_required",
            "approved_by_id": False,
            "approved_at": False,
        })
        self.message_post(
            body=self.env._(
                "Purchase returned to Purchasing by %(user)s: %(reason)s",
                user=self.env.user.name,
                reason=self.approval_note,
            )
        )
        return True

    def action_start_purchasing(self):
        self.ensure_one()
        require_role(self.env, PURCHASING_GROUP)
        lock_records(self)
        self.invalidate_recordset(["state", "purchase_order_id", "selected_quote_id"])

        if self.purchase_order_id:
            raise UserError(
                self.env._("A purchase order already exists for this stock request.")
            )
        if self.state != "purchase_approved":
            raise UserError(
                self.env._("The selected quotation must be approved before creating a purchase order.")
            )
        if not self.selected_quote_id or self.selected_quote_id.state != "ready":
            raise ValidationError(self.env._("The approved quotation is not ready."))

        self._check_transition("purchasing")
        company = self.env.company
        if self.company_id != company:
            raise UserError(
                self.env._("Switch to the stock request company before creating its purchase order.")
            )

        central_warehouse = self._get_central_warehouse(company)
        quote = self.selected_quote_id
        order_lines = []
        for line in quote.line_ids:
            order_lines.append(Command.create({
                "product_id": line.product_id.id,
                "product_qty": line.quantity,
                "uom_id": line.uom_id.id,
                "price_unit": line.price_unit,
            }))

        purchase_order = self.env["purchase.order"].with_company(company).create({
            "partner_id": quote.vendor_id.id,
            "company_id": company.id,
            "currency_id": quote.currency_id.id,
            "user_id": self.env.uid,
            "origin": self.name,
            "picking_type_id": central_warehouse.in_type_id.id,
            "order_line": order_lines,
        })
        self.with_context(**{PURCHASE_APPROVAL_CONTEXT: True}).write({
            "vendor_id": quote.vendor_id.id,
            "purchase_order_id": purchase_order.id,
            "purchasing_officer_id": self.env.uid,
            "state": "purchasing",
        })
        self.message_post(
            body=self.env._(
                "Purchase order %(order)s created from approved quotation %(quote)s.",
                order=purchase_order.name,
                quote=quote.name,
            )
        )
        return {
            "type": "ir.actions.act_window",
            "res_model": "purchase.order",
            "res_id": purchase_order.id,
            "view_mode": "form",
        }


class RestaurantPurchaseQuote(models.Model):
    _name = "restaurant.purchase.quote"
    _description = "Restaurant Vendor Quotation"
    _inherit = ["mail.thread", "mail.activity.mixin"]
    _order = "quote_date desc, id desc"

    name = fields.Char(
        string="Quotation Reference",
        required=True,
        default="New",
        tracking=True,
    )
    request_id = fields.Many2one(
        "restaurant.stock.request",
        required=True,
        ondelete="cascade",
        index=True,
        tracking=True,
    )
    company_id = fields.Many2one(
        related="request_id.company_id",
        store=True,
        readonly=True,
        index=True,
    )
    vendor_id = fields.Many2one(
        "res.partner",
        required=True,
        ondelete="restrict",
        domain=[("supplier_rank", ">", 0)],
        tracking=True,
    )
    quote_date = fields.Date(
        required=True,
        default=fields.Date.context_today,
        tracking=True,
    )
    valid_until = fields.Date(tracking=True)
    currency_id = fields.Many2one(
        "res.currency",
        required=True,
        default=lambda self: self.env.company.currency_id,
    )
    line_ids = fields.One2many(
        "restaurant.purchase.quote.line",
        "quote_id",
        string="Quoted Items",
        copy=True,
    )
    amount_total = fields.Monetary(
        compute="_compute_amounts",
        store=True,
        currency_field="currency_id",
    )
    amount_company_currency = fields.Monetary(
        string="Amount in Company Currency",
        compute="_compute_amounts",
        store=True,
        currency_field="company_currency_id",
    )
    company_currency_id = fields.Many2one(
        related="company_id.currency_id",
        string="Company Currency",
        readonly=True,
    )
    attachment = fields.Binary(string="Vendor Quotation", attachment=True)
    attachment_filename = fields.Char()
    state = fields.Selection(
        [("draft", "Draft"), ("ready", "Ready")],
        default="draft",
        required=True,
        readonly=True,
        tracking=True,
    )
    is_selected = fields.Boolean(compute="_compute_is_selected")

    @api.depends("line_ids.subtotal", "currency_id", "quote_date", "company_id")
    def _compute_amounts(self):
        for quote in self:
            total = sum(quote.line_ids.mapped("subtotal"))
            quote.amount_total = total
            if quote.currency_id and quote.company_id:
                quote.amount_company_currency = quote.currency_id._convert(
                    total,
                    quote.company_id.currency_id,
                    quote.company_id,
                    quote.quote_date or fields.Date.context_today(quote),
                )
            else:
                quote.amount_company_currency = total

    @api.depends("request_id.selected_quote_id")
    def _compute_is_selected(self):
        for quote in self:
            quote.is_selected = quote.request_id.selected_quote_id == quote

    @api.model_create_multi
    def create(self, vals_list):
        require_role(self.env, PURCHASING_GROUP)
        prepared = []
        for vals in vals_list:
            values = dict(vals)
            request = self.env["restaurant.stock.request"].browse(values.get("request_id"))
            if not request or request.state != "purchase_required":
                raise ValidationError(
                    self.env._("Quotations can only be added while Purchasing is preparing the request.")
                )
            if not values.get("line_ids"):
                values["line_ids"] = [
                    Command.create({
                        "request_line_id": line.id,
                        "quantity": line.purchase_qty,
                        "uom_id": line.uom_id.id,
                    })
                    for line in request.line_ids
                    if not line.uom_id.is_zero(line.purchase_qty)
                ]
            prepared.append(values)
        return super().create(prepared)

    def write(self, vals):
        require_role(self.env, PURCHASING_GROUP)
        if any(quote.request_id.state != "purchase_required" for quote in self):
            raise AccessError(
                self.env._("Approved or submitted quotations cannot be edited.")
            )
        if "request_id" in vals:
            raise AccessError(self.env._("A quotation cannot be moved to another request."))
        return super().write(vals)

    def unlink(self):
        require_role(self.env, PURCHASING_GROUP)
        if any(quote.request_id.state != "purchase_required" for quote in self):
            raise AccessError(
                self.env._("Approved or submitted quotations cannot be deleted.")
            )
        if any(quote.is_selected for quote in self):
            raise ValidationError(self.env._("Unselect the quotation before deleting it."))
        return super().unlink()

    def action_mark_ready(self):
        for quote in self:
            require_role(quote.env, PURCHASING_GROUP)
            if quote.request_id.state != "purchase_required":
                raise UserError(self.env._("This purchase request is no longer editable."))
            if not quote.attachment:
                raise ValidationError(self.env._("Upload the vendor quotation first."))
            expected = quote.request_id.line_ids.filtered(
                lambda line: not line.uom_id.is_zero(line.purchase_qty)
            )
            if set(quote.line_ids.mapped("request_line_id").ids) != set(expected.ids):
                raise ValidationError(
                    self.env._("The quotation must include every item that must be purchased.")
                )
            if any(line.price_unit <= 0 or line.quantity <= 0 for line in quote.line_ids):
                raise ValidationError(
                    self.env._("Every quoted item needs a positive quantity and unit price.")
                )
            quote.write({"state": "ready"})
        return True

    def action_reset_draft(self):
        for quote in self:
            require_role(quote.env, PURCHASING_GROUP)
            if quote.request_id.state != "purchase_required":
                raise UserError(self.env._("This purchase request is no longer editable."))
            quote.write({"state": "draft"})
        return True

    def action_select(self):
        self.ensure_one()
        require_role(self.env, PURCHASING_GROUP)
        if self.state != "ready":
            raise ValidationError(self.env._("Mark the quotation Ready before selecting it."))
        if self.request_id.state != "purchase_required":
            raise UserError(self.env._("This purchase request is no longer editable."))
        self.request_id.with_context(**{PURCHASE_APPROVAL_CONTEXT: True}).write({
            "selected_quote_id": self.id,
            "vendor_id": self.vendor_id.id,
        })
        return {"type": "ir.actions.act_window_close"}


class RestaurantPurchaseQuoteLine(models.Model):
    _name = "restaurant.purchase.quote.line"
    _description = "Restaurant Vendor Quotation Line"
    _order = "id"

    quote_id = fields.Many2one(
        "restaurant.purchase.quote",
        required=True,
        ondelete="cascade",
        index=True,
    )
    request_line_id = fields.Many2one(
        "restaurant.stock.request.line",
        required=True,
        ondelete="restrict",
    )
    product_id = fields.Many2one(
        related="request_line_id.product_id",
        store=True,
        readonly=True,
    )
    quantity = fields.Float(required=True)
    uom_id = fields.Many2one("uom.uom", required=True)
    price_unit = fields.Monetary(
        required=True,
        default=0.0,
        currency_field="currency_id",
    )
    currency_id = fields.Many2one(
        related="quote_id.currency_id",
        readonly=True,
    )
    subtotal = fields.Monetary(
        compute="_compute_subtotal",
        store=True,
        currency_field="currency_id",
    )
    company_id = fields.Many2one(
        related="quote_id.company_id",
        store=True,
        readonly=True,
        index=True,
    )

    @api.depends("quantity", "price_unit")
    def _compute_subtotal(self):
        for line in self:
            line.subtotal = line.quantity * line.price_unit

    @api.model_create_multi
    def create(self, vals_list):
        require_role(self.env, PURCHASING_GROUP)
        quotes = self.env["restaurant.purchase.quote"].browse(
            [values.get("quote_id") for values in vals_list if values.get("quote_id")]
        )
        if any(
            quote.request_id.state != "purchase_required" or quote.state != "draft"
            for quote in quotes
        ):
            raise AccessError(
                self.env._("Quoted items can only be added to a draft quotation.")
            )
        return super().create(vals_list)

    def write(self, vals):
        require_role(self.env, PURCHASING_GROUP)
        if any(
            line.quote_id.request_id.state != "purchase_required"
            or line.quote_id.state != "draft"
            for line in self
        ):
            raise AccessError(
                self.env._("Ready, submitted, or approved quotation lines cannot be edited.")
            )
        if set(vals) & {"quote_id", "request_line_id", "product_id"}:
            raise AccessError(
                self.env._("Quotation item links cannot be changed manually.")
            )
        return super().write(vals)

    def unlink(self):
        require_role(self.env, PURCHASING_GROUP)
        if any(
            line.quote_id.request_id.state != "purchase_required"
            or line.quote_id.state != "draft"
            for line in self
        ):
            raise AccessError(
                self.env._("Ready, submitted, or approved quotation lines cannot be deleted.")
            )
        return super().unlink()

    @api.constrains("request_line_id", "quote_id")
    def _check_request_line(self):
        for line in self:
            if line.request_line_id.request_id != line.quote_id.request_id:
                raise ValidationError(
                    self.env._("Quoted items must belong to the same stock request.")
                )

    @api.constrains("quantity")
    def _check_quantity(self):
        for line in self:
            if line.quantity <= 0:
                raise ValidationError(self.env._("Quoted quantity must be positive."))

            requested = line.request_line_id.purchase_qty
            if line.uom_id.compare(line.quantity, requested) != 0:
                raise ValidationError(
                    self.env._("Quoted quantity must match the quantity required for purchase.")
                )

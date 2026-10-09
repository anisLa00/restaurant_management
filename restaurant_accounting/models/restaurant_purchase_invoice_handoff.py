from odoo import Command, api, fields, models
from odoo.exceptions import AccessError, UserError, ValidationError
from odoo.tools import float_compare, float_is_zero


PURCHASING_GROUP = "restaurant_core.group_restaurant_purchasing"
ACCOUNTANT_GROUP = "restaurant_core.group_restaurant_accountant"
OPERATIONS_GROUP = "restaurant_core.group_restaurant_operations_manager"
OWNER_GROUP = "restaurant_core.group_restaurant_owner"
SYSTEM_GROUP = "base.group_system"
WORKFLOW_CONTEXT = "restaurant_invoice_handoff_workflow"
LINE_CONTEXT = "restaurant_invoice_handoff_lines"


def require_group(env, group):
    if not env.su and not env.user.has_group(group):
        raise AccessError(env._("Your role does not allow this operation."))


class RestaurantPurchaseInvoiceHandoff(models.Model):
    _name = "restaurant.purchase.invoice.handoff"
    _description = "Restaurant Supplier Invoice Handoff"
    _inherit = ["mail.thread", "mail.activity.mixin"]
    _order = "invoice_date desc, id desc"
    _mail_post_access = "read"

    name = fields.Char(
        string="Handoff Reference", required=True, readonly=True, copy=False,
        default="New", index=True,
    )
    purchase_order_id = fields.Many2one(
        "purchase.order", required=True, ondelete="restrict", index=True,
        check_company=True, tracking=True,
    )
    company_id = fields.Many2one(
        related="purchase_order_id.company_id", store=True, readonly=True, index=True,
    )
    currency_id = fields.Many2one(
        related="purchase_order_id.currency_id", store=True, readonly=True,
    )
    vendor_id = fields.Many2one(
        related="purchase_order_id.partner_id", store=True, readonly=True, index=True,
    )
    invoice_reference = fields.Char(required=True, tracking=True, index=True)
    invoice_date = fields.Date(required=True, default=fields.Date.context_today, tracking=True)
    vendor_invoice_file = fields.Binary(
        string="Supplier Invoice", required=True, attachment=True,
    )
    vendor_invoice_filename = fields.Char()
    line_ids = fields.One2many(
        "restaurant.purchase.invoice.handoff.line", "handoff_id",
        string="Invoice Lines", copy=False,
    )
    state = fields.Selection(
        [
            ("draft", "Draft by Purchasing"),
            ("awaiting_operations", "Variance: Awaiting Operations"),
            ("awaiting_owner", "Variance: Awaiting Owner"),
            ("awaiting_accounting", "Awaiting Accounting"),
            ("bill_draft", "Vendor Bill Draft"),
            ("posted", "Vendor Bill Posted"),
            ("paid", "Paid"),
            ("cancelled", "Cancelled"),
        ],
        required=True, default="draft", readonly=True, copy=False,
        tracking=True, index=True,
    )
    match_state = fields.Selection(
        [
            ("pending", "Pending"),
            ("pending_receipt", "Receipt Incomplete"),
            ("quantity_variance", "Quantity Variance"),
            ("price_variance", "Price Variance"),
            ("matched", "Matched"),
        ],
        compute="_compute_match", store=True, readonly=True, index=True,
    )
    purchase_total = fields.Monetary(
        compute="_compute_match", store=True, currency_field="currency_id",
    )
    invoice_total = fields.Monetary(
        compute="_compute_match", store=True, currency_field="currency_id",
    )
    variance_amount = fields.Monetary(
        compute="_compute_match", store=True, currency_field="currency_id",
    )
    purchasing_note = fields.Text(tracking=True)
    exception_reason = fields.Text(tracking=True)
    accounting_note = fields.Text(tracking=True)
    submitted_by_id = fields.Many2one("res.users", readonly=True, copy=False)
    submitted_at = fields.Datetime(readonly=True, copy=False)
    exception_approved_by_id = fields.Many2one("res.users", readonly=True, copy=False)
    exception_approved_at = fields.Datetime(readonly=True, copy=False)
    vendor_bill_id = fields.Many2one(
        "account.move", readonly=True, copy=False, check_company=True,
        domain="[('move_type', '=', 'in_invoice')]",
    )
    bill_posted_by_id = fields.Many2one("res.users", readonly=True, copy=False)
    bill_posted_at = fields.Datetime(readonly=True, copy=False)
    payment_state = fields.Selection(related="vendor_bill_id.payment_state", readonly=True)

    _vendor_invoice_unique = models.Constraint(
        "UNIQUE(company_id, vendor_id, invoice_reference)",
        "This supplier invoice reference is already registered for this vendor.",
    )

    @api.depends(
        "line_ids.purchase_line_id.qty_to_invoice",
        "line_ids.purchase_line_id.qty_received",
        "line_ids.purchase_line_id.price_unit",
        "line_ids.invoice_quantity",
        "line_ids.invoice_unit_price",
        "line_ids.product_id.type",
        "currency_id",
    )
    def _compute_match(self):
        precision = self.env["decimal.precision"].precision_get("Product Unit")
        for handoff in self:
            handoff.purchase_total = sum(
                line.expected_quantity * line.purchase_unit_price
                for line in handoff.line_ids
            )
            handoff.invoice_total = sum(line.subtotal for line in handoff.line_ids)
            handoff.variance_amount = handoff.invoice_total - handoff.purchase_total
            if not handoff.line_ids:
                handoff.match_state = "pending"
                continue
            receipt_incomplete = any(
                line.product_id.type != "service"
                and float_compare(
                    line.received_quantity,
                    line.invoice_quantity,
                    precision_digits=precision,
                ) < 0
                for line in handoff.line_ids
            )
            quantity_variance = any(
                float_compare(
                    line.invoice_quantity,
                    line.expected_quantity,
                    precision_digits=precision,
                ) != 0
                for line in handoff.line_ids
            )
            price_variance = any(
                not float_is_zero(
                    line.invoice_unit_price - line.purchase_unit_price,
                    precision_rounding=handoff.currency_id.rounding,
                )
                for line in handoff.line_ids
            ) if handoff.currency_id else False
            if receipt_incomplete:
                handoff.match_state = "pending_receipt"
            elif quantity_variance:
                handoff.match_state = "quantity_variance"
            elif price_variance:
                handoff.match_state = "price_variance"
            else:
                handoff.match_state = "matched"

    @api.model_create_multi
    def create(self, vals_list):
        require_group(self.env, PURCHASING_GROUP)
        prepared = []
        for vals in vals_list:
            values = dict(vals)
            if values.get("state", "draft") != "draft":
                raise AccessError(self.env._("Supplier invoices must start in Draft."))
            purchase_order = self.env["purchase.order"].browse(values.get("purchase_order_id"))
            if not purchase_order or purchase_order.state not in ("purchase", "done"):
                raise ValidationError(self.env._("Select a confirmed purchase order."))
            if purchase_order.company_id not in self.env.companies:
                raise AccessError(self.env._("Switch to the purchase order company first."))
            values.update({
                "name": self.env["ir.sequence"].next_by_code(
                    "restaurant.purchase.invoice.handoff"
                ) or "New",
                "state": "draft",
            })
            if not values.get("line_ids"):
                billable_lines = purchase_order.order_line.filtered(
                    lambda line: not line.display_type and line.qty_to_invoice > 0
                )
                if not billable_lines:
                    raise ValidationError(self.env._("This purchase order has no quantity to invoice."))
                values["line_ids"] = [Command.create({
                    "purchase_line_id": line.id,
                    "invoice_quantity": line.qty_to_invoice,
                    "invoice_unit_price": line.price_unit,
                }) for line in billable_lines]
            prepared.append(values)
        return super().create(prepared)

    def write(self, vals):
        if self.env.context.get(WORKFLOW_CONTEXT):
            return super().write(vals)
        self.check_access("write")
        if "state" in vals:
            raise AccessError(self.env._("Use the workflow buttons to change status."))
        if all(record.state == "draft" for record in self):
            require_group(self.env, PURCHASING_GROUP)
            editable = {
                "purchase_order_id", "invoice_reference", "invoice_date",
                "vendor_invoice_file", "vendor_invoice_filename", "line_ids",
                "purchasing_note",
            }
            if set(vals) - editable:
                raise AccessError(self.env._("Only draft supplier invoice details can be edited."))
            if "purchase_order_id" in vals:
                raise AccessError(self.env._("Create a new handoff for a different purchase order."))
            return super().write(vals)
        if (
            set(vals) <= {"exception_reason"}
            and all(record.state in ("awaiting_operations", "awaiting_owner") for record in self)
        ):
            for record in self:
                require_group(
                    self.env,
                    OPERATIONS_GROUP if record.state == "awaiting_operations" else OWNER_GROUP,
                )
            return super().write(vals)
        if set(vals) <= {"accounting_note"} and all(
            record.state in ("awaiting_accounting", "bill_draft", "posted", "paid")
            for record in self
        ):
            require_group(self.env, ACCOUNTANT_GROUP)
            return super().write(vals)
        raise AccessError(self.env._("This supplier invoice handoff is locked."))

    def unlink(self):
        require_group(self.env, PURCHASING_GROUP)
        if any(record.state != "draft" for record in self):
            raise AccessError(self.env._("Only draft handoffs can be deleted."))
        return super().unlink()

    def _workflow_write(self, vals):
        return self.with_context(**{WORKFLOW_CONTEXT: True}).write(vals)

    def action_submit(self):
        self.ensure_one()
        require_group(self.env, PURCHASING_GROUP)
        self.check_access("write")
        self.lock_for_update()
        if self.state != "draft":
            raise UserError(self.env._("Only draft handoffs can be submitted."))
        if not self.vendor_invoice_file or not self.invoice_reference or not self.invoice_date:
            raise ValidationError(self.env._("Enter the supplier invoice reference, date, and file."))
        if not self.line_ids or any(
            line.invoice_quantity <= 0 or line.invoice_unit_price < 0
            for line in self.line_ids
        ):
            raise ValidationError(self.env._("Enter valid supplier invoice quantities and prices."))
        if self.match_state == "pending_receipt":
            raise ValidationError(self.env._("Complete the supplier receipt before invoice handoff."))
        if self.match_state == "matched":
            target = "awaiting_accounting"
        else:
            threshold = self.company_id.restaurant_owner_purchase_approval_threshold
            target = (
                "awaiting_owner"
                if threshold >= 0 and abs(self.variance_amount) > threshold
                else "awaiting_operations"
            )
        self._workflow_write({
            "state": target,
            "submitted_by_id": self.env.uid,
            "submitted_at": fields.Datetime.now(),
            "exception_approved_by_id": False,
            "exception_approved_at": False,
        })
        return True

    def action_approve_exception(self):
        self.ensure_one()
        self.check_access("write")
        self.lock_for_update()
        if self.state == "awaiting_operations":
            require_group(self.env, OPERATIONS_GROUP)
        elif self.state == "awaiting_owner":
            require_group(self.env, OWNER_GROUP)
        else:
            raise UserError(self.env._("This handoff is not awaiting variance approval."))
        if not (self.exception_reason or "").strip():
            raise ValidationError(self.env._("Document why this invoice variance is accepted."))
        self._workflow_write({
            "state": "awaiting_accounting",
            "exception_approved_by_id": self.env.uid,
            "exception_approved_at": fields.Datetime.now(),
        })
        return True

    def action_return_to_purchasing(self):
        self.ensure_one()
        self.check_access("write")
        self.lock_for_update()
        if self.state == "awaiting_operations":
            require_group(self.env, OPERATIONS_GROUP)
        elif self.state == "awaiting_owner":
            require_group(self.env, OWNER_GROUP)
        elif self.state == "awaiting_accounting":
            require_group(self.env, ACCOUNTANT_GROUP)
        else:
            raise UserError(self.env._("This handoff cannot be returned now."))
        reason = self.exception_reason if self.state != "awaiting_accounting" else self.accounting_note
        if not (reason or "").strip():
            raise ValidationError(self.env._("Enter a return reason first."))
        self._workflow_write({
            "state": "draft",
            "exception_approved_by_id": False,
            "exception_approved_at": False,
        })
        return True

    def action_create_vendor_bill(self):
        self.ensure_one()
        require_group(self.env, ACCOUNTANT_GROUP)
        self.check_access("write")
        self.lock_for_update()
        if self.state != "awaiting_accounting":
            raise UserError(self.env._("This supplier invoice is not awaiting Accounting."))
        if self.vendor_bill_id:
            raise UserError(self.env._("A vendor bill already exists for this handoff."))
        purchase_order = self.purchase_order_id.with_user(self.env.user)
        before = purchase_order.invoice_ids
        purchase_order.action_create_invoice()
        purchase_order.invalidate_recordset(["invoice_ids"])
        bill = (purchase_order.invoice_ids - before).filtered(
            lambda move: move.move_type == "in_invoice" and move.state == "draft"
        )
        if len(bill) != 1:
            raise ValidationError(self.env._("Accounting could not create one draft vendor bill."))
        bill_lines = bill.invoice_line_ids.filtered(lambda line: line.purchase_line_id)
        handoff_by_po_line = {
            line.purchase_line_id.id: line for line in self.line_ids
        }
        for bill_line in bill_lines:
            handoff_line = handoff_by_po_line.get(bill_line.purchase_line_id.id)
            if not handoff_line:
                bill_line.unlink()
                continue
            bill_line.write({
                "quantity": handoff_line.invoice_quantity,
                "price_unit": handoff_line.invoice_unit_price,
            })
        attachment = self.env["ir.attachment"].create({
            "name": self.vendor_invoice_filename or f"{self.invoice_reference}.pdf",
            "raw": self.vendor_invoice_file,
            "res_model": "account.move",
            "res_id": bill.id,
        })
        bill.write({
            "ref": self.invoice_reference,
            "invoice_date": self.invoice_date,
            "restaurant_purchase_handoff_id": self.id,
        })
        bill.message_post(
            body=self.env._("Supplier invoice received from Purchasing handoff %s.", self.name),
            attachment_ids=attachment.ids,
        )
        self._workflow_write({
            "vendor_bill_id": bill.id,
            "state": "bill_draft",
        })
        return {
            "type": "ir.actions.act_window",
            "res_model": "account.move",
            "res_id": bill.id,
            "view_mode": "form",
        }

    def action_post_vendor_bill(self):
        self.ensure_one()
        require_group(self.env, ACCOUNTANT_GROUP)
        self.check_access("write")
        self.lock_for_update()
        if self.state != "bill_draft" or not self.vendor_bill_id:
            raise UserError(self.env._("Create the draft vendor bill first."))
        if self.vendor_bill_id.state != "draft":
            raise ValidationError(self.env._("The linked vendor bill is no longer in Draft."))
        self.vendor_bill_id.action_post()
        self._workflow_write({
            "state": "posted",
            "bill_posted_by_id": self.env.uid,
            "bill_posted_at": fields.Datetime.now(),
        })
        return True

    def action_refresh_payment_status(self):
        require_group(self.env, ACCOUNTANT_GROUP)
        for handoff in self:
            if handoff.state not in ("posted", "paid") or not handoff.vendor_bill_id:
                continue
            target = "paid" if handoff.vendor_bill_id.payment_state == "paid" else "posted"
            if handoff.state != target:
                handoff._workflow_write({"state": target})
        return True


class RestaurantPurchaseInvoiceHandoffLine(models.Model):
    _name = "restaurant.purchase.invoice.handoff.line"
    _description = "Restaurant Supplier Invoice Handoff Line"
    _order = "id"

    handoff_id = fields.Many2one(
        "restaurant.purchase.invoice.handoff", required=True,
        ondelete="cascade", index=True,
    )
    purchase_line_id = fields.Many2one(
        "purchase.order.line", required=True, ondelete="restrict", index=True,
    )
    company_id = fields.Many2one(
        related="handoff_id.company_id", store=True, readonly=True, index=True,
    )
    currency_id = fields.Many2one(related="handoff_id.currency_id", readonly=True)
    product_id = fields.Many2one(related="purchase_line_id.product_id", readonly=True)
    description = fields.Text(related="purchase_line_id.name", readonly=True)
    ordered_quantity = fields.Float(related="purchase_line_id.product_qty", readonly=True)
    received_quantity = fields.Float(related="purchase_line_id.qty_received", readonly=True)
    expected_quantity = fields.Float(related="purchase_line_id.qty_to_invoice", readonly=True)
    purchase_unit_price = fields.Float(
        related="purchase_line_id.price_unit", digits="Product Price", readonly=True,
    )
    invoice_quantity = fields.Float(required=True)
    invoice_unit_price = fields.Monetary(required=True, currency_field="currency_id")
    subtotal = fields.Monetary(
        compute="_compute_subtotal", store=True, currency_field="currency_id",
    )

    @api.depends("invoice_quantity", "invoice_unit_price")
    def _compute_subtotal(self):
        for line in self:
            line.subtotal = line.invoice_quantity * line.invoice_unit_price

    @api.model_create_multi
    def create(self, vals_list):
        if not self.env.context.get(LINE_CONTEXT):
            require_group(self.env, PURCHASING_GROUP)
        handoffs = self.env["restaurant.purchase.invoice.handoff"].browse(
            [vals.get("handoff_id") for vals in vals_list if vals.get("handoff_id")]
        )
        if any(handoff.state != "draft" for handoff in handoffs):
            raise AccessError(self.env._("Invoice lines require a draft handoff."))
        records = super().create(vals_list)
        records._check_purchase_order_links()
        return records

    def write(self, vals):
        require_group(self.env, PURCHASING_GROUP)
        if any(line.handoff_id.state != "draft" for line in self):
            raise AccessError(self.env._("Submitted supplier invoice lines are locked."))
        if set(vals) - {"invoice_quantity", "invoice_unit_price"}:
            raise AccessError(self.env._("Only supplier invoice quantity and price can be edited."))
        return super().write(vals)

    def unlink(self):
        require_group(self.env, PURCHASING_GROUP)
        if any(line.handoff_id.state != "draft" for line in self):
            raise AccessError(self.env._("Submitted supplier invoice lines are locked."))
        return super().unlink()

    @api.constrains("purchase_line_id", "handoff_id", "invoice_quantity", "invoice_unit_price")
    def _check_purchase_order_links(self):
        for line in self:
            if line.purchase_line_id.order_id != line.handoff_id.purchase_order_id:
                raise ValidationError(self.env._("Invoice lines must belong to the selected purchase order."))
            if line.invoice_quantity <= 0 or line.invoice_unit_price < 0:
                raise ValidationError(self.env._("Invoice quantities must be positive and prices non-negative."))


class PurchaseOrder(models.Model):
    _inherit = "purchase.order"

    restaurant_invoice_handoff_ids = fields.One2many(
        "restaurant.purchase.invoice.handoff",
        "purchase_order_id",
        string="Restaurant Supplier Invoices",
        readonly=True,
    )
    restaurant_invoice_handoff_count = fields.Integer(
        compute="_compute_restaurant_invoice_handoff_count",
    )

    @api.depends("restaurant_invoice_handoff_ids")
    def _compute_restaurant_invoice_handoff_count(self):
        grouped = self.env["restaurant.purchase.invoice.handoff"].sudo()._read_group(
            [("purchase_order_id", "in", self.ids)],
            ["purchase_order_id"],
            ["__count"],
        ) if self.ids else []
        counts = {order.id: count for order, count in grouped}
        for order in self:
            order.restaurant_invoice_handoff_count = counts.get(order.id, 0)

    def action_open_restaurant_invoice_handoffs(self):
        self.ensure_one()
        return {
            "type": "ir.actions.act_window",
            "name": self.env._("Supplier Invoice Handoffs"),
            "res_model": "restaurant.purchase.invoice.handoff",
            "view_mode": "list,form",
            "domain": [("purchase_order_id", "=", self.id)],
            "context": {"default_purchase_order_id": self.id},
        }

class AccountMove(models.Model):
    _inherit = "account.move"

    restaurant_purchase_handoff_id = fields.Many2one(
        "restaurant.purchase.invoice.handoff",
        string="Restaurant Supplier Invoice Handoff",
        readonly=True,
        copy=False,
        index=True,
    )

from odoo import api, fields, models
from odoo.exceptions import AccessError, UserError, ValidationError

from odoo.addons.restaurant_stock.models.stock_security import (
    get_company_business_date,
)

from .restaurant_stock_event import (
    MANAGER_GROUP,
    RECEPTION_GROUP,
    STOCKKEEPER_GROUP,
    SYSTEM_EVENT_WRITE_CONTEXT,
    require_assigned_branches,
    require_role,
)


SYSTEM_SHEET_WRITE_CONTEXT = "restaurant_stock_event_sheet_system_write"
SHEET_MIGRATION_CONTEXT = "restaurant_stock_event_sheet_migration"


class RestaurantStockEventSheet(models.Model):
    _name = "restaurant.stock.event.sheet"
    _description = "Restaurant Cancellation / Complimentary Sheet"
    _inherit = ["mail.thread", "mail.activity.mixin"]
    _order = "service_date desc, id desc"
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
        default=lambda self: self.env.user.default_restaurant_branch_id,
        tracking=True,
        index=True,
    )
    company_id = fields.Many2one(
        related="branch_id.company_id",
        store=True,
        readonly=True,
        index=True,
    )
    service_date = fields.Date(
        required=True,
        default=lambda self: get_company_business_date(self.env.company),
        tracking=True,
        index=True,
    )
    order_reference = fields.Char(
        help="POS, receipt, or customer order reference shared by every item in the sheet.",
        tracking=True,
        index=True,
    )
    receipt_proof_ids = fields.Many2many(
        "ir.attachment",
        "restaurant_stock_event_sheet_receipt_rel",
        "sheet_id",
        "attachment_id",
        string="Receipt Proof",
        copy=False,
        help="Receipt or order proof shared by every item in this sheet.",
    )
    closing_id = fields.Many2one(
        "restaurant.daily.closing",
        string="Reception Daily Closing",
        copy=False,
        ondelete="restrict",
        tracking=True,
        index=True,
    )
    line_ids = fields.One2many(
        "restaurant.stock.event",
        "sheet_id",
        string="Cancellation / Complimentary Items",
        copy=True,
    )
    currency_id = fields.Many2one(
        related="company_id.currency_id",
        store=True,
        readonly=True,
    )
    total_reported_amount = fields.Monetary(
        compute="_compute_totals",
        store=True,
        string="Total Reported Amount",
    )
    item_count = fields.Integer(compute="_compute_totals", store=True)
    cancellation_count = fields.Integer(compute="_compute_totals", store=True)
    complimentary_count = fields.Integer(compute="_compute_totals", store=True)
    notes = fields.Text(tracking=True)
    kitchen_note = fields.Text(tracking=True)
    manager_note = fields.Text(tracking=True)
    state = fields.Selection(
        [
            ("draft", "Draft"),
            ("stockkeeper_review", "Stockkeeper Review"),
            ("manager_review", "Manager Review"),
            ("approved", "Approved"),
            ("rejected", "Rejected"),
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
        required=True,
        readonly=True,
        default=lambda self: self.env.user,
    )
    stockkeeper_id = fields.Many2one(
        "res.users",
        string="Stockkeeper Reviewed By",
        readonly=True,
        copy=False,
    )
    stockkeeper_reviewed_at = fields.Datetime(readonly=True, copy=False)
    manager_id = fields.Many2one(
        "res.users",
        string="Manager Reviewed By",
        readonly=True,
        copy=False,
    )
    manager_reviewed_at = fields.Datetime(readonly=True, copy=False)

    @api.depends("line_ids.event_type", "line_ids.reported_amount")
    def _compute_totals(self):
        for sheet in self:
            sheet.item_count = len(sheet.line_ids)
            sheet.cancellation_count = len(
                sheet.line_ids.filtered(lambda line: line.event_type == "cancellation")
            )
            sheet.complimentary_count = len(
                sheet.line_ids.filtered(lambda line: line.event_type == "complimentary")
            )
            sheet.total_reported_amount = sum(
                sheet.line_ids.mapped("reported_amount")
            )

    @api.constrains("branch_id", "service_date", "closing_id")
    def _check_closing_matches_sheet(self):
        for sheet in self.filtered("closing_id"):
            closing = sheet.closing_id
            if (
                closing.branch_id != sheet.branch_id
                or closing.company_id != sheet.company_id
                or closing.closing_date != sheet.service_date
            ):
                raise ValidationError(self.env._(
                    "The Reception Closing must match the sheet branch, company, "
                    "and business date."
                ))
            if closing.state == "cancelled":
                raise ValidationError(self.env._(
                    "A service-event sheet cannot use a cancelled Reception Closing."
                ))

    @api.model_create_multi
    def create(self, vals_list):
        if self.env.context.get(SHEET_MIGRATION_CONTEXT) and self.env.su:
            return super().create(vals_list)
        require_role(self.env, RECEPTION_GROUP)
        protected = {
            "state",
            "reception_user_id",
            "stockkeeper_id",
            "stockkeeper_reviewed_at",
            "manager_id",
            "manager_reviewed_at",
            "company_id",
            "currency_id",
        }
        prepared = []
        for values in vals_list:
            values = dict(values)
            if set(values) & protected:
                raise AccessError(self.env._(
                    "Workflow and review fields are system-controlled."
                ))
            values.update({
                "name": self.env["ir.sequence"].next_by_code(
                    "restaurant.stock.event.sheet"
                ) or "New",
                "state": "draft",
                "reception_user_id": self.env.uid,
            })
            prepared.append(values)
        records = super().create(prepared)
        records._bind_receipt_attachments()
        require_assigned_branches(records.branch_id)
        return records

    def write(self, values):
        if self.env.context.get(SYSTEM_SHEET_WRITE_CONTEXT) and self.env.su:
            return super().write(values)

        self.check_access("write")
        require_assigned_branches(self.branch_id)
        if "state" in values:
            raise AccessError(self.env._("Use the workflow buttons to change status."))

        if all(sheet.state == "draft" for sheet in self):
            require_role(self.env, RECEPTION_GROUP)
            allowed = {
                "branch_id",
                "service_date",
                "order_reference",
                "receipt_proof_ids",
                "closing_id",
                "line_ids",
                "notes",
            }
            if set(values) - allowed:
                raise AccessError(self.env._(
                    "Only Reception sheet details can be edited in Draft."
                ))
            if "branch_id" in values:
                require_assigned_branches(
                    self.env["restaurant.branch"].browse(values["branch_id"])
                )
            result = super().write(values)
            if "receipt_proof_ids" in values:
                self._bind_receipt_attachments()
            shared = {
                key: values[key]
                for key in (
                    "branch_id",
                    "service_date",
                    "order_reference",
                    "closing_id",
                )
                if key in values
            }
            if shared:
                self.line_ids.sudo().with_context(
                    **{SYSTEM_EVENT_WRITE_CONTEXT: True}
                ).write(shared)
            return result

        if all(sheet.state == "stockkeeper_review" for sheet in self):
            require_role(self.env, STOCKKEEPER_GROUP)
            if set(values) - {"kitchen_note"}:
                raise AccessError(self.env._(
                    "The Stockkeeper can only edit the sheet kitchen note."
                ))
            return super().write(values)

        if all(sheet.state == "manager_review" for sheet in self):
            require_role(self.env, MANAGER_GROUP)
            if set(values) - {"manager_note"}:
                raise AccessError(self.env._(
                    "The Manager can only edit the manager note."
                ))
            return super().write(values)

        raise AccessError(self.env._("Approved or closed sheets are immutable."))

    def _bind_receipt_attachments(self):
        """Anchor the shared receipt proof to its sheet for secure reviewers."""
        for sheet in self:
            unbound = sheet.receipt_proof_ids.sudo().filtered(
                lambda attachment: not attachment.res_model
                and not attachment.res_id
            )
            if unbound:
                unbound.write({
                    "res_model": self._name,
                    "res_id": sheet.id,
                })

    def unlink(self):
        self.check_access("unlink")
        require_role(self.env, RECEPTION_GROUP)
        require_assigned_branches(self.branch_id)
        if any(sheet.state != "draft" for sheet in self):
            raise AccessError(self.env._("Only Draft sheets can be deleted."))
        return super().unlink()

    def _workflow_write(self, values):
        return super(
            RestaurantStockEventSheet,
            self.sudo().with_context(**{SYSTEM_SHEET_WRITE_CONTEXT: True}),
        ).write(values)

    def action_submit(self):
        self.ensure_one()
        require_role(self.env, RECEPTION_GROUP)
        require_assigned_branches(self.branch_id)
        self.check_access("write")
        self.lock_for_update()
        if self.state != "draft":
            raise UserError(self.env._("Only a Draft sheet can be submitted."))
        if not (self.order_reference or "").strip():
            raise ValidationError(
                self.env._("Enter the Bill / Order Reference for this sheet.")
            )
        if not self.receipt_proof_ids:
            raise ValidationError(
                self.env._(
                    "Upload the receipt proof for this Cancellation / "
                    "Complimentary sheet."
                )
            )
        if not self.line_ids:
            raise ValidationError(self.env._("Add at least one item to the sheet."))
        for line in self.line_ids:
            line.with_user(self.env.user).action_submit_kitchen()
        target = (
            "stockkeeper_review"
            if self.line_ids.filtered(lambda line: line.event_type == "cancellation")
            else "manager_review"
        )
        self._workflow_write({"state": target})
        return True

    def action_mark_all_prepared(self):
        self.ensure_one()
        require_role(self.env, STOCKKEEPER_GROUP)
        require_assigned_branches(self.branch_id)
        self.check_access("write")
        if self.state != "stockkeeper_review":
            raise UserError(self.env._(
                "Only a sheet in Stockkeeper Review can be updated."
            ))
        pending = self.line_ids.filtered(
            lambda line: line.event_type == "cancellation"
            and line.state == "kitchen_review"
        )
        for line in pending:
            line.with_user(self.env.user).action_mark_prepared()
        return True

    def action_send_manager(self):
        self.ensure_one()
        require_role(self.env, STOCKKEEPER_GROUP)
        require_assigned_branches(self.branch_id)
        self.check_access("write")
        self.lock_for_update()
        if self.state != "stockkeeper_review":
            raise UserError(self.env._(
                "Only a sheet in Stockkeeper Review can be sent to Management."
            ))
        pending = self.line_ids.filtered(
            lambda line: line.event_type == "cancellation"
            and line.state == "kitchen_review"
        )
        if pending:
            raise ValidationError(self.env._(
                "Confirm Prepared or Not Prepared for every Cancellation item first."
            ))
        invalid = self.line_ids.filtered(
            lambda line: line.state != "manager_review"
        )
        if invalid:
            raise ValidationError(self.env._(
                "Every item must be ready for Manager Review."
            ))
        self._workflow_write({
            "state": "manager_review",
            "stockkeeper_id": self.env.uid,
            "stockkeeper_reviewed_at": fields.Datetime.now(),
        })
        return True

    def action_approve(self):
        self.ensure_one()
        require_role(self.env, MANAGER_GROUP)
        require_assigned_branches(self.branch_id)
        self.check_access("write")
        self.lock_for_update()
        if self.state != "manager_review":
            raise UserError(self.env._(
                "Only a sheet in Manager Review can be approved."
            ))
        for line in self.line_ids:
            line.with_user(self.env.user).action_approve()
        self._workflow_write({
            "state": "approved",
            "manager_id": self.env.uid,
            "manager_reviewed_at": fields.Datetime.now(),
        })
        return True

    def action_reject(self):
        self.ensure_one()
        require_role(self.env, MANAGER_GROUP)
        require_assigned_branches(self.branch_id)
        self.check_access("write")
        self.lock_for_update()
        if self.state != "manager_review":
            raise UserError(self.env._(
                "Only a sheet in Manager Review can be rejected."
            ))
        for line in self.line_ids:
            line.with_user(self.env.user).action_reject()
        self._workflow_write({
            "state": "rejected",
            "manager_id": self.env.uid,
            "manager_reviewed_at": fields.Datetime.now(),
        })
        return True

    def action_return_to_stockkeeper(self):
        self.ensure_one()
        require_role(self.env, MANAGER_GROUP)
        require_assigned_branches(self.branch_id)
        self.check_access("write")
        self.lock_for_update()
        if self.state != "manager_review":
            raise UserError(self.env._(
                "Only a sheet in Manager Review can be returned."
            ))
        cancellations = self.line_ids.filtered(
            lambda line: line.event_type == "cancellation"
            and line.state == "manager_review"
        )
        if not cancellations:
            raise ValidationError(self.env._(
                "This sheet has no Cancellation items for Stockkeeper review."
            ))
        for line in cancellations:
            line.with_user(self.env.user).action_return_to_kitchen()
        self._workflow_write({
            "state": "stockkeeper_review",
            "stockkeeper_id": False,
            "stockkeeper_reviewed_at": False,
        })
        return True

    def action_cancel(self):
        self.ensure_one()
        require_role(self.env, RECEPTION_GROUP)
        require_assigned_branches(self.branch_id)
        self.check_access("write")
        self.lock_for_update()
        if self.state != "draft":
            raise UserError(self.env._("Only a Draft sheet can be cancelled."))
        for line in self.line_ids:
            line.with_user(self.env.user).action_cancel()
        self._workflow_write({"state": "cancelled"})
        return True

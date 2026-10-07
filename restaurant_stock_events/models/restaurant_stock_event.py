from odoo import api, fields, models, tools
from odoo.exceptions import AccessError, UserError, ValidationError

from odoo.addons.restaurant_stock.models.stock_security import (
    get_company_business_date,
)


RECEPTION_GROUP = "restaurant_core.group_restaurant_reception"
STOCKKEEPER_GROUP = "restaurant_core.group_restaurant_stockkeeper"
MANAGER_GROUP = "restaurant_core.group_restaurant_branch_manager"
OPERATIONS_GROUP = "restaurant_core.group_restaurant_operations_manager"
OWNER_GROUP = "restaurant_core.group_restaurant_owner"
SYSTEM_EVENT_WRITE_CONTEXT = "restaurant_stock_event_system_write"


def require_role(env, group):
    if not env.su and not env.user.has_group(group):
        raise AccessError(env._("Your role does not allow this operation."))


def require_assigned_branches(branches):
    if not branches:
        return

    branches.check_access("read")
    if branches.company_id - branches.env.companies:
        raise AccessError(
            branches.env._("Select an authorized company for this branch.")
        )

    has_global_access = (
        branches.env.su
        or branches.env.user.has_group(OPERATIONS_GROUP)
        or branches.env.user.has_group(OWNER_GROUP)
    )
    if not has_global_access:
        assigned = branches.sudo().search([
            ("id", "in", branches.ids),
            ("user_ids", "in", [branches.env.uid]),
        ])
        if branches - assigned:
            raise AccessError(
                branches.env._(
                    "You are not assigned to this restaurant branch."
                )
            )


class RestaurantStockEvent(models.Model):
    _name = "restaurant.stock.event"
    _description = "Restaurant Cancellation / Complimentary Event"
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
    event_type = fields.Selection(
        [
            ("cancellation", "Cancellation"),
            ("complimentary", "Complimentary"),
        ],
        required=True,
        default="cancellation",
        tracking=True,
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
        help="Optional POS, receipt, or customer order reference.",
        index=True,
    )
    category_id = fields.Many2one(
        "restaurant.menu.category",
        required=True,
        ondelete="restrict",
        tracking=True,
    )
    menu_item_id = fields.Many2one(
        "restaurant.menu.item",
        required=True,
        ondelete="restrict",
        tracking=True,
    )
    variant_id = fields.Many2one(
        "restaurant.menu.variant",
        string="Menu Item / Size",
        required=True,
        ondelete="restrict",
        tracking=True,
    )
    size = fields.Selection(
        related="variant_id.size",
        store=True,
        readonly=True,
    )
    quantity = fields.Float(
        string="Menu Quantity",
        required=True,
        default=1.0,
        tracking=True,
    )
    reported_amount = fields.Monetary(
        string="Original / Reported Amount",
        default=0.0,
        tracking=True,
        help=(
            "Financial amount reported by Reception for this cancellation or "
            "complimentary event. It does not drive stock quantities."
        ),
    )
    currency_id = fields.Many2one(
        related="company_id.currency_id",
        store=True,
        readonly=True,
    )
    reason = fields.Char(required=True, tracking=True)
    note = fields.Text(tracking=True)
    kitchen_note = fields.Text(tracking=True)
    manager_note = fields.Text(tracking=True)
    preparation_status = fields.Selection(
        [
            ("pending", "Pending Kitchen Check"),
            ("prepared", "Prepared"),
            ("not_prepared", "Not Prepared"),
        ],
        required=True,
        default="pending",
        readonly=True,
        copy=False,
        tracking=True,
        index=True,
    )
    state = fields.Selection(
        [
            ("draft", "Draft"),
            ("kitchen_review", "Kitchen Check"),
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
    kitchen_checked_by_id = fields.Many2one(
        "res.users",
        string="Kitchen Check Confirmed By",
        readonly=True,
        copy=False,
    )
    kitchen_checked_at = fields.Datetime(readonly=True, copy=False)
    manager_id = fields.Many2one(
        "res.users",
        string="Manager Reviewed By",
        readonly=True,
        copy=False,
    )
    manager_reviewed_at = fields.Datetime(readonly=True, copy=False)
    closing_id = fields.Many2one(
        "restaurant.daily.closing",
        string="Reception Daily Closing",
        readonly=False,
        copy=False,
        ondelete="restrict",
        index=True,
    )
    attachment_ids = fields.Many2many(
        "ir.attachment",
        "restaurant_stock_event_attachment_rel",
        "event_id",
        "attachment_id",
        string="Receipt / Report Proof",
        copy=False,
    )
    daily_id = fields.Many2one(
        "restaurant.stock.daily",
        string="Daily Stock",
        readonly=True,
        copy=False,
        ondelete="restrict",
    )
    application_ids = fields.One2many(
        "restaurant.stock.event.application",
        "event_id",
        string="Stock Impact Snapshot",
        readonly=True,
    )
    mapping_missing = fields.Boolean(
        compute="_compute_mapping_missing",
        store=True,
    )
    application_status = fields.Selection(
        [
            ("pending", "Pending Workflow"),
            ("missing_mapping", "Missing Mapping"),
            ("pending_daily", "Pending Daily Stock"),
            ("applied", "Applied to Daily Stock"),
            ("no_stock_effect", "No Stock Effect"),
        ],
        compute="_compute_application_status",
        string="Daily Stock Status",
    )

    @api.depends("preparation_status", "variant_id.mapping_ids")
    def _compute_mapping_missing(self):
        for event in self:
            event.mapping_missing = (
                event.preparation_status == "prepared"
                and not event.variant_id.mapping_ids
            )

    @api.depends(
        "state",
        "preparation_status",
        "mapping_missing",
        "application_ids.daily_line_id",
    )
    def _compute_application_status(self):
        for event in self:
            if event.state != "approved":
                event.application_status = (
                    "missing_mapping" if event.mapping_missing else "pending"
                )
            elif event.preparation_status == "not_prepared":
                event.application_status = "no_stock_effect"
            elif event.mapping_missing:
                event.application_status = "missing_mapping"
            elif (
                event.application_ids
                and not event.application_ids.filtered(
                    lambda application: not application.daily_line_id
                )
            ):
                event.application_status = "applied"
            else:
                event.application_status = "pending_daily"

    @api.onchange("category_id")
    def _onchange_category_id(self):
        if self.menu_item_id.category_id != self.category_id:
            self.menu_item_id = False
            self.variant_id = False

    @api.onchange("menu_item_id")
    def _onchange_menu_item_id(self):
        if self.variant_id.item_id != self.menu_item_id:
            self.variant_id = False

    @api.constrains(
        "branch_id",
        "category_id",
        "menu_item_id",
        "variant_id",
        "quantity",
        "reported_amount",
        "closing_id",
    )
    def _check_event_values(self):
        for event in self:
            if event.quantity <= 0:
                raise ValidationError(
                    self.env._("Event quantity must be greater than zero.")
                )
            if event.menu_item_id.category_id != event.category_id:
                raise ValidationError(
                    self.env._("The menu item does not belong to the category.")
                )
            if event.variant_id.item_id != event.menu_item_id:
                raise ValidationError(
                    self.env._("The selected size does not belong to the menu item.")
                )
            if event.variant_id.company_id != event.company_id:
                raise ValidationError(
                    self.env._(
                        "The menu variant and restaurant branch must belong "
                        "to the same company."
                    )
                )
            if event.variant_id.branch_id != event.branch_id:
                raise ValidationError(
                    self.env._(
                        "The menu item belongs to a different restaurant branch."
                    )
                )
            if event.reported_amount < 0:
                raise ValidationError(
                    self.env._("The reported event amount cannot be negative.")
                )
            if event.closing_id:
                closing = event.closing_id
                if (
                    closing.branch_id != event.branch_id
                    or closing.company_id != event.company_id
                    or closing.closing_date != event.service_date
                ):
                    raise ValidationError(
                        self.env._(
                            "The Reception Closing must match the event branch, "
                            "company, and business date."
                        )
                    )
                if closing.state == "cancelled":
                    raise ValidationError(
                        self.env._(
                            "A service event cannot be linked to a cancelled "
                            "Reception Closing."
                        )
                    )
                if event.state == "draft" and closing.state != "draft":
                    raise ValidationError(
                        self.env._(
                            "New or draft service events can only be linked to "
                            "a Draft Reception Closing."
                        )
                    )

    @api.model_create_multi
    def create(self, vals_list):
        require_role(self.env, RECEPTION_GROUP)
        prepared = []
        protected = {
            "state",
            "preparation_status",
            "reception_user_id",
            "kitchen_checked_by_id",
            "kitchen_checked_at",
            "manager_id",
            "manager_reviewed_at",
            "daily_id",
            "application_ids",
            "company_id",
            "currency_id",
        }
        for vals in vals_list:
            vals = dict(vals)
            if set(vals) & protected:
                raise AccessError(
                    self.env._("Workflow and stock-impact fields are system-controlled.")
                )
            vals.update({
                "name": self.env["ir.sequence"].next_by_code(
                    "restaurant.stock.event"
                ) or "New",
                "state": "draft",
                "preparation_status": "pending",
                "reception_user_id": self.env.uid,
            })
            prepared.append(vals)
        records = super().create(prepared)
        require_assigned_branches(records.branch_id)
        for event in records.filtered(lambda item: not item.closing_id):
            closing = self.env["restaurant.daily.closing"].search([
                ("branch_id", "=", event.branch_id.id),
                ("company_id", "=", event.company_id.id),
                ("closing_date", "=", event.service_date),
                ("state", "=", "draft"),
            ], limit=1)
            if closing:
                event.write({"closing_id": closing.id})
        return records

    def write(self, vals):
        if self.env.context.get(SYSTEM_EVENT_WRITE_CONTEXT) and self.env.su:
            return super().write(vals)

        self.check_access("write")
        require_assigned_branches(self.branch_id)
        if "state" in vals or "preparation_status" in vals:
            raise AccessError(
                self.env._("Use the workflow buttons to change event status.")
            )

        if all(event.state == "draft" for event in self):
            require_role(self.env, RECEPTION_GROUP)
            allowed = {
                "event_type",
                "branch_id",
                "service_date",
                "order_reference",
                "category_id",
                "menu_item_id",
                "variant_id",
                "quantity",
                "reported_amount",
                "reason",
                "note",
                "closing_id",
                "attachment_ids",
            }
            if set(vals) - allowed:
                raise AccessError(
                    self.env._("Only Reception event details can be edited in Draft.")
                )
            if "branch_id" in vals:
                require_assigned_branches(
                    self.env["restaurant.branch"].browse(vals["branch_id"])
                )
            if "closing_id" in vals and vals["closing_id"]:
                require_assigned_branches(
                    self.env["restaurant.daily.closing"].browse(
                        vals["closing_id"]
                    ).branch_id
                )
            return super().write(vals)

        if all(event.state == "kitchen_review" for event in self):
            require_role(self.env, STOCKKEEPER_GROUP)
            if set(vals) - {"kitchen_note"}:
                raise AccessError(
                    self.env._("The Stockkeeper can only edit the kitchen note.")
                )
            return super().write(vals)

        if all(event.state == "manager_review" for event in self):
            require_role(self.env, MANAGER_GROUP)
            if set(vals) - {"manager_note"}:
                raise AccessError(
                    self.env._("The Manager can only edit the manager note.")
                )
            return super().write(vals)

        raise AccessError(self.env._("Approved or closed events are immutable."))

    def unlink(self):
        self.check_access("unlink")
        require_role(self.env, RECEPTION_GROUP)
        require_assigned_branches(self.branch_id)
        if any(event.state != "draft" for event in self):
            raise AccessError(self.env._("Only Draft events can be deleted."))
        return super().unlink()

    def _workflow_write(self, values):
        return super(
            RestaurantStockEvent,
            self.with_context(**{SYSTEM_EVENT_WRITE_CONTEXT: True}),
        ).write(values)

    def _active_daily(self, sections=None):
        self.ensure_one()
        domain = [
            ("branch_id", "=", self.branch_id.id),
            ("company_id", "=", self.company_id.id),
            ("stock_date", "=", self.service_date),
            ("state", "!=", "cancelled"),
        ]
        if sections is not None:
            domain.extend([
                "|",
                ("section_id", "=", False),
                ("section_id", "in", sections.ids),
            ])
        return self.env["restaurant.stock.daily"].sudo().search(
            domain,
            order="id desc",
        )

    def _mapping_sections_by_product(self, require_complete=False):
        self.ensure_one()
        Section = self.env["restaurant.stock.section"].sudo()
        sections = Section.search([
            ("branch_id", "=", self.branch_id.id),
            ("active", "=", True),
        ])
        if not sections:
            return {}

        mappings = self.variant_id.mapping_ids
        result = {
            product.id: Section._section_for_product(self.branch_id, product)
            for product in mappings.stock_product_id
        }
        if require_complete:
            missing = mappings.stock_product_id.filtered(
                lambda product: not result[product.id]
            )
            if missing:
                raise ValidationError(
                    self.env._(
                        "Choose an approved Kitchen, Bar, or Disposable Product "
                        "Category before approval: %s",
                        ", ".join(missing.mapped("display_name")),
                    )
                )
        return result

    def _check_daily_is_open_for_approval(self):
        self.ensure_one()
        sections_by_product = self._mapping_sections_by_product(
            require_complete=(
                self.preparation_status == "prepared"
                and bool(self.variant_id.mapping_ids)
            )
        )
        relevant_sections = (
            self.env["restaurant.stock.section"].browse(list({
                section.id
                for section in sections_by_product.values()
                if section
            }))
            if self.preparation_status == "prepared" and sections_by_product
            else None
        )
        daily = self._active_daily(relevant_sections)
        closed = daily.filtered(lambda sheet: sheet.state == "closed")
        if closed:
            raise ValidationError(
                self.env._(
                    "Daily Stock %s is already closed. This event cannot be "
                    "approved or applied retroactively.",
                    ", ".join(closed.mapped("display_name")),
                )
            )
        current_date = get_company_business_date(self.company_id)
        if not daily and self.service_date != current_date:
            raise ValidationError(
                self.env._(
                    "No editable Daily Stock exists for service date %s. "
                    "Only the current company-local service date can wait "
                    "for a new Daily Stock sheet.",
                    self.service_date,
                )
            )
        return daily

    def action_submit_kitchen(self):
        self.ensure_one()
        require_role(self.env, RECEPTION_GROUP)
        require_assigned_branches(self.branch_id)
        self.check_access("write")
        self.lock_for_update()
        if self.state != "draft":
            raise UserError(self.env._("Only Draft events can be submitted."))
        self._check_daily_is_open_for_approval()
        if self.event_type == "complimentary":
            self._workflow_write({
                "state": "manager_review",
                "preparation_status": "prepared",
                "kitchen_checked_by_id": False,
                "kitchen_checked_at": False,
            })
        else:
            self._workflow_write({"state": "kitchen_review"})
        return True

    def _complete_kitchen_check(self, preparation_status):
        self.ensure_one()
        require_role(self.env, STOCKKEEPER_GROUP)
        require_assigned_branches(self.branch_id)
        self.check_access("write")
        self.lock_for_update()
        if self.state != "kitchen_review":
            raise UserError(
                self.env._("This event is not waiting for a kitchen check.")
            )
        self._workflow_write({
            "state": "manager_review",
            "preparation_status": preparation_status,
            "kitchen_checked_by_id": self.env.uid,
            "kitchen_checked_at": fields.Datetime.now(),
        })
        return True

    def action_mark_prepared(self):
        return self._complete_kitchen_check("prepared")

    def action_mark_not_prepared(self):
        return self._complete_kitchen_check("not_prepared")

    def action_return_to_reception(self):
        self.ensure_one()
        require_role(self.env, STOCKKEEPER_GROUP)
        require_assigned_branches(self.branch_id)
        self.check_access("write")
        self.lock_for_update()
        if self.state != "kitchen_review":
            raise UserError(self.env._("Only Kitchen Check events can be returned."))
        self._workflow_write({
            "state": "draft",
            "preparation_status": "pending",
            "kitchen_checked_by_id": False,
            "kitchen_checked_at": False,
        })
        return True

    def action_return_to_kitchen(self):
        self.ensure_one()
        require_role(self.env, MANAGER_GROUP)
        require_assigned_branches(self.branch_id)
        self.check_access("write")
        self.lock_for_update()
        if self.state != "manager_review":
            raise UserError(self.env._("Only Manager Review events can be returned."))
        if self.event_type != "cancellation":
            raise UserError(
                self.env._(
                    "Served Complimentary events do not require a Kitchen Check."
                )
            )
        self._workflow_write({
            "state": "kitchen_review",
            "preparation_status": "pending",
            "kitchen_checked_by_id": False,
            "kitchen_checked_at": False,
        })
        return True

    def _create_stock_impact_snapshot(self):
        self.ensure_one()
        if self.preparation_status != "prepared":
            return self.env["restaurant.stock.event.application"]

        mappings = self.variant_id.mapping_ids
        if not mappings:
            raise ValidationError(
                self.env._(
                    "Missing Mapping: %s has no stock-product quantity/UoM "
                    "mapping. The event remains Pending in Manager Review.",
                    self.variant_id.display_name,
                )
            )

        Application = self.env["restaurant.stock.event.application"].sudo()
        existing = Application.search([("event_id", "=", self.id)])
        if existing:
            return existing

        effect_type = (
            "waste" if self.event_type == "cancellation" else "consumption"
        )
        values = []
        sections_by_product = self._mapping_sections_by_product(
            require_complete=True
        )
        for mapping in mappings:
            section = sections_by_product.get(mapping.stock_product_id.id)
            mapping_qty = mapping.stock_qty * self.quantity
            product_qty = mapping.stock_uom_id._compute_quantity(
                mapping_qty,
                mapping.stock_product_id.uom_id,
                round=False,
            )
            values.append({
                "event_id": self.id,
                "mapping_id": mapping.id,
                "branch_id": self.branch_id.id,
                "service_date": self.service_date,
                "company_id": self.company_id.id,
                "variant_id": self.variant_id.id,
                "stock_product_id": mapping.stock_product_id.id,
                "section_id": section.id if section else False,
                "uom_id": mapping.stock_product_id.uom_id.id,
                "quantity": product_qty,
                "effect_type": effect_type,
            })
        return Application.create(values)

    def action_approve(self):
        self.ensure_one()
        require_role(self.env, MANAGER_GROUP)
        require_assigned_branches(self.branch_id)
        self.check_access("write")
        self.lock_for_update()
        self.invalidate_recordset(["state", "preparation_status"])
        if self.state != "manager_review":
            raise UserError(
                self.env._("Only events in Manager Review can be approved.")
            )
        if self.preparation_status == "pending":
            raise ValidationError(
                self.env._(
                    "Kitchen preparation is still Pending. The Stockkeeper "
                    "must confirm Prepared or Not Prepared first."
                )
            )
        if self.kitchen_checked_by_id == self.env.user:
            raise AccessError(
                self.env._(
                    "The user who confirmed the kitchen status cannot approve "
                    "the same event as Manager."
                )
            )

        daily = self._check_daily_is_open_for_approval()
        self._create_stock_impact_snapshot()
        self._workflow_write({
            "state": "approved",
            "manager_id": self.env.uid,
            "manager_reviewed_at": fields.Datetime.now(),
        })
        if daily:
            daily.sudo()._sync_approved_stock_events()
        return True

    def action_reject(self):
        self.ensure_one()
        require_role(self.env, MANAGER_GROUP)
        require_assigned_branches(self.branch_id)
        self.check_access("write")
        self.lock_for_update()
        if self.state != "manager_review":
            raise UserError(
                self.env._("Only events in Manager Review can be rejected.")
            )
        self._workflow_write({
            "state": "rejected",
            "manager_id": self.env.uid,
            "manager_reviewed_at": fields.Datetime.now(),
        })
        return True

    def action_cancel(self):
        self.ensure_one()
        require_role(self.env, RECEPTION_GROUP)
        require_assigned_branches(self.branch_id)
        self.check_access("write")
        self.lock_for_update()
        if self.state != "draft":
            raise UserError(self.env._("Only Draft events can be cancelled."))
        self._workflow_write({"state": "cancelled"})
        return True


class RestaurantStockEventApplication(models.Model):
    _name = "restaurant.stock.event.application"
    _description = "Restaurant Stock Event Impact Snapshot"
    _order = "service_date desc, event_id, stock_product_id, id"

    event_id = fields.Many2one(
        "restaurant.stock.event",
        required=True,
        ondelete="restrict",
        index=True,
    )
    mapping_id = fields.Many2one(
        "restaurant.menu.stock.mapping",
        required=True,
        ondelete="restrict",
    )
    branch_id = fields.Many2one(
        "restaurant.branch",
        required=True,
        ondelete="restrict",
        index=True,
    )
    service_date = fields.Date(required=True, index=True)
    company_id = fields.Many2one(
        "res.company",
        required=True,
        ondelete="restrict",
        index=True,
    )
    variant_id = fields.Many2one(
        "restaurant.menu.variant",
        required=True,
        ondelete="restrict",
    )
    stock_product_id = fields.Many2one(
        "product.product",
        required=True,
        ondelete="restrict",
        index=True,
    )
    section_id = fields.Many2one(
        "restaurant.stock.section",
        string="Stock Section",
        ondelete="restrict",
        index=True,
        help=(
            "Kitchen or Bar assignment captured when the event is approved. "
            "Empty is retained for legacy, unsectioned applications."
        ),
    )
    uom_id = fields.Many2one("uom.uom", required=True, ondelete="restrict")
    quantity = fields.Float(required=True)
    effect_type = fields.Selection(
        [
            ("consumption", "Complimentary Consumption"),
            ("waste", "Prepared Cancellation Waste"),
        ],
        required=True,
        index=True,
    )
    daily_id = fields.Many2one(
        "restaurant.stock.daily",
        readonly=True,
        ondelete="restrict",
        index=True,
    )
    daily_line_id = fields.Many2one(
        "restaurant.stock.daily.line",
        readonly=True,
        ondelete="restrict",
        index=True,
    )
    applied_at = fields.Datetime(readonly=True)

    _event_mapping_unique = models.UniqueIndex(
        "(event_id, mapping_id)",
        "This approved event mapping has already been applied.",
    )

    @api.model_create_multi
    def create(self, vals_list):
        if not self.env.su:
            raise AccessError(
                self.env._("Stock impact snapshots are system-controlled.")
            )
        return super().create(vals_list)

    def write(self, vals):
        if not self.env.su:
            raise AccessError(
                self.env._("Stock impact snapshots are system-controlled.")
            )
        return super().write(vals)

    def unlink(self):
        raise AccessError(
            self.env._("Approved stock impact snapshots cannot be deleted.")
        )


class RestaurantStockDailyEventSummary(models.Model):
    _name = "restaurant.stock.daily.event.summary"
    _description = "Daily Cancellation / Complimentary Menu Totals"
    _auto = False
    _rec_name = "variant_id"
    _order = "service_date desc, category_id, menu_item_id, variant_id"

    daily_id = fields.Many2one("restaurant.stock.daily", readonly=True)
    branch_id = fields.Many2one("restaurant.branch", readonly=True)
    company_id = fields.Many2one("res.company", readonly=True)
    service_date = fields.Date(readonly=True)
    category_id = fields.Many2one("restaurant.menu.category", readonly=True)
    menu_item_id = fields.Many2one("restaurant.menu.item", readonly=True)
    variant_id = fields.Many2one("restaurant.menu.variant", readonly=True)
    size = fields.Selection(
        selection=lambda self: self.env[
            "restaurant.menu.variant"
        ]._fields["size"].selection,
        readonly=True,
    )
    cancelled_qty = fields.Float(readonly=True, aggregator=None)
    prepared_cancelled_qty = fields.Float(readonly=True, aggregator=None)
    unprepared_cancelled_qty = fields.Float(readonly=True, aggregator=None)
    complimentary_qty = fields.Float(readonly=True, aggregator=None)
    prepared_complimentary_qty = fields.Float(readonly=True, aggregator=None)
    unprepared_complimentary_qty = fields.Float(readonly=True, aggregator=None)

    def init(self):
        tools.drop_view_if_exists(self.env.cr, self._table)
        self.env.cr.execute(f"""
            CREATE VIEW {self._table} AS (
                SELECT
                    MIN(event.id) AS id,
                    event.daily_id AS daily_id,
                    event.branch_id AS branch_id,
                    event.company_id AS company_id,
                    event.service_date AS service_date,
                    event.category_id AS category_id,
                    event.menu_item_id AS menu_item_id,
                    event.variant_id AS variant_id,
                    event.size AS size,
                    SUM(CASE
                        WHEN event.event_type = 'cancellation'
                        THEN event.quantity ELSE 0 END
                    ) AS cancelled_qty,
                    SUM(CASE
                        WHEN event.event_type = 'cancellation'
                         AND event.preparation_status = 'prepared'
                        THEN event.quantity ELSE 0 END
                    ) AS prepared_cancelled_qty,
                    SUM(CASE
                        WHEN event.event_type = 'cancellation'
                         AND event.preparation_status = 'not_prepared'
                        THEN event.quantity ELSE 0 END
                    ) AS unprepared_cancelled_qty,
                    SUM(CASE
                        WHEN event.event_type = 'complimentary'
                        THEN event.quantity ELSE 0 END
                    ) AS complimentary_qty,
                    SUM(CASE
                        WHEN event.event_type = 'complimentary'
                         AND event.preparation_status = 'prepared'
                        THEN event.quantity ELSE 0 END
                    ) AS prepared_complimentary_qty,
                    SUM(CASE
                        WHEN event.event_type = 'complimentary'
                         AND event.preparation_status = 'not_prepared'
                        THEN event.quantity ELSE 0 END
                    ) AS unprepared_complimentary_qty
                FROM restaurant_stock_event AS event
                WHERE event.state = 'approved'
                  AND event.daily_id IS NOT NULL
                GROUP BY
                    event.daily_id,
                    event.branch_id,
                    event.company_id,
                    event.service_date,
                    event.category_id,
                    event.menu_item_id,
                    event.variant_id,
                    event.size
            )
        """)

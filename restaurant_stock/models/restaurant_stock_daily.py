from datetime import timedelta

from odoo import Command, api, fields, models, tools
from odoo.exceptions import AccessError, UserError, ValidationError

from .stock_security import (
    MANAGER_GROUP,
    STOCKKEEPER_GROUP,
    get_company_business_date,
    get_company_day_utc_range,
    lock_records,
    require_assigned_branches,
    require_role,
)


INVENTORY_RECEIPT_SYNC_CONTEXT = "restaurant_inventory_receipt_sync"
SOLD_QUANTITY_SYNC_CONTEXT = "restaurant_sold_quantity_sync"
ALLOW_NONCURRENT_DAILY_DATE_CONTEXT = (
    "restaurant_allow_noncurrent_daily_date"
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

    section_id = fields.Many2one(
        "restaurant.stock.section",
        string="Stock Section",
        ondelete="restrict",
        index=True,
        tracking=True,
        help=(
            "Kitchen, Bar, or Disposable group for this sheet. Empty is retained for "
            "legacy branches that have not configured stock sections."
        ),
    )

    uses_stock_sections = fields.Boolean(
        compute="_compute_uses_stock_sections",
        compute_sudo=True,
    )

    stock_date = fields.Date(
        required=True,
        default=lambda self: get_company_business_date(self.env.company),
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

    can_edit_stock_lines = fields.Boolean(
        compute="_compute_can_edit_stock_lines",
    )

    line_ids = fields.One2many(
        "restaurant.stock.daily.line",
        "daily_id",
        string="Stock Lines",
        copy=True,
    )

    existing_daily_id = fields.Many2one(
        "restaurant.stock.daily",
        string="Existing Daily Stock",
        compute="_compute_existing_daily_id",
        readonly=True,
        help=(
            "The active Daily Stock sheet that already owns the selected "
            "branch and service date."
        ),
    )

    generated_request_id = fields.Many2one(
        "restaurant.stock.request",
        string="Generated Request",
        readonly=True,
        copy=False,
        ondelete="restrict",
        help="The single stock request generated from this closing sheet.",
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
        "(branch_id, stock_date, COALESCE(section_id, 0)) "
        "WHERE state != 'cancelled'",
        "An active daily stock sheet already exists for this branch, date, and section.",
    )

    @api.constrains("branch_id", "section_id")
    def _check_section_branch(self):
        for sheet in self.filtered("section_id"):
            if sheet.section_id.branch_id != sheet.branch_id:
                raise ValidationError(
                    self.env._("The stock section belongs to another branch.")
                )
            if sheet.section_id.company_id != sheet.company_id:
                raise ValidationError(
                    self.env._("The stock section belongs to another company.")
                )

    @api.model
    def _active_sections_for_branch(self, branch):
        return self.env["restaurant.stock.section"].sudo().search([
            ("branch_id", "=", branch.id),
            ("company_id", "=", branch.company_id.id),
            ("active", "=", True),
        ], order="section_type, id")

    @api.depends("branch_id")
    def _compute_uses_stock_sections(self):
        for sheet in self:
            sheet.uses_stock_sections = bool(
                sheet.branch_id
                and sheet._active_sections_for_branch(sheet.branch_id)
            )

    def _validate_section_policy(self):
        for sheet in self:
            sections = self._active_sections_for_branch(sheet.branch_id)
            if sections and not sheet.section_id:
                raise ValidationError(
                    self.env._(
                        "Choose Kitchen, Bar, or Disposable for this branch's "
                        "Daily Stock sheet."
                    )
                )
            if sheet.section_id and sheet.section_id not in sections:
                raise ValidationError(
                    self.env._(
                        "The selected stock section is not active for this branch."
                    )
                )

    @api.depends("state")
    @api.depends_context("uid")
    def _compute_can_edit_stock_lines(self):
        is_stockkeeper = self.env.user.has_group(STOCKKEEPER_GROUP)
        is_manager = self.env.user.has_group(MANAGER_GROUP)
        for record in self:
            record.can_edit_stock_lines = (
                is_stockkeeper and record.state in ("draft", "opened")
            ) or (
                is_manager and record.state == "closing_review"
            )

    @api.model
    def _validate_current_service_date(self, stock_date, company):
        if (
            self.env.su
            and self.env.context.get(ALLOW_NONCURRENT_DAILY_DATE_CONTEXT)
        ):
            return True

        selected_date = fields.Date.to_date(stock_date)
        current_date = get_company_business_date(company)
        if selected_date != current_date:
            raise ValidationError(
                self.env._(
                    "Daily Stock can only be created or opened for the "
                    "current company service date (%s). Selected date: %s. "
                    "An already opened prior-day sheet may still be closed "
                    "after midnight.",
                    current_date,
                    selected_date,
                )
            )

        return True

    def _find_existing_daily(self):
        self.ensure_one()
        if not self.branch_id or not self.stock_date:
            return self.env["restaurant.stock.daily"]

        domain = [
            ("branch_id", "=", self.branch_id.id),
            ("company_id", "=", self.branch_id.company_id.id),
            ("stock_date", "=", self.stock_date),
            ("section_id", "=", self.section_id.id or False),
            ("state", "!=", "cancelled"),
        ]
        if self._origin.id:
            domain.append(("id", "!=", self._origin.id))

        return self.env["restaurant.stock.daily"].search(
            domain,
            order="id desc",
            limit=1,
        )

    @api.depends("branch_id", "stock_date", "section_id")
    def _compute_existing_daily_id(self):
        for record in self:
            record.existing_daily_id = record._find_existing_daily()

    @api.onchange("branch_id", "stock_date", "section_id")
    def _onchange_opening_context(self):
        for record in self:
            if not record.branch_id or not record.stock_date:
                record.line_ids = [Command.clear()]
                continue

            sections = record._active_sections_for_branch(record.branch_id)
            if sections and not record.section_id:
                record.line_ids = [Command.clear()]
                continue

            if record._find_existing_daily():
                # A closed/open sheet already owns this service date.  Do not
                # show a second, recalculated Opening that cannot be saved and
                # could be mistaken for the next service day's carryover.
                record.line_ids = [Command.clear()]
                continue

            record.line_ids = [
                Command.clear(),
                *[
                    Command.create(line_values)
                    for line_values in record._prepare_automatic_opening_lines()
                ],
            ]

    @api.model
    def default_get(self, fields_list):
        values = super().default_get(fields_list)

        # Automatically select the Stockkeeper's branch.
        branch = False

        assigned_branches = self.env["restaurant.branch"].sudo().search([
            ("user_ids", "in", [self.env.uid]),
            ("company_id", "=", self.env.company.id),
        ])

        if len(assigned_branches) == 1:
            branch = assigned_branches

        if branch and "branch_id" in fields_list:
            values["branch_id"] = branch.id

        section = False
        if branch:
            sections = self._active_sections_for_branch(branch)
            default_section_id = self.env.context.get("default_section_id")
            if default_section_id:
                section = sections.filtered(
                    lambda current: current.id == default_section_id
                )[:1]
            elif len(sections) == 1:
                section = sections
        if section and "section_id" in fields_list:
            values["section_id"] = section.id

        # Today by default.
        stock_date = self.env.context.get("default_stock_date")
        if not stock_date:
            company = branch.company_id if branch else self.env.company
            stock_date = get_company_business_date(company)

        if "stock_date" in fields_list:
            values["stock_date"] = stock_date

        # Show the opening snapshot immediately in the New form, except when
        # this branch/date already has an active sheet.  In that case the form
        # displays an explicit link to the existing record and no misleading
        # duplicate Opening lines.
        if branch and "line_ids" in fields_list and (
            section or not self._active_sections_for_branch(branch)
        ):
            draft = self.new({
                "branch_id": branch.id,
                "stock_date": stock_date,
                "section_id": section.id if section else False,
            })

            opening_lines = (
                []
                if draft._find_existing_daily()
                else draft._prepare_automatic_opening_lines()
            )

            values["line_ids"] = [
                (0, 0, line_values)
                for line_values in opening_lines
            ]

        return values

    def _prepare_automatic_opening_lines(self):
        self.ensure_one()
        self._validate_section_policy()

        branch = self.branch_id.sudo()
        warehouse = branch.warehouse_id

        if not warehouse:
            raise UserError(
                self.env._(
                    "No warehouse is configured for branch %s."
                ) % branch.display_name
            )

        stock_location = warehouse.lot_stock_id

        catalog_product_ids = set(self._get_catalog_product_ids())

        quant_domain = [
            ("location_id", "child_of", stock_location.id),
            ("company_id", "=", branch.company_id.id),
            ("quantity", "!=", 0),
        ]
        if self.section_id:
            quant_domain.append(
                ("product_id", "in", list(catalog_product_ids))
            )
        quants = self.env["stock.quant"].sudo().search(quant_domain)

        native_quantities = {}

        for quant in quants:
            product = quant.product_id

            if not product.is_storable:
                continue

            native_quantities[product.id] = (
                native_quantities.get(product.id, 0.0)
                + quant.quantity
            )

        received_quantities = self._get_received_quantities_from_central()

        previous_closed_sheet = self.env["restaurant.stock.daily"].sudo().search([
            ("branch_id", "=", branch.id),
            ("company_id", "=", branch.company_id.id),
            ("stock_date", "<", self.stock_date),
            ("section_id", "=", self.section_id.id or False),
            ("state", "=", "closed"),
        ], order="stock_date desc, reviewed_at desc, id desc", limit=1)

        post_close_receipts = (
            self._get_post_close_receipt_quantities(previous_closed_sheet)
            if previous_closed_sheet
            else {}
        )

        # Inventory already includes every completed receipt for this service
        # day.  Remove those receipts to reconstruct the quantity that existed
        # before receiving; otherwise the first Daily sheet counts the same
        # receipt once in Opening and again in Received.
        opening_quantities = {
            product_id: quantity - received_quantities.get(product_id, 0.0)
            for product_id, quantity in native_quantities.items()
        }

        # A confirmed prior close remains the authoritative carryover.  Add
        # only receipts completed after that close and before this service day:
        # those moves were too late to belong to the closed sheet, while this
        # service day's receipts remain separate in Received.  Products absent
        # from the prior sheet retain the native reconstruction above, which
        # already contains their legitimate stock and avoids adding a receipt
        # twice.
        if previous_closed_sheet:
            for line in previous_closed_sheet.line_ids:
                opening_quantities[line.product_id.id] = (
                    line.uom_id._compute_quantity(
                        line.actual_closing_qty,
                        line.product_id.uom_id,
                        round=False,
                    )
                    + post_close_receipts.get(line.product_id.id, 0.0)
                )

        product_ids = (
            set(opening_quantities)
            | set(received_quantities)
            | catalog_product_ids
        )
        if self.section_id:
            product_ids &= catalog_product_ids
        products = (
            self.env["product.product"]
            .sudo()
            .browse(product_ids)
            .sorted("display_name")
        )

        result = []

        for product in products:
            quantity = opening_quantities.get(product.id, 0.0)
            received_quantity = received_quantities.get(product.id, 0.0)

            if (
                product.id not in catalog_product_ids
                and product.uom_id.is_zero(quantity)
                and product.uom_id.is_zero(received_quantity)
            ):
                continue

            result.append({
                "product_id": product.id,
                "uom_id": product.uom_id.id,
                "opening_qty": quantity,
                "received_qty": received_quantity,
            })

        return result

    @api.model_create_multi
    def create(self, vals_list):
        require_role(self.env, STOCKKEEPER_GROUP)

        prepared = []
        active_keys = set()

        for values in vals_list:
            values = dict(values)

            branch = self.env["restaurant.branch"].browse(
                values.get("branch_id")
            )
            if branch:
                stock_date = fields.Date.to_date(
                    values.get(
                        "stock_date",
                        get_company_business_date(branch.company_id),
                    )
                )
                section_id = values.get("section_id") or False
                key = (branch.id, stock_date, section_id)
                existing = self.sudo().search([
                    ("branch_id", "=", branch.id),
                    ("stock_date", "=", stock_date),
                    ("section_id", "=", section_id),
                    ("state", "!=", "cancelled"),
                ], limit=1)
                if existing or key in active_keys:
                    raise ValidationError(
                        self.env._(
                            "An active Daily Stock sheet already exists for "
                            "this branch, date, and section."
                        )
                    )
                active_keys.add(key)
                self._validate_current_service_date(
                    stock_date,
                    branch.company_id,
                )

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

            # Prevent default_get from injecting opening lines for its
            # wall-clock default date when the caller explicitly creates a
            # different service date.  UI forms already submit their visible
            # line commands, which remain untouched.
            if "line_ids" not in values:
                values["line_ids"] = []

            prepared.append(values)

        records = super().create(prepared)

        require_assigned_branches(records.branch_id)
        records._validate_section_policy()

        Line = self.env["restaurant.stock.daily.line"]

        for record in records:
            # If lines were explicitly provided, do not overwrite them.
            if record.line_ids:
                continue

            opening_lines = record._prepare_automatic_opening_lines()

            if opening_lines:
                Line.create([
                    {
                        **line_values,
                        "daily_id": record.id,
                    }
                    for line_values in opening_lines
                ])

        # Received is a derived calendar-day total.  Recompute it on creation
        # as the receipt may have been completed before the sheet was opened.
        records._sync_received_from_central()

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

        if (
            self.env.user.has_group(MANAGER_GROUP)
            and all(record.state == "closing_review" for record in self)
        ):
            require_assigned_branches(self.branch_id)
            if set(vals) - {"line_ids", "sold_menu_line_ids", "note"}:
                raise AccessError(
                    self.env._(
                        "During Closing Review, the Branch Manager can only "
                        "edit closing quantities and notes."
                    )
                )
            return super().write(vals)

        require_role(self.env, STOCKKEEPER_GROUP)
        require_assigned_branches(self.branch_id)

        if "stock_date" in vals or "branch_id" in vals:
            for record in self:
                branch = (
                    self.env["restaurant.branch"].browse(vals["branch_id"])
                    if "branch_id" in vals
                    else record.branch_id
                )
                stock_date = vals.get("stock_date", record.stock_date)
                record._validate_current_service_date(
                    stock_date,
                    branch.company_id,
                )

        if any(record.state not in ("draft", "opened") for record in self):
            raise AccessError(
                self.env._(
                    "Only draft or opened stock sheets can be edited."
                )
            )

        editable = {
            "branch_id",
            "section_id",
            "stock_date",
            "line_ids",
            "sold_menu_line_ids",
            "note",
        }

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

        if "section_id" in vals and any(
            record.state != "draft" for record in self
        ):
            raise AccessError(
                self.env._(
                    "The stock section can only be changed while the sheet is draft."
                )
            )

        result = super().write(vals)
        if "branch_id" in vals or "section_id" in vals:
            self._validate_section_policy()
        return result

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

    def _get_stock_day_utc_range(self):
        self.ensure_one()
        return get_company_day_utc_range(self.company_id, self.stock_date)

    def _get_section_product_ids(self):
        self.ensure_one()
        if not self.section_id:
            return []
        return self.env["restaurant.stock.section"]._products_for_section(
            self.section_id
        ).ids

    def _get_catalog_product_ids(self):
        self.ensure_one()
        Section = self.env["restaurant.stock.section"]
        if self.section_id:
            return Section._products_for_section(self.section_id).ids
        if self._active_sections_for_branch(self.branch_id):
            # Legacy active sheets without a section keep one combined list
            # until they finish; closed history is never rewritten.
            return Section._catalog_products(self.company_id).ids
        return []

    def _ensure_catalog_lines(self):
        Line = self.env["restaurant.stock.daily.line"].sudo().with_context(
            **{INVENTORY_RECEIPT_SYNC_CONTEXT: True}
        )
        for sheet in self:
            if sheet.state in ("closed", "cancelled"):
                continue
            existing_ids = set(sheet.line_ids.product_id.ids)
            products = self.env["product.product"].sudo().browse(
                sheet._get_catalog_product_ids()
            )
            missing = products.filtered(lambda product: product.id not in existing_ids)
            if missing:
                Line.create([
                    {
                        "daily_id": sheet.id,
                        "product_id": product.id,
                        "opening_qty": 0.0,
                        "received_qty": 0.0,
                        "incoming_transfer_qty": 0.0,
                    }
                    for product in missing
                ])
        return True

    def _get_received_quantities_from_central(self):
        self.ensure_one()

        warehouse = self.branch_id.sudo().warehouse_id
        transit_location = self.company_id.internal_transit_location_id

        if not warehouse:
            raise UserError(
                self.env._(
                    "No warehouse is configured for branch %s."
                ) % self.branch_id.display_name
            )

        if not transit_location:
            raise UserError(
                self.env._(
                    "The inter-warehouse transit location is not "
                    "configured for this company."
                )
            )

        start_dt, end_dt = self._get_stock_day_utc_range()
        move_domain = [
            ("state", "=", "done"),
            ("company_id", "=", self.company_id.id),
            ("picking_id.date_done", ">=", start_dt),
            ("picking_id.date_done", "<", end_dt),
            ("location_id", "child_of", transit_location.id),
            ("location_dest_id", "child_of", warehouse.lot_stock_id.id),
        ]
        if self.section_id:
            move_domain.append(
                ("product_id", "in", self._get_section_product_ids())
            )
        moves = self.env["stock.move"].sudo().search(move_domain)

        totals = {}
        for move in moves:
            product = move.product_id
            quantity = move.uom_id._compute_quantity(
                move.quantity,
                product.uom_id,
                round=False,
            )
            totals[product.id] = totals.get(product.id, 0.0) + quantity

        return totals

    def _get_post_close_receipt_quantities(self, previous_closed_sheet):
        """Return receipts owned by the gap after the prior final close.

        A receipt completed before the manager's final close is synchronized
        into that sheet (including while it is in Closing Review), so the
        physical close can include it.  A receipt completed strictly after the
        final-close cutoff cannot belong to the frozen sheet.  Carry only those
        gap receipts into the next Opening, stopping at the next service-day
        boundary so that today's receipts remain in Received.
        """
        self.ensure_one()
        previous_closed_sheet.ensure_one()

        close_cutoff = previous_closed_sheet.reviewed_at
        if not close_cutoff:
            # Legacy closed rows without an auditable final-close timestamp do
            # not provide a safe ownership boundary.  Falling back to the
            # confirmed close avoids silently double-counting old receipts.
            return {}

        service_day_start, _service_day_end = self._get_stock_day_utc_range()
        if close_cutoff >= service_day_start:
            # An overnight close happened on or after the new service-day
            # boundary.  Subsequent receipts belong to this day's Received.
            return {}

        warehouse = self.branch_id.sudo().warehouse_id
        transit_location = self.company_id.internal_transit_location_id
        if not warehouse or not transit_location:
            return {}

        move_domain = [
            ("state", "=", "done"),
            ("company_id", "=", self.company_id.id),
            ("picking_id.date_done", ">", close_cutoff),
            ("picking_id.date_done", "<", service_day_start),
            ("location_id", "child_of", transit_location.id),
            ("location_dest_id", "child_of", warehouse.lot_stock_id.id),
        ]
        if self.section_id:
            move_domain.append(
                ("product_id", "in", self._get_section_product_ids())
            )
        moves = self.env["stock.move"].sudo().search(move_domain)

        totals = {}
        for move in moves:
            product = move.product_id
            quantity = move.uom_id._compute_quantity(
                move.quantity,
                product.uom_id,
                round=False,
            )
            totals[product.id] = totals.get(product.id, 0.0) + quantity

        return totals

    def _sync_received_from_central(self):
        require_role(self.env, STOCKKEEPER_GROUP)
        require_assigned_branches(self.branch_id)

        Line = self.env["restaurant.stock.daily.line"]

        for sheet in self:
            if sheet.state in ("closed", "cancelled"):
                continue

            if sheet.state not in ("draft", "opened", "closing_review"):
                raise UserError(
                    self.env._(
                        "Central receipts can only be refreshed while the "
                        "sheet is active or under closing review."
                    )
                )

            sheet._ensure_catalog_lines()

            totals = sheet._get_received_quantities_from_central()

            # Apply the complete derived value once per existing line.  This is
            # idempotent and never creates a transient all-zero Daily between a
            # reset pass and a repopulation pass.  Opening is deliberately not
            # part of receipt refresh: it is a frozen service-start snapshot.
            remaining_totals = dict(totals)
            for line in sheet.line_ids:
                received_in_product_uom = remaining_totals.pop(
                    line.product_id.id,
                    0.0,
                )
                received_in_line_uom = line.product_id.uom_id._compute_quantity(
                    received_in_product_uom,
                    line.uom_id,
                    round=False,
                )
                line.with_context(
                    **{INVENTORY_RECEIPT_SYNC_CONTEXT: True}
                ).write({
                    "received_qty": received_in_line_uom,
                    "incoming_transfer_qty": 0.0,
                })

            for product_id, received_qty in remaining_totals.items():
                product = self.env["product.product"].sudo().browse(product_id)
                Line.with_context(
                    **{INVENTORY_RECEIPT_SYNC_CONTEXT: True}
                ).create({
                    "daily_id": sheet.id,
                    "product_id": product.id,
                    "opening_qty": 0.0,
                    "received_qty": received_qty,
                    "incoming_transfer_qty": 0.0,
                })

        return True

    @api.model
    def _sync_completed_branch_receipt(self, picking, branch, source_record):
        """Synchronize a validated Transit -> Branch picking automatically.

        ``stock.picking.date_done`` is Odoo's reliable validation timestamp
        for the receipt pickings created by both branch receipt workflows.
        """
        picking = picking.sudo()
        branch = branch.sudo()
        if (
            not picking
            or picking.state != "done"
            or not picking.date_done
            or picking.company_id != branch.company_id
        ):
            return self.browse()

        receipt_date = get_company_business_date(
            branch.company_id,
            picking.date_done,
        )
        # Receipt synchronization is a system-owned continuation of an
        # already authorized branch receipt.  It must work whether the
        # receiver is the assigned Stockkeeper or Manager, without granting
        # Managers general Daily Stock creation rights.
        Daily = self.sudo()
        legacy_sheet = Daily.search([
            ("branch_id", "=", branch.id),
            ("company_id", "=", branch.company_id.id),
            ("stock_date", "=", receipt_date),
            ("section_id", "=", False),
            ("state", "!=", "cancelled"),
        ], order="id desc", limit=1)
        if legacy_sheet:
            if legacy_sheet.state == "closed":
                return self.browse()
            legacy_sheet._sync_received_from_central()
            if legacy_sheet.state == "draft":
                legacy_sheet.action_confirm_opening()
            return legacy_sheet

        sections = Daily._active_sections_for_branch(branch)
        if sections:
            receipt_products = picking.move_ids.filtered(
                lambda move: (
                    move.state == "done"
                    and move.product_id.is_storable
                    and move.quantity > 0
                )
            ).product_id
            Section = self.env["restaurant.stock.section"]
            section_by_product = {
                product.id: Section._section_for_product(branch, product)
                for product in receipt_products
            }
            missing_products = receipt_products.filtered(
                lambda product: not section_by_product[product.id]
            )
            if missing_products:
                raise ValidationError(
                    self.env._(
                        "Choose an approved Kitchen, Bar, or Disposable Product "
                        "Category before confirming the receipt: %s",
                        ", ".join(missing_products.mapped("display_name")),
                    )
                )

            result = Daily.browse()
            relevant_sections = Section.browse([
                section.id for section in section_by_product.values() if section
            ])
            for section in relevant_sections:
                sheet = Daily.search([
                    ("branch_id", "=", branch.id),
                    ("company_id", "=", branch.company_id.id),
                    ("stock_date", "=", receipt_date),
                    ("section_id", "=", section.id),
                    ("state", "!=", "cancelled"),
                ], order="id desc", limit=1)
                if sheet and sheet.state == "closed":
                    continue
                if not sheet:
                    sheet = Daily.create({
                        "branch_id": branch.id,
                        "section_id": section.id,
                        "stock_date": receipt_date,
                    })
                else:
                    sheet._sync_received_from_central()
                if sheet.state == "draft":
                    sheet.action_confirm_opening()
                result |= sheet
            return result

        sheet = Daily.search([
            ("branch_id", "=", branch.id),
            ("company_id", "=", branch.company_id.id),
            ("stock_date", "=", receipt_date),
            ("section_id", "=", False),
            ("state", "!=", "cancelled"),
        ], order="id desc", limit=1)

        # Confirming the first Incoming Receipt is the operational Opening for
        # its actual confirmation date.  A closed service is immutable: later
        # same-date receipts affect native stock but never reopen or rewrite it.
        if sheet and sheet.state == "closed":
            return self.browse()
        if not sheet:
            sheet = Daily.create({
                "branch_id": branch.id,
                "stock_date": receipt_date,
            })
        else:
            sheet._sync_received_from_central()
        if sheet.state == "draft":
            sheet.action_confirm_opening()
        return sheet

    def _sync_stock_movements(self):
        """Compatibility wrapper for callers of the earlier refresh method."""
        return self._sync_received_from_central()

    def action_refresh_movements(self):
        self.ensure_one()
        self._sync_stock_movements()

        return {
            "type": "ir.actions.client",
            "tag": "reload",
        }

    def action_confirm_opening(self):
        self.ensure_one()
        self._validate_section_policy()
        self._validate_current_service_date(
            self.stock_date,
            self.company_id,
        )
        self.line_ids._validate_available_quantities()
        return self.write({"state": "opened"})

    def action_submit_closing(self):
        self.ensure_one()

        # Always refresh real Inventory movements before closing review.
        self._sync_stock_movements()
        self.line_ids._validate_available_quantities()
        self._validate_physical_counts_complete()

        self._create_or_update_generated_request()

        return self.write({"state": "closing_review"})

    def _create_or_update_generated_request(self):
        """Create the one request owned by this closing, or safely refresh it."""
        self.ensure_one()
        require_role(self.env, STOCKKEEPER_GROUP)
        require_assigned_branches(self.branch_id)

        positive_lines = self.line_ids.filtered(
            lambda line: line.uom_id.compare(line.request_tomorrow_qty, 0) > 0
        )
        request = self.generated_request_id.sudo()
        if not request:
            request = self.env["restaurant.stock.request"].sudo().search([
                ("daily_stock_id", "=", self.id),
            ], limit=1)

        if not positive_lines:
            if request and request.state in ("draft", "submitted"):
                request.line_ids.with_context(
                    generated_from_daily_stock=True,
                ).unlink()
                if request.state == "submitted":
                    request.message_post(body=self.env._(
                        "The linked closing was resubmitted without requested quantities."
                    ))
            if request and self.generated_request_id != request:
                super(RestaurantStockDaily, self.sudo()).write({
                    "generated_request_id": request.id,
                })
            return request

        line_commands = [
            Command.create({
                "product_id": line.product_id.id,
                "requested_qty": line.request_tomorrow_qty,
            })
            for line in positive_lines
        ]

        if request:
            if request.state not in ("draft", "submitted"):
                requested = {
                    line.product_id.id: line.requested_qty
                    for line in request.line_ids
                }
                closing = {
                    line.product_id.id: line.request_tomorrow_qty
                    for line in positive_lines
                }
                if requested != closing:
                    raise UserError(self.env._(
                        "The generated request is already being processed by Central. "
                        "Return it to an editable state before changing requested quantities."
                    ))
            else:
                request.line_ids.with_context(
                    generated_from_daily_stock=True,
                ).unlink()
                request.with_context(generated_from_daily_stock=True).write({
                    "line_ids": line_commands,
                })
        else:
            request = self.env["restaurant.stock.request"].sudo().with_context(
                generated_from_daily_stock=True,
            ).create({
                "branch_id": self.branch_id.id,
                "request_date": self.stock_date + timedelta(days=1),
                "requested_by_id": self.env.uid,
                "daily_stock_id": self.id,
                "line_ids": line_commands,
            })

        if request.state == "draft":
            request.sudo().with_context(generated_from_daily_stock=True).write({
                "state": "submitted",
            })

        if self.generated_request_id != request:
            super(RestaurantStockDaily, self.sudo()).write({
                "generated_request_id": request.id,
            })

        return request

    def _apply_daily_line_stock_moves(
        self,
        quantity_field,
        move_field,
        destination_name,
        label,
    ):
        """Post one audited native deduction per line and category."""
        StockMove = self.env["stock.move"].sudo()
        Location = self.env["stock.location"].sudo()

        for sheet in self:
            warehouse = sheet.branch_id.sudo().warehouse_id
            if not warehouse:
                raise UserError(
                    self.env._(
                        "No warehouse is configured for branch %s."
                    ) % sheet.branch_id.display_name
                )

            source_location = warehouse.lot_stock_id

            destination = Location.search([
                ("name", "=", destination_name),
                ("usage", "=", "inventory"),
                ("company_id", "in", [False, sheet.company_id.id]),
            ], limit=1)

            if not destination:
                destination = Location.create({
                    "name": destination_name,
                    "usage": "inventory",
                    "company_id": sheet.company_id.id,
                })

            for line in sheet.line_ids.filtered(
                lambda item: item.uom_id.compare(
                    getattr(item, quantity_field),
                    0,
                ) > 0
            ):
                quantity = getattr(line, quantity_field)
                move = getattr(line, move_field).sudo()

                if move and move.state == "done":
                    continue

                if move and move.state == "cancel":
                    super(
                        RestaurantStockDailyLine,
                        line.sudo(),
                    ).write({move_field: False})
                    move = self.env["stock.move"]

                if not move:
                    move = StockMove.create({
                        "description_picking": "%s - %s - %s" % (
                            sheet.display_name,
                            label,
                            line.product_id.display_name,
                        ),
                        "origin": sheet.display_name,
                        "company_id": sheet.company_id.id,
                        "product_id": line.product_id.id,
                        "product_uom_qty": quantity,
                        "location_id": source_location.id,
                        "location_dest_id": destination.id,
                    })

                    super(
                        RestaurantStockDailyLine,
                        line.sudo(),
                    ).write({move_field: move.id})

                if move.state == "draft":
                    move._action_confirm()

                if move.state not in ("assigned", "done"):
                    move._action_assign()

                if move.state == "done":
                    continue

                move.quantity = quantity
                move.picked = True
                move._action_done()

        return True

    def _apply_consumption_stock_moves(self):
        """Post every known depletion once at the manager's final Close."""
        self._apply_daily_line_stock_moves(
            "sold_qty",
            "sold_move_id",
            "Restaurant Sold",
            "Sold",
        )
        self._apply_daily_line_stock_moves(
            "consumption_qty",
            "consumption_move_id",
            "Restaurant Consumption",
            "Legacy Consumption",
        )
        self._apply_daily_line_stock_moves(
            "waste_qty",
            "waste_move_id",
            "Restaurant Waste",
            "Waste",
        )
        self._apply_daily_line_stock_moves(
            "damaged_qty",
            "damaged_move_id",
            "Restaurant Damaged",
            "Damaged",
        )
        self._apply_daily_line_stock_moves(
            "missing_qty",
            "missing_move_id",
            "Restaurant Missing",
            "Missing",
        )
        return True

    def action_close(self):
        self.ensure_one()
        lock_records(self)

        if self.state != "closing_review":
            raise UserError(
                self.env._(
                    "Daily Stock can only be closed after it has been "
                    "submitted for closing review."
                )
            )

        self._validate_physical_counts_complete()
        self.line_ids._validate_available_quantities()
        self.sudo()._create_or_update_generated_request()
        self._apply_consumption_stock_moves()
        return self.write({"state": "closed"})

    def action_return_to_open(self):
        self.ensure_one()
        return self.write({"state": "opened"})

    def _validate_physical_counts_complete(self):
        for sheet in self:
            uncounted = sheet.line_ids.filtered(
                lambda line: line.actual_closing_state != "counted"
            )
            if uncounted:
                raise ValidationError(
                    self.env._(
                        "Complete and confirm the physical Actual Closing count "
                        "for every product before continuing. Not Counted: %s",
                        ", ".join(uncounted.mapped("product_id.display_name")),
                    )
                )
        return True

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

    section_id = fields.Many2one(
        related="daily_id.section_id",
        string="Stock Section",
        store=True,
        readonly=True,
        index=True,
    )

    stock_date = fields.Date(
        related="daily_id.stock_date",
        store=True,
        readonly=True,
    )

    daily_state = fields.Selection(
        related="daily_id.state",
        string="Status",
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

    opening_qty = fields.Float(
        string="Opening Qty",
        default=0.0,
        readonly=True,
        aggregator=None,
    )

    current_on_hand_qty = fields.Float(
        string="Current",
        compute="_compute_current_on_hand_qty",
        aggregator=None,
    )

    received_qty = fields.Float(
        string="Received Qty",
        default=0.0,
        readonly=True,
        aggregator=None,
    )

    incoming_transfer_qty = fields.Float(
        string="Incoming Transfer",
        default=0.0,
        aggregator=None,
    )

    outgoing_transfer_qty = fields.Float(
        string="Outgoing Transfer",
        default=0.0,
        aggregator=None,
    )

    consumption_qty = fields.Float(
        string="Legacy Consumption",
        default=0.0,
        aggregator=None,
        help=(
            "Historical manual consumption retained for audit. New daily "
            "closings use mapped Sold entries instead of this input."
        ),
    )

    sold_qty = fields.Float(
        string="Sold",
        default=0.0,
        readonly=True,
        aggregator=None,
        help=(
            "Native stock quantity derived from manually entered sold menu "
            "variants through their explicit stock quantity and UoM mappings."
        ),
    )

    consumption_move_id = fields.Many2one(
        "stock.move",
        string="Consumption Stock Move",
        readonly=True,
        copy=False,
        ondelete="set null",
        help=(
            "Native inventory move created when the manager finally closes "
            "this Daily Stock sheet."
        ),
    )

    sold_move_id = fields.Many2one(
        "stock.move",
        string="Sold Stock Move",
        readonly=True,
        copy=False,
        ondelete="set null",
    )

    waste_move_id = fields.Many2one(
        "stock.move",
        string="Waste Stock Move",
        readonly=True,
        copy=False,
        ondelete="set null",
    )

    damaged_move_id = fields.Many2one(
        "stock.move",
        string="Damaged Stock Move",
        readonly=True,
        copy=False,
        ondelete="set null",
    )

    missing_move_id = fields.Many2one(
        "stock.move",
        string="Missing Stock Move",
        readonly=True,
        copy=False,
        ondelete="set null",
    )

    waste_qty = fields.Float(
        string="Waste Qty",
        default=0.0,
        aggregator=None,
    )

    damaged_qty = fields.Float(
        string="Damaged Qty",
        default=0.0,
        aggregator=None,
    )

    expected_closing_qty = fields.Float(
        string="Expected Closing",
        compute="_compute_expected_closing",
        aggregator=None,
    )

    actual_closing_qty = fields.Float(
        string="Actual Closing",
        default=0.0,
        aggregator=None,
        help="Manual physical count. Zero is valid only when Count Status is Counted.",
    )

    actual_closing_state = fields.Selection(
        [
            ("not_counted", "Not Counted"),
            ("counted", "Counted"),
        ],
        string="Count Status",
        required=True,
        default="not_counted",
        copy=False,
        index=True,
        help=(
            "Confirms that Actual Closing is a completed physical count. "
            "An untouched zero remains Not Counted and never becomes Missing."
        ),
    )

    request_tomorrow_qty = fields.Float(
        string="Request for Tomorrow",
        default=0.0,
        aggregator=None,
    )

    difference_qty = fields.Float(
        string="Difference",
        compute="_compute_difference",
        aggregator=None,
    )

    missing_qty = fields.Float(
        string="Missing",
        compute="_compute_variance_quantities",
        aggregator=None,
        help=(
            "Expected Closing minus Actual Closing when the physical count is "
            "short. Surplus never becomes negative Missing."
        ),
    )

    surplus_qty = fields.Float(
        string="Surplus",
        compute="_compute_variance_quantities",
        aggregator=None,
        help=(
            "Actual Closing above Expected Closing. It is reported for review "
            "and never reduces Sold automatically."
        ),
    )

    total_consumption_qty = fields.Float(
        string="Total Consumption",
        compute="_compute_total_consumption",
        aggregator=None,
        help=(
            "Internal compatibility value for Sold plus historical manual "
            "consumption. It is not displayed as a closing input or total."
        ),
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

        allowed_states = ("draft", "opened")
        if (
            self.env.context.get(INVENTORY_RECEIPT_SYNC_CONTEXT)
            or self.env.context.get(SOLD_QUANTITY_SYNC_CONTEXT)
        ):
            allowed_states += ("closing_review",)

        if any(record.daily_id.state not in allowed_states for record in records):
            raise AccessError(
                self.env._(
                    "Stock lines can only be added to draft or opened stock sheets."
                )
            )

        return records


    def write(self, vals):
        vals = dict(vals)
        if (
            "actual_closing_qty" in vals
            and "actual_closing_state" not in vals
        ):
            # A write to Actual Closing is an explicit physical count, even
            # when the entered quantity is zero. Newly generated untouched
            # lines still keep their default Not Counted state.
            vals["actual_closing_state"] = "counted"

        require_assigned_branches(self.branch_id)

        if self.env.context.get(SOLD_QUANTITY_SYNC_CONTEXT) and self.env.su:
            if set(vals) != {"sold_qty"}:
                raise AccessError(
                    self.env._("Only mapped Sold quantity can be synchronized.")
                )
            return super().write(vals)

        if self.env.context.get(INVENTORY_RECEIPT_SYNC_CONTEXT):
            if set(vals) - {"received_qty", "incoming_transfer_qty"}:
                raise AccessError(
                    self.env._(
                        "Only inventory-derived receipt fields can be "
                        "synchronized during closing review."
                    )
                )
            synchronized_states = (
                "draft",
                "opened",
                "closing_review",
            )
            if any(
                record.daily_id.state not in synchronized_states
                for record in self
            ):
                raise AccessError(
                    self.env._(
                        "This stock sheet cannot be synchronized."
                    )
            )
            return super().write(vals)

        if self.env.user.has_group(MANAGER_GROUP):
            if any(
                record.daily_id.state != "closing_review"
                for record in self
            ):
                raise AccessError(
                    self.env._(
                        "Closing quantities can only be edited by the Branch "
                        "Manager during Closing Review."
                    )
                )

            manager_review_fields = {
                "consumption_qty",
                "waste_qty",
                "total_waste_qty",
                "damaged_qty",
                "actual_closing_qty",
                "actual_closing_state",
                "request_tomorrow_qty",
                "note",
            }
            if set(vals) - manager_review_fields:
                raise AccessError(
                    self.env._(
                        "During Closing Review, the Branch Manager can only "
                        "edit legacy Consumption, Waste, Damaged, Actual Closing, "
                        "Request for Tomorrow, and notes."
                    )
                )
            result = super().write(vals)
            self._validate_available_quantities()
            return result

        require_role(self.env, STOCKKEEPER_GROUP)

        if any(
            record.daily_id.state not in ("draft", "opened")
            for record in self
        ):
            raise AccessError(
                self.env._(
                    "Stock lines can only be edited while "
                    "the stock sheet is draft or opened."
                )
            )

        protected_fields = {
            "opening_qty",
            "received_qty",
            "incoming_transfer_qty",
            "outgoing_transfer_qty",
            "sold_qty",
            "consumption_move_id",
            "sold_move_id",
            "waste_move_id",
            "damaged_move_id",
            "missing_move_id",
            "product_id",
            "uom_id",
            "daily_id",
            "branch_id",
            "stock_date",
            "company_id",
        }

        if set(vals) & protected_fields:
            raise AccessError(
                self.env._(
                    "Opening and product identity are system-controlled "
                    "and cannot be changed manually."
                )
            )

        return super().write(vals)

    def action_mark_counted(self):
        """Explicitly confirm a physical zero without inventing a quantity."""
        self.ensure_one()
        return self.write({"actual_closing_state": "counted"})


    def unlink(self):
        raise AccessError(
            self.env._(
                "Products cannot be removed manually "
                "from a Daily Stock sheet."
            )
        )

    @api.depends(
        "current_on_hand_qty",
        "sold_qty",
        "consumption_qty",
        "waste_qty",
        "damaged_qty",
    )
    def _compute_expected_closing(self):
        for line in self:
            line.expected_closing_qty = (
                line.current_on_hand_qty
                - line.sold_qty
                - line.consumption_qty
                - line.waste_qty
                - line.damaged_qty
            )

    @api.depends(
        "actual_closing_qty",
        "actual_closing_state",
        "expected_closing_qty",
    )
    def _compute_difference(self):
        for line in self:
            line.difference_qty = 0.0
            if line.actual_closing_state == "counted":
                line.difference_qty = (
                    line.actual_closing_qty
                    - line.expected_closing_qty
                )

    @api.depends(
        "actual_closing_qty",
        "actual_closing_state",
        "expected_closing_qty",
    )
    def _compute_variance_quantities(self):
        for line in self:
            if line.actual_closing_state != "counted":
                line.missing_qty = 0.0
                line.surplus_qty = 0.0
                continue
            variance = line.actual_closing_qty - line.expected_closing_qty
            line.missing_qty = max(-variance, 0.0)
            line.surplus_qty = max(variance, 0.0)

    @api.depends(
        "sold_qty",
        "consumption_qty",
    )
    def _compute_total_consumption(self):
        for line in self:
            line.total_consumption_qty = (
                line.sold_qty
                + line.consumption_qty
            )

    @api.depends("opening_qty", "received_qty")
    def _compute_current_on_hand_qty(self):
        for line in self:
            line.current_on_hand_qty = (
                line.opening_qty
                + line.received_qty
            )

    @api.constrains(
        "opening_qty",
        "received_qty",
        "incoming_transfer_qty",
        "outgoing_transfer_qty",
        "sold_qty",
        "consumption_qty",
        "waste_qty",
        "damaged_qty",
        "actual_closing_qty",
        "request_tomorrow_qty",
    )
    def _check_non_negative_quantities(self):
        quantity_fields = (
            "opening_qty",
            "received_qty",
            "incoming_transfer_qty",
            "outgoing_transfer_qty",
            "sold_qty",
            "consumption_qty",
            "waste_qty",
            "damaged_qty",
            "actual_closing_qty",
            "request_tomorrow_qty",
        )

        for line in self:
            if any(
                line.uom_id.compare(
                    getattr(line, field_name),
                    0.0,
                ) < 0
                for field_name in quantity_fields
            ):
                raise ValidationError(
                    self.env._(
                        "%s: stock quantities cannot be negative.",
                        line.product_id.display_name,
                    )
                )

        self._validate_available_quantities()

    def _validate_available_quantities(self):
        for line in self:
            current_qty = line.opening_qty + line.received_qty
            consumption_qty = line.sold_qty + line.consumption_qty
            total_out_qty = (
                consumption_qty
                + line.waste_qty
                + line.damaged_qty
            )

            if line.uom_id.compare(consumption_qty, current_qty) > 0:
                raise ValidationError(
                    self.env._(
                        "%s: Sold plus legacy consumption %s %s cannot exceed "
                        "Current %s %s.",
                        line.product_id.display_name,
                        consumption_qty,
                        line.uom_id.display_name,
                        current_qty,
                        line.uom_id.display_name,
                    )
                )

            if line.uom_id.compare(total_out_qty, current_qty) > 0:
                raise ValidationError(
                    self.env._(
                        "%s: Sold + legacy consumption + Waste + Damaged (%s %s) "
                        "cannot exceed Current (%s %s).",
                        line.product_id.display_name,
                        total_out_qty,
                        line.uom_id.display_name,
                        current_qty,
                        line.uom_id.display_name,
                    )
                )

        return True


class RestaurantStockDailyProductTotal(models.Model):
    _name = "restaurant.stock.daily.product.total"
    _description = "Restaurant Daily Stock Product Total"
    _auto = False
    _rec_name = "product_id"
    _order = "stock_date desc, product_id, uom_id"

    company_id = fields.Many2one("res.company", readonly=True, index=True)
    stock_date = fields.Date(readonly=True, index=True)
    product_id = fields.Many2one(
        "product.product",
        readonly=True,
        index=True,
    )
    uom_id = fields.Many2one("uom.uom", readonly=True)
    included_statuses = fields.Char(readonly=True)
    branch_count = fields.Integer(readonly=True, aggregator=None)
    sheet_count = fields.Integer(readonly=True, aggregator=None)
    counted_line_count = fields.Integer(readonly=True, aggregator=None)
    uncounted_line_count = fields.Integer(readonly=True, aggregator=None)
    opening_qty = fields.Float(
        string="Total Opening",
        readonly=True,
        aggregator=None,
    )
    received_qty = fields.Float(
        string="Total Received",
        readonly=True,
        aggregator=None,
    )
    current_on_hand_qty = fields.Float(
        string="Total Current",
        readonly=True,
        aggregator=None,
    )
    consumption_qty = fields.Float(
        string="Total Consumption",
        readonly=True,
        aggregator=None,
    )
    waste_qty = fields.Float(
        string="Total Waste",
        readonly=True,
        aggregator=None,
    )
    damaged_qty = fields.Float(
        string="Total Damaged",
        readonly=True,
        aggregator=None,
    )
    expected_closing_qty = fields.Float(
        string="Total Expected Closing",
        readonly=True,
        aggregator=None,
    )
    actual_closing_qty = fields.Float(
        string="Total Actual Closing",
        readonly=True,
        aggregator=None,
    )
    request_tomorrow_qty = fields.Float(
        string="Total Request for Tomorrow",
        readonly=True,
        aggregator=None,
    )
    difference_qty = fields.Float(
        string="Total Difference",
        readonly=True,
        aggregator=None,
    )

    def init(self):
        tools.drop_view_if_exists(self.env.cr, self._table)
        self.env.cr.execute(f"""
            CREATE VIEW {self._table} AS (
                SELECT
                    MIN(line.id) AS id,
                    daily.company_id AS company_id,
                    daily.stock_date AS stock_date,
                    line.product_id AS product_id,
                    template.uom_id AS uom_id,
                    STRING_AGG(
                        DISTINCT CASE daily.state
                            WHEN 'draft' THEN 'Draft'
                            WHEN 'opened' THEN 'Opened'
                            WHEN 'closing_review' THEN 'Closing Review'
                            WHEN 'closed' THEN 'Closed'
                        END,
                        ', '
                    ) AS included_statuses,
                    COUNT(DISTINCT daily.branch_id) AS branch_count,
                    COUNT(DISTINCT daily.id) AS sheet_count,
                    COUNT(*) FILTER (
                        WHERE line.actual_closing_state = 'counted'
                    ) AS counted_line_count,
                    COUNT(*) FILTER (
                        WHERE line.actual_closing_state != 'counted'
                    ) AS uncounted_line_count,
                    SUM(COALESCE(line.opening_qty, 0.0)) AS opening_qty,
                    SUM(COALESCE(line.received_qty, 0.0)) AS received_qty,
                    SUM(
                        COALESCE(line.opening_qty, 0.0)
                        + COALESCE(line.received_qty, 0.0)
                    ) AS current_on_hand_qty,
                    SUM(COALESCE(line.consumption_qty, 0.0)) AS consumption_qty,
                    SUM(COALESCE(line.waste_qty, 0.0)) AS waste_qty,
                    SUM(COALESCE(line.damaged_qty, 0.0)) AS damaged_qty,
                    SUM(
                        COALESCE(line.opening_qty, 0.0)
                        + COALESCE(line.received_qty, 0.0)
                        - COALESCE(line.sold_qty, 0.0)
                        - COALESCE(line.consumption_qty, 0.0)
                        - COALESCE(line.waste_qty, 0.0)
                        - COALESCE(line.damaged_qty, 0.0)
                    ) AS expected_closing_qty,
                    SUM(
                        CASE
                            WHEN line.actual_closing_state = 'counted'
                            THEN COALESCE(line.actual_closing_qty, 0.0)
                            ELSE 0.0
                        END
                    ) AS actual_closing_qty,
                    SUM(
                        COALESCE(line.request_tomorrow_qty, 0.0)
                    ) AS request_tomorrow_qty,
                    SUM(
                        CASE
                            WHEN line.actual_closing_state = 'counted'
                            THEN COALESCE(line.actual_closing_qty, 0.0)
                                - (
                                    COALESCE(line.opening_qty, 0.0)
                                    + COALESCE(line.received_qty, 0.0)
                                    - COALESCE(line.sold_qty, 0.0)
                                    - COALESCE(line.consumption_qty, 0.0)
                                    - COALESCE(line.waste_qty, 0.0)
                                    - COALESCE(line.damaged_qty, 0.0)
                                )
                            ELSE 0.0
                        END
                    ) AS difference_qty
                FROM restaurant_stock_daily_line AS line
                JOIN restaurant_stock_daily AS daily
                    ON daily.id = line.daily_id
                JOIN product_product AS product
                    ON product.id = line.product_id
                JOIN product_template AS template
                    ON template.id = product.product_tmpl_id
                WHERE daily.state != 'cancelled'
                GROUP BY
                    daily.company_id,
                    daily.stock_date,
                    line.product_id,
                    template.uom_id
            )
        """)

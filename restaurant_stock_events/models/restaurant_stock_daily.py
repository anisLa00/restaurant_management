from odoo import api, fields, models, tools
from odoo.exceptions import AccessError, UserError, ValidationError

from odoo.addons.restaurant_stock.models.restaurant_stock_daily import (
    INVENTORY_RECEIPT_SYNC_CONTEXT,
    SOLD_QUANTITY_SYNC_CONTEXT,
)

from .restaurant_stock_event import SYSTEM_EVENT_WRITE_CONTEXT


class RestaurantStockDaily(models.Model):
    _inherit = "restaurant.stock.daily"

    stock_event_ids = fields.One2many(
        "restaurant.stock.event",
        "daily_id",
        string="Approved Cancellation / Complimentary Events",
        readonly=True,
    )
    stock_event_summary_ids = fields.One2many(
        "restaurant.stock.daily.event.summary",
        "daily_id",
        string="Cancellation / Complimentary Totals",
        readonly=True,
    )
    menu_stock_line_ids = fields.One2many(
        "restaurant.stock.daily.line",
        "daily_id",
        string="Menu / Sold Items",
        domain=[("operation_mode", "in", ("menu", "setup_incomplete"))],
    )
    consumption_stock_line_ids = fields.One2many(
        "restaurant.stock.daily.line",
        "daily_id",
        string="Consumption Items",
        domain=[("operation_mode", "=", "consumption")],
    )
    historical_stock_line_ids = fields.One2many(
        "restaurant.stock.daily.line",
        "daily_id",
        string="Historical Stock Lines",
        domain=[("operation_mode", "=", "legacy")],
    )
    has_menu_stock_lines = fields.Boolean(compute="_compute_stock_line_modes")
    has_consumption_stock_lines = fields.Boolean(
        compute="_compute_stock_line_modes",
    )
    has_incomplete_menu_setup = fields.Boolean(
        compute="_compute_stock_line_modes",
    )
    has_historical_stock_lines = fields.Boolean(
        compute="_compute_stock_line_modes",
    )

    @api.depends("line_ids.operation_mode")
    def _compute_stock_line_modes(self):
        for daily in self:
            modes = set(daily.line_ids.mapped("operation_mode"))
            daily.has_menu_stock_lines = bool(
                modes & {"menu", "setup_incomplete"}
            )
            daily.has_consumption_stock_lines = "consumption" in modes
            daily.has_incomplete_menu_setup = "setup_incomplete" in modes
            daily.has_historical_stock_lines = "legacy" in modes

    def write(self, vals):
        values = dict(vals)
        split_commands = []
        for field_name in (
            "menu_stock_line_ids",
            "consumption_stock_line_ids",
            "historical_stock_line_ids",
        ):
            split_commands.extend(values.pop(field_name, []))
        if split_commands:
            values["line_ids"] = list(values.get("line_ids", [])) + split_commands
        return super().write(values)

    def _validate_menu_setup_ready(self):
        for daily in self:
            incomplete = daily.line_ids.filtered(
                lambda line: line.operation_mode == "setup_incomplete"
            )
            if incomplete:
                raise ValidationError(
                    self.env._(
                        "Complete Menu Setup before opening or closing Daily "
                        "Stock. These products have an active but incomplete "
                        "setup: %s",
                        ", ".join(incomplete.mapped("product_id.display_name")),
                    )
                )
        return True

    def _validate_manual_consumption_closing(self):
        for daily in self:
            pure_lines = daily.line_ids.filtered(
                lambda line: line.operation_mode == "consumption"
            )
            incompatible = pure_lines.filtered(
                lambda line: any((
                    line.uom_id.compare(line.sold_qty, 0.0) > 0,
                    line.uom_id.compare(line.auto_complimentary_qty, 0.0) > 0,
                    line.uom_id.compare(line.waste_qty, 0.0) > 0,
                    line.uom_id.compare(
                        line.auto_cancellation_waste_qty,
                        0.0,
                    ) > 0,
                    line.uom_id.compare(line.damaged_qty, 0.0) > 0,
                ))
            )
            if incompatible:
                raise ValidationError(
                    self.env._(
                        "Pure Consumption items cannot contain Sold, "
                        "Complimentary, Waste, or Damaged quantities. Correct "
                        "the Menu Setup or source entry before closing: %s",
                        ", ".join(incompatible.mapped("product_id.display_name")),
                    )
                )

            counted = pure_lines.filtered(
                lambda line: line.actual_closing_state == "counted"
            )
            above_current = counted.filtered(
                lambda line: line.uom_id.compare(
                    line.actual_closing_qty,
                    line.current_on_hand_qty,
                ) > 0
            )
            if above_current:
                raise ValidationError(
                    self.env._(
                        "Actual is above Current for these Consumption items: "
                        "%s. A non-negative Consumption quantity cannot "
                        "reconcile that surplus. Correct the physical count, "
                        "Opening, or Received source data; do not invent "
                        "negative usage.",
                        ", ".join(above_current.mapped("product_id.display_name")),
                    )
                )

            mismatched = counted.filtered(
                lambda line: line.uom_id.compare(
                    line.actual_closing_qty,
                    line.expected_closing_qty,
                ) != 0
            )
            if mismatched:
                details = ", ".join(
                    "%s (Expected %s, Actual %s)" % (
                        line.product_id.display_name,
                        line.expected_closing_qty,
                        line.actual_closing_qty,
                    )
                    for line in mismatched
                )
                raise ValidationError(
                    self.env._(
                        "Correct Consumption until Expected matches Actual, "
                        "then record the reason in Note. No quantity is "
                        "rewritten and no Missing deduction is created "
                        "automatically. %s",
                        details,
                    )
                )

            manual_lines = daily.line_ids.filtered(
                lambda line: (
                    line.operation_mode in ("menu", "consumption")
                    and line.manual_consumption_enabled
                    and line.uom_id.compare(line.consumption_qty, 0.0) > 0
                )
            )
            missing_notes = manual_lines.filtered(
                lambda line: not (line.note or "").strip()
            )
            if missing_notes:
                raise ValidationError(
                    self.env._(
                        "Record the reason for manual Consumption in Note "
                        "before closing: %s",
                        ", ".join(missing_notes.mapped("product_id.display_name")),
                    )
                )
        return True

    @api.model_create_multi
    def create(self, vals_list):
        records = super().create(vals_list)
        records.sudo()._sync_approved_stock_events()
        return records

    def _sync_approved_stock_events(self):
        Event = self.env["restaurant.stock.event"].sudo()
        Application = self.env["restaurant.stock.event.application"].sudo()
        Line = self.env["restaurant.stock.daily.line"].sudo().with_context(
            **{INVENTORY_RECEIPT_SYNC_CONTEXT: True}
        )

        for daily in self.sudo():
            if daily.state in ("closed", "cancelled"):
                continue

            events = Event.search([
                ("state", "=", "approved"),
                ("company_id", "=", daily.company_id.id),
                ("branch_id", "=", daily.branch_id.id),
                ("service_date", "=", daily.stock_date),
            ])
            if events and not daily.section_id:
                events.with_context(**{
                    SYSTEM_EVENT_WRITE_CONTEXT: True,
                }).write({"daily_id": daily.id})
                events.flush_recordset(["daily_id"])

            application_domain = [
                ("event_id", "in", events.ids),
                ("daily_line_id", "=", False),
            ]
            if daily.section_id:
                application_domain.append(
                    ("section_id", "=", daily.section_id.id)
                )
            applications = Application.search(application_domain)
            for application in applications:
                line = daily.line_ids.filtered(
                    lambda item: (
                        item.product_id == application.stock_product_id
                    )
                )[:1]
                if not line:
                    line = Line.create({
                        "daily_id": daily.id,
                        "product_id": application.stock_product_id.id,
                        "opening_qty": 0.0,
                        "received_qty": 0.0,
                        "incoming_transfer_qty": 0.0,
                    })
                application.write({
                    "daily_id": daily.id,
                    "daily_line_id": line.id,
                    "applied_at": fields.Datetime.now(),
                })

            if daily.section_id:
                for event in applications.event_id:
                    application_sections = event.application_ids.section_id
                    if (
                        application_sections == daily.section_id
                        and event.daily_id != daily
                    ):
                        event.with_context(**{
                            SYSTEM_EVENT_WRITE_CONTEXT: True,
                        }).write({"daily_id": daily.id})
                        event.flush_recordset(["daily_id"])

        return True

    def action_confirm_opening(self):
        self.check_access("write")
        self.sudo()._sync_approved_stock_events()
        self._validate_menu_setup_ready()
        return super().action_confirm_opening()

    def action_submit_closing(self):
        self.check_access("write")
        self.sudo()._sync_approved_stock_events()
        self._validate_menu_setup_ready()
        self._validate_manual_consumption_closing()
        return super().action_submit_closing()

    def action_close(self):
        self.check_access("write")
        self.sudo()._sync_approved_stock_events()
        self._validate_menu_setup_ready()
        self._validate_manual_consumption_closing()
        return super().action_close()

    def _get_event_destination_location(self, name):
        self.ensure_one()
        Location = self.env["stock.location"].sudo()
        location = Location.search([
            ("name", "=", name),
            ("usage", "=", "inventory"),
            ("company_id", "in", [False, self.company_id.id]),
        ], limit=1)
        if not location:
            location = Location.create({
                "name": name,
                "usage": "inventory",
                "company_id": self.company_id.id,
            })
        return location

    def _apply_component_stock_moves(
        self,
        quantity_field,
        move_field,
        destination_name,
        label,
    ):
        StockMove = self.env["stock.move"].sudo()
        Line = self.env["restaurant.stock.daily.line"]

        for sheet in self:
            warehouse = sheet.branch_id.sudo().warehouse_id
            if not warehouse:
                raise UserError(
                    self.env._(
                        "No warehouse is configured for branch %s.",
                        sheet.branch_id.display_name,
                    )
                )
            source_location = warehouse.lot_stock_id
            destination = sheet._get_event_destination_location(
                destination_name
            )

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
                    models.Model.write(line.sudo(), {move_field: False})
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
                    models.Model.write(line.sudo(), {move_field: move.id})

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
        self._apply_component_stock_moves(
            "sold_qty",
            "sold_move_id",
            "Restaurant Sold",
            "Sold",
        )
        self._apply_component_stock_moves(
            "consumption_move_qty",
            "consumption_move_id",
            "Restaurant Consumption",
            "Prepared Complimentary / Manual Consumption",
        )
        self._apply_component_stock_moves(
            "waste_qty",
            "waste_move_id",
            "Restaurant Waste",
            "Waste",
        )
        self._apply_component_stock_moves(
            "damaged_qty",
            "damaged_move_id",
            "Restaurant Damaged",
            "Damaged",
        )
        self._apply_component_stock_moves(
            "missing_qty",
            "missing_move_id",
            "Restaurant Missing",
            "Missing",
        )
        self._apply_component_stock_moves(
            "auto_cancellation_waste_qty",
            "cancellation_waste_move_id",
            "Restaurant Cancellation Waste",
            "Prepared Cancellation Waste",
        )
        return True


class RestaurantStockDailyLine(models.Model):
    _inherit = "restaurant.stock.daily.line"

    _event_system_fields = {
        "stock_event_application_ids",
        "auto_complimentary_qty",
        "auto_cancellation_waste_qty",
        "consumption_move_qty",
        "total_consumption_qty",
        "cancellation_waste_move_id",
        "operation_mode",
        "menu_setup_id",
        "allow_additional_consumption",
    }

    consumption_qty = fields.Float(
        string="Consumption",
        help=(
            "Manual stock-unit consumption. For a Menu / Sold item this is "
            "available only when the branch Menu Setup explicitly enables "
            "Additional Consumption; never enter a quantity already included "
            "in mapped Sold."
        ),
    )
    operation_mode = fields.Selection(
        [
            ("legacy", "Historical (Before Split)"),
            ("menu", "Menu / Sold"),
            ("consumption", "Consumption"),
            ("setup_incomplete", "Menu Setup Incomplete"),
        ],
        string="Daily Operation",
        required=True,
        default="legacy",
        readonly=True,
        copy=False,
        index=True,
    )
    menu_setup_id = fields.Many2one(
        "restaurant.menu.mapping.profile",
        string="Menu Setup Snapshot",
        readonly=True,
        copy=False,
        ondelete="restrict",
        help="Branch Menu Setup used to classify this Daily Stock line.",
    )
    allow_additional_consumption = fields.Boolean(
        string="Additional Consumption Enabled",
        readonly=True,
        copy=False,
        help=(
            "Snapshot of the branch Menu Setup option for this daily sheet. "
            "The quantity itself always starts at zero on a new sheet."
        ),
    )
    manual_consumption_enabled = fields.Boolean(
        compute="_compute_manual_consumption_enabled",
    )
    waste_qty = fields.Float(
        string="Manual Waste",
        help=(
            "Manual waste only. Do not include approved Prepared Cancellation "
            "events; they are added separately and audibly."
        ),
    )
    stock_event_application_ids = fields.One2many(
        "restaurant.stock.event.application",
        "daily_line_id",
        string="Approved Event Components",
        readonly=True,
    )
    auto_complimentary_qty = fields.Float(
        string="Complimentary",
        compute="_compute_event_components",
        store=True,
        readonly=True,
        aggregator=None,
    )
    auto_cancellation_waste_qty = fields.Float(
        string="Prepared Cancelled",
        compute="_compute_event_components",
        store=True,
        readonly=True,
        aggregator=None,
    )
    total_consumption_qty = fields.Float(
        string="Total Consumption",
        compute="_compute_event_components",
        store=True,
        readonly=True,
        aggregator=None,
    )
    consumption_move_qty = fields.Float(
        compute="_compute_event_components",
        store=True,
        readonly=True,
        aggregator=None,
    )
    total_waste_qty = fields.Float(
        string="Waste",
        compute="_compute_event_components",
        inverse="_inverse_total_waste_qty",
        store=True,
        aggregator=None,
    )
    cancellation_waste_move_id = fields.Many2one(
        "stock.move",
        string="Prepared Cancellation Waste Move",
        readonly=True,
        copy=False,
        ondelete="set null",
    )

    @api.depends("operation_mode", "allow_additional_consumption")
    def _compute_manual_consumption_enabled(self):
        for line in self:
            line.manual_consumption_enabled = (
                line.operation_mode == "consumption"
                or (
                    line.operation_mode == "legacy"
                    and line.daily_state not in ("closed", "cancelled")
                )
                or (
                    line.operation_mode == "menu"
                    and line.allow_additional_consumption
                )
            )

    @api.model
    def _operation_mode_values(self, daily, product):
        if not daily.section_id:
            return {
                "operation_mode": "legacy",
                "menu_setup_id": False,
                "allow_additional_consumption": False,
            }

        profiles = self.env["restaurant.menu.mapping.profile"].sudo().with_context(
            active_test=False,
        ).search([
            ("company_id", "=", daily.company_id.id),
            ("branch_id", "=", daily.branch_id.id),
            ("stock_product_id", "=", product.id),
            ("active", "=", True),
        ], order="id")
        if profiles:
            mode = (
                "setup_incomplete"
                if any(not profile.configuration_complete for profile in profiles)
                else "menu"
            )
            return {
                "operation_mode": mode,
                "menu_setup_id": profiles[0].id,
                "allow_additional_consumption": (
                    mode == "menu"
                    and any(profiles.mapped("allow_additional_consumption"))
                ),
            }

        legacy_mapping = self.env[
            "restaurant.menu.stock.mapping"
        ].sudo().search([
            ("company_id", "=", daily.company_id.id),
            ("branch_id", "=", daily.branch_id.id),
            ("stock_product_id", "=", product.id),
            ("variant_id.active", "=", True),
            ("variant_id.item_id.active", "=", True),
        ], limit=1)
        return {
            "operation_mode": "menu" if legacy_mapping else "consumption",
            "menu_setup_id": False,
            "allow_additional_consumption": False,
        }

    def _refresh_operation_mode_from_setup(self):
        for line in self:
            if line.daily_state in ("closed", "cancelled"):
                continue
            values = line._operation_mode_values(
                line.daily_id,
                line.product_id,
            )
            models.Model.write(line.sudo(), values)
        return True

    @api.model_create_multi
    def create(self, vals_list):
        if not self.env.su and any(
            set(values) & self._event_system_fields
            for values in vals_list
        ):
            raise AccessError(
                self.env._(
                    "Approved event components and stock moves are "
                    "system-controlled."
                )
            )
        prepared = []
        for incoming_values in vals_list:
            values = dict(incoming_values)
            daily = self.env["restaurant.stock.daily"].browse(
                values.get("daily_id")
            )
            product = self.env["product.product"].browse(
                values.get("product_id")
            )
            if daily and product:
                values.update(self._operation_mode_values(daily, product))
            prepared.append(values)
        return super().create(prepared)

    def write(self, vals):
        if not self.env.su and set(vals) & self._event_system_fields:
            raise AccessError(
                self.env._(
                    "Approved event components and stock moves are "
                    "system-controlled."
                )
            )
        if "consumption_qty" in vals:
            disabled = self.filtered(
                lambda line: not line.manual_consumption_enabled
            )
            if disabled:
                raise AccessError(
                    self.env._(
                        "Manual Consumption is not enabled for these Menu / "
                        "Sold items: %s. A manager must enable Additional "
                        "Consumption in this branch's Menu Setup for a future "
                        "daily sheet.",
                        ", ".join(disabled.mapped("product_id.display_name")),
                    )
                )
        return super().write(vals)

    @api.depends(
        "opening_qty",
        "received_qty",
        "actual_closing_qty",
        "sold_qty",
        "consumption_qty",
        "waste_qty",
        "damaged_qty",
        "stock_event_application_ids.quantity",
        "stock_event_application_ids.effect_type",
    )
    def _compute_event_components(self):
        for line in self:
            consumption = sum(
                line.stock_event_application_ids.filtered(
                    lambda item: item.effect_type == "consumption"
                ).mapped("quantity")
            )
            cancellation_waste = sum(
                line.stock_event_application_ids.filtered(
                    lambda item: item.effect_type == "waste"
                ).mapped("quantity")
            )
            line.auto_complimentary_qty = consumption
            line.auto_cancellation_waste_qty = cancellation_waste
            line.consumption_move_qty = line.consumption_qty + consumption
            line.total_waste_qty = line.waste_qty + cancellation_waste
            line.total_consumption_qty = (
                line.sold_qty + line.consumption_move_qty
            )

    def _inverse_total_waste_qty(self):
        for line in self:
            if line.uom_id.compare(
                line.total_waste_qty,
                line.auto_cancellation_waste_qty,
            ) < 0:
                raise ValidationError(
                    self.env._(
                        "%s: Waste cannot be lower than the approved prepared "
                        "cancellation quantity (%s %s).",
                        line.product_id.display_name,
                        line.auto_cancellation_waste_qty,
                        line.uom_id.display_name,
                    )
                )
            line.waste_qty = (
                line.total_waste_qty
                - line.auto_cancellation_waste_qty
            )

    @api.depends(
        "current_on_hand_qty",
        "sold_qty",
        "consumption_move_qty",
        "total_waste_qty",
        "damaged_qty",
        "operation_mode",
    )
    def _compute_expected_closing(self):
        for line in self:
            if line.operation_mode == "consumption":
                line.expected_closing_qty = (
                    line.current_on_hand_qty - line.consumption_qty
                )
            else:
                line.expected_closing_qty = (
                    line.current_on_hand_qty
                    - line.sold_qty
                    - line.consumption_move_qty
                    - line.total_waste_qty
                    - line.damaged_qty
                )

    @api.depends(
        "actual_closing_qty",
        "actual_closing_state",
        "expected_closing_qty",
        "operation_mode",
    )
    def _compute_variance_quantities(self):
        super()._compute_variance_quantities()
        for line in self.filtered(
            lambda current: current.operation_mode == "consumption"
        ):
            line.missing_qty = 0.0
            line.surplus_qty = 0.0

    def _validate_available_quantities(self):
        for line in self:
            current_qty = line.opening_qty + line.received_qty
            complimentary_qty = sum(
                line.stock_event_application_ids.filtered(
                    lambda item: item.effect_type == "consumption"
                ).mapped("quantity")
            )
            cancellation_waste_qty = sum(
                line.stock_event_application_ids.filtered(
                    lambda item: item.effect_type == "waste"
                ).mapped("quantity")
            )
            consumption_qty = (
                line.sold_qty
                + line.consumption_qty
                + complimentary_qty
            )
            total_waste_qty = line.waste_qty + cancellation_waste_qty
            total_out_qty = (
                consumption_qty
                + total_waste_qty
                + line.damaged_qty
            )
            if line.uom_id.compare(consumption_qty, current_qty) > 0:
                raise ValidationError(
                    self.env._(
                        "%s: Sold + Complimentary + manual Consumption %s %s "
                        "cannot exceed Current "
                        "%s %s.",
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
                        "%s: Sold + Complimentary + manual Consumption + "
                        "Waste + Damaged "
                        "(%s %s) cannot exceed Current (%s %s).",
                        line.product_id.display_name,
                        total_out_qty,
                        line.uom_id.display_name,
                        current_qty,
                        line.uom_id.display_name,
                    )
                )
        return True


class RestaurantStockDailyProductTotal(models.Model):
    _inherit = "restaurant.stock.daily.product.total"

    sold_qty = fields.Float(readonly=True, aggregator=None)
    manual_consumption_qty = fields.Float(readonly=True, aggregator=None)
    complimentary_qty = fields.Float(readonly=True, aggregator=None)
    total_consumption_qty = fields.Float(readonly=True, aggregator=None)
    manual_waste_qty = fields.Float(readonly=True, aggregator=None)
    prepared_cancelled_qty = fields.Float(readonly=True, aggregator=None)
    total_waste_qty = fields.Float(readonly=True, aggregator=None)
    missing_qty = fields.Float(readonly=True, aggregator=None)
    surplus_qty = fields.Float(readonly=True, aggregator=None)

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
                    SUM(COALESCE(line.consumption_qty, 0.0))
                        AS manual_consumption_qty,
                    SUM(COALESCE(line.sold_qty, 0.0)) AS sold_qty,
                    SUM(COALESCE(line.auto_complimentary_qty, 0.0))
                        AS complimentary_qty,
                    SUM(COALESCE(line.total_consumption_qty, 0.0))
                        AS total_consumption_qty,
                    SUM(COALESCE(line.total_consumption_qty, 0.0))
                        AS consumption_qty,
                    SUM(COALESCE(line.waste_qty, 0.0)) AS manual_waste_qty,
                    SUM(COALESCE(line.auto_cancellation_waste_qty, 0.0))
                        AS prepared_cancelled_qty,
                    SUM(COALESCE(line.total_waste_qty, 0.0))
                        AS total_waste_qty,
                    SUM(COALESCE(line.total_waste_qty, 0.0)) AS waste_qty,
                    SUM(COALESCE(line.damaged_qty, 0.0)) AS damaged_qty,
                    SUM(
                        COALESCE(line.opening_qty, 0.0)
                        + COALESCE(line.received_qty, 0.0)
                        - COALESCE(line.total_consumption_qty, 0.0)
                        - COALESCE(line.total_waste_qty, 0.0)
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
                        CASE
                            WHEN line.actual_closing_state = 'counted'
                                AND line.operation_mode != 'consumption'
                            THEN GREATEST(
                                COALESCE(line.opening_qty, 0.0)
                                + COALESCE(line.received_qty, 0.0)
                                - COALESCE(line.total_consumption_qty, 0.0)
                                - COALESCE(line.total_waste_qty, 0.0)
                                - COALESCE(line.damaged_qty, 0.0)
                                - COALESCE(line.actual_closing_qty, 0.0),
                                0.0
                            )
                            ELSE 0.0
                        END
                    ) AS missing_qty,
                    SUM(
                        CASE
                            WHEN line.actual_closing_state = 'counted'
                                AND line.operation_mode != 'consumption'
                            THEN GREATEST(
                                COALESCE(line.actual_closing_qty, 0.0)
                                - (
                                    COALESCE(line.opening_qty, 0.0)
                                    + COALESCE(line.received_qty, 0.0)
                                    - COALESCE(line.total_consumption_qty, 0.0)
                                    - COALESCE(line.total_waste_qty, 0.0)
                                    - COALESCE(line.damaged_qty, 0.0)
                                ),
                                0.0
                            )
                            ELSE 0.0
                        END
                    ) AS surplus_qty,
                    SUM(COALESCE(line.request_tomorrow_qty, 0.0))
                        AS request_tomorrow_qty,
                    SUM(
                        CASE
                            WHEN line.actual_closing_state = 'counted'
                            THEN COALESCE(line.actual_closing_qty, 0.0)
                                - (
                                    COALESCE(line.opening_qty, 0.0)
                                    + COALESCE(line.received_qty, 0.0)
                                    - COALESCE(line.total_consumption_qty, 0.0)
                                    - COALESCE(line.total_waste_qty, 0.0)
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

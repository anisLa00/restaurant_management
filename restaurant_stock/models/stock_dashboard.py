from odoo import api, fields, models

from .stock_security import (
    get_company_business_date,
    get_company_day_utc_range,
)

CENTRAL_STOREKEEPER_GROUP = (
    "restaurant_core.group_restaurant_central_storekeeper"
)

STOCKKEEPER_GROUP = (
    "restaurant_core.group_restaurant_stockkeeper"
)

MANAGER_GROUP = (
    "restaurant_core.group_restaurant_branch_manager"
)


class ProductTemplate(models.Model):
    _inherit = "product.template"

    restaurant_stock_location_name = fields.Char(
        string="Location",
        compute="_compute_restaurant_stock_dashboard",
    )

    restaurant_stock_opening = fields.Float(
        string="Opening",
        compute="_compute_restaurant_stock_dashboard",
    )

    restaurant_stock_received_today = fields.Float(
        string="Received Today",
        compute="_compute_restaurant_stock_dashboard",
    )

    restaurant_stock_consumption = fields.Float(
        string="Consumption",
        compute="_compute_restaurant_stock_dashboard",
    )

    restaurant_stock_consumption_mirdif = fields.Float(
        string="Mirdif",
        compute="_compute_restaurant_stock_dashboard",
    )

    restaurant_stock_consumption_jafilya = fields.Float(
        string="Jafilya",
        compute="_compute_restaurant_stock_dashboard",
    )

    restaurant_stock_consumption_global_village = fields.Float(
        string="Global Village",
        compute="_compute_restaurant_stock_dashboard",
    )

    restaurant_stock_waste = fields.Float(
        string="Waste",
        compute="_compute_restaurant_stock_dashboard",
    )

    restaurant_stock_damaged = fields.Float(
        string="Damaged",
        compute="_compute_restaurant_stock_dashboard",
    )

    restaurant_stock_on_hand = fields.Float(
        string="Current Stock",
        compute="_compute_restaurant_stock_dashboard",
    )

    restaurant_stock_reserved = fields.Float(
        string="Reserved",
        compute="_compute_restaurant_stock_dashboard",
    )

    restaurant_stock_available = fields.Float(
        string="Available",
        compute="_compute_restaurant_stock_dashboard",
    )

    def _restaurant_dashboard_branch(self):
        user = self.env.user

        # Central keeps its own dashboard source even if a user happens to
        # hold an additional branch role.
        if user.has_group(CENTRAL_STOREKEEPER_GROUP):
            return self.env["restaurant.branch"]

        if not (
            user.has_group(STOCKKEEPER_GROUP)
            or user.has_group(MANAGER_GROUP)
        ):
            return self.env["restaurant.branch"]

        branches = (
            self.env["restaurant.branch"]
            .sudo()
            .search([
                ("user_ids", "in", [user.id]),
                ("company_id", "=", self.env.company.id),
            ])
        )

        if len(branches) == 1:
            return branches

        return self.env["restaurant.branch"]

    def _restaurant_dashboard_location(self):
        user = self.env.user

        if user.has_group(CENTRAL_STOREKEEPER_GROUP):
            warehouse = self.env.ref(
                "stock.warehouse0",
                raise_if_not_found=False,
            )

            if warehouse:
                return warehouse.lot_stock_id

            return self.env["stock.location"]

        branch = self._restaurant_dashboard_branch()

        if branch and branch.warehouse_id:
            return branch.warehouse_id.lot_stock_id

        return self.env["stock.location"]

    def _restaurant_dashboard_effective_sheets(self, branch):
        """Return every confirmed sheet for the latest service date.

        Sectioned stock uses one Daily Stock sheet per operational area, so a
        single ``limit=1`` record is not a complete branch snapshot.  Legacy
        unsectioned sheets remain a fallback, but are ignored when sectioned
        sheets exist for the same date to prevent the migration overlap from
        being counted twice.
        """
        Daily = self.env["restaurant.stock.daily"].sudo()
        confirmed_domain = [
            ("branch_id", "=", branch.id),
            ("company_id", "=", branch.company_id.id),
            ("state", "in", ["opened", "closing_review", "closed"]),
        ]
        latest_sheet = Daily.search(
            confirmed_domain,
            order="stock_date desc, id desc",
            limit=1,
        )
        if not latest_sheet:
            return Daily.browse()

        effective_sheets = Daily.search(
            confirmed_domain + [("stock_date", "=", latest_sheet.stock_date)],
            order="section_id, id",
        )
        sectioned_sheets = effective_sheets.filtered("section_id")
        return sectioned_sheets or effective_sheets.filtered(
            lambda sheet: not sheet.section_id
        )

    @api.depends_context(
        "uid",
        "company",
        "allowed_company_ids",
        "location",
    )
    def _compute_restaurant_stock_dashboard(self):
        location = self._restaurant_dashboard_location()
        branch = self._restaurant_dashboard_branch()

        daily_by_product = {}
        central_consumption_by_product = {}

        # -------------------------
        # Branch Daily Stock values
        # -------------------------
        if branch:
            effective_sheets = self._restaurant_dashboard_effective_sheets(
                branch
            )

            # Current Stock follows the confirmed service-date snapshot.
            # A newer Draft is intentionally ignored until Opening is
            # confirmed; until then the prior closed service remains visible.
            for effective_sheet in effective_sheets:
                for line in effective_sheet.line_ids:
                    # Product/section ownership is unique in current data.
                    # Keep the first deterministic section line if malformed
                    # historical data contains the product more than once.
                    if line.product_id.id in daily_by_product:
                        continue
                    daily_by_product[line.product_id.id] = {
                        "opening": line.opening_qty,
                        "received": line.received_qty,
                        # Consumption becomes authoritative only after the
                        # manager confirms the service's final Close.
                        "consumption": (
                            line.consumption_qty
                            if effective_sheet.state == "closed"
                            else 0.0
                        ),
                        "waste": line.waste_qty,
                        "damaged": line.damaged_qty,
                    }

        # ---------------------------------------
        # Central dispatch consumption by branch
        # ---------------------------------------
        if self.env.user.has_group(CENTRAL_STOREKEEPER_GROUP) and location:
            company = self.env.company
            transit_location = company.internal_transit_location_id
            if transit_location:
                today = get_company_business_date(company)
                utc_start, utc_end = get_company_day_utc_range(company, today)
                moves = self.env["stock.move"].sudo().search([
                    ("state", "=", "done"),
                    ("company_id", "=", company.id),
                    ("picking_id.date_done", ">=", utc_start),
                    ("picking_id.date_done", "<", utc_end),
                    ("location_id", "child_of", location.id),
                    ("location_dest_id", "child_of", transit_location.id),
                ])

                picking_ids = moves.picking_id.ids
                branch_by_picking = {}
                if picking_ids:
                    requests = self.env["restaurant.stock.request"].sudo().search([
                        ("company_id", "=", company.id),
                        ("dispatch_picking_id", "in", picking_ids),
                    ])
                    distributions = self.env[
                        "restaurant.stock.distribution"
                    ].sudo().search([
                        ("company_id", "=", company.id),
                        ("dispatch_picking_id", "in", picking_ids),
                    ])
                    branch_by_picking.update({
                        request.dispatch_picking_id.id: request.branch_id
                        for request in requests
                    })
                    branch_by_picking.update({
                        distribution.dispatch_picking_id.id:
                            distribution.branch_id
                        for distribution in distributions
                    })

                for move in moves:
                    destination_branch = branch_by_picking.get(
                        move.picking_id.id
                    )
                    if not destination_branch:
                        continue
                    product = move.product_id
                    quantity = move.uom_id._compute_quantity(
                        move.quantity,
                        product.uom_id,
                        round=False,
                    )
                    product_values = central_consumption_by_product.setdefault(
                        product.id,
                        {},
                    )
                    branch_code = destination_branch.code
                    product_values[branch_code] = (
                        product_values.get(branch_code, 0.0) + quantity
                    )

        # -------------------------
        # Dashboard values
        # -------------------------
        for template in self:
            template.restaurant_stock_location_name = False

            template.restaurant_stock_opening = 0.0
            template.restaurant_stock_received_today = 0.0
            template.restaurant_stock_consumption = 0.0
            template.restaurant_stock_consumption_mirdif = 0.0
            template.restaurant_stock_consumption_jafilya = 0.0
            template.restaurant_stock_consumption_global_village = 0.0
            template.restaurant_stock_waste = 0.0
            template.restaurant_stock_damaged = 0.0

            template.restaurant_stock_on_hand = 0.0
            template.restaurant_stock_reserved = 0.0
            template.restaurant_stock_available = 0.0

            if not location:
                continue

            template.restaurant_stock_location_name = (
                location.complete_name
            )

            variants = (
                template
                .sudo()
                .product_variant_ids
            )

            opening = 0.0
            received_today = 0.0
            consumption = 0.0
            consumption_mirdif = 0.0
            consumption_jafilya = 0.0
            consumption_global_village = 0.0
            waste = 0.0
            damaged = 0.0

            on_hand = 0.0
            available = 0.0

            for variant in variants:
                # Live native stock
                fresh_variant = (
                    self.env["product.product"]
                    .sudo()
                    .with_context(
                        location=location.id,
                        compute_child=True,
                    )
                    .browse(variant.id)
                )

                on_hand += (
                    fresh_variant.qty_available
                )

                available += (
                    fresh_variant.free_qty
                )

                # Daily values
                daily_values = daily_by_product.get(
                    variant.id,
                    {},
                )

                central_consumption = central_consumption_by_product.get(
                    variant.id,
                    {},
                )

                opening += daily_values.get(
                    "opening",
                    0.0,
                )

                consumption += daily_values.get(
                    "consumption",
                    0.0,
                )

                if central_consumption:
                    consumption += sum(central_consumption.values())
                    consumption_mirdif += central_consumption.get("MIR", 0.0)
                    consumption_jafilya += central_consumption.get("JAF", 0.0)
                    consumption_global_village += central_consumption.get(
                        "GV",
                        0.0,
                    )

                waste += daily_values.get(
                    "waste",
                    0.0,
                )

                damaged += daily_values.get(
                    "damaged",
                    0.0,
                )

                received_today += daily_values.get(
                    "received",
                    0.0,
                )

            template.restaurant_stock_opening = opening

            template.restaurant_stock_received_today = (
                received_today
            )

            template.restaurant_stock_consumption = (
                consumption
            )

            template.restaurant_stock_consumption_mirdif = consumption_mirdif
            template.restaurant_stock_consumption_jafilya = consumption_jafilya
            template.restaurant_stock_consumption_global_village = (
                consumption_global_village
            )

            template.restaurant_stock_waste = waste
            template.restaurant_stock_damaged = damaged

            template.restaurant_stock_on_hand = on_hand
            template.restaurant_stock_available = available

            template.restaurant_stock_reserved = max(
                on_hand - available,
                0.0,
            )

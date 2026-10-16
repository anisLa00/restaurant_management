from datetime import timedelta

from odoo import api, fields, models
from odoo.exceptions import AccessError


OWNER_GROUP = "restaurant_core.group_restaurant_owner"


class RestaurantManagementDashboard(models.Model):
    _inherit = "restaurant.management.dashboard"

    is_owner = fields.Boolean(compute="_compute_management_role")
    management_title = fields.Char(compute="_compute_management_role")
    daily_closing_review_count = fields.Integer(compute="_compute_metrics")
    attendance_review_count = fields.Integer(compute="_compute_metrics")
    waiter_sales_review_count = fields.Integer(compute="_compute_metrics")
    service_event_review_count = fields.Integer(compute="_compute_metrics")
    daily_stock_review_count = fields.Integer(compute="_compute_metrics")
    processed_payroll_count = fields.Integer(compute="_compute_metrics")
    selected_day_sales = fields.Monetary(
        compute="_compute_metrics", currency_field="currency_id",
    )
    selected_week_sales = fields.Monetary(
        compute="_compute_metrics", currency_field="currency_id",
    )
    selected_month_sales = fields.Monetary(
        compute="_compute_metrics", currency_field="currency_id",
    )
    previous_month_sales = fields.Monetary(
        compute="_compute_metrics", currency_field="currency_id",
    )
    month_sales_change_percent = fields.Float(
        compute="_compute_metrics", digits=(16, 2),
    )
    month_sales_change_amount = fields.Monetary(
        compute="_compute_metrics", currency_field="currency_id",
    )
    month_sales_trend = fields.Selection(
        [
            ("up", "Up"),
            ("down", "Down"),
            ("same", "No Change"),
        ],
        compute="_compute_metrics",
    )

    def _require_management(self):
        if self.env.su or self.env.user.has_group("base.group_system"):
            return
        if not (
            self.env.user.has_group(OWNER_GROUP)
            or self.env.user.has_group(
                "restaurant_core.group_restaurant_operations_manager"
            )
        ):
            raise AccessError(
                self.env._("Only Operations or the Owner can use Management Control.")
            )

    @api.depends("user_id")
    def _compute_management_role(self):
        owner_group = self.env.ref(OWNER_GROUP)
        for dashboard in self:
            user = (dashboard.user_id or self.env.user).sudo()
            dashboard.is_owner = owner_group in user.all_group_ids or user._is_admin()
            dashboard.management_title = (
                self.env._("Owner Executive Control")
                if dashboard.is_owner
                else self.env._("Operations Control")
            )

    @api.depends("company_id", "date_from", "date_to")
    def _compute_metrics(self):
        super()._compute_metrics()
        owner_group = self.env.ref(OWNER_GROUP)
        for dashboard in self:
            company_domain = [("company_id", "=", dashboard.company_id.id)]
            anchor_date = fields.Date.to_date(dashboard.date_to)
            week_start = anchor_date - timedelta(days=anchor_date.weekday())
            week_end = week_start + timedelta(days=6)
            month_start = anchor_date.replace(day=1)
            next_month = (
                month_start.replace(day=28) + timedelta(days=4)
            ).replace(day=1)
            month_end = next_month - timedelta(days=1)
            previous_month_end = month_start - timedelta(days=1)
            previous_month_start = previous_month_end.replace(day=1)

            def sales_total(date_start, date_end):
                sales = self.env["restaurant.accounting.daily.sales"].sudo().search(
                    company_domain + [
                        ("business_date", ">=", date_start),
                        ("business_date", "<=", date_end),
                    ]
                )
                return sum(sales.mapped("total_sales"))

            dashboard.selected_day_sales = sales_total(anchor_date, anchor_date)
            dashboard.selected_week_sales = sales_total(week_start, week_end)
            dashboard.selected_month_sales = sales_total(month_start, month_end)
            dashboard.previous_month_sales = sales_total(
                previous_month_start, previous_month_end
            )
            dashboard.month_sales_change_amount = (
                dashboard.selected_month_sales - dashboard.previous_month_sales
            )
            if dashboard.previous_month_sales:
                dashboard.month_sales_change_percent = (
                    (
                        dashboard.selected_month_sales
                        - dashboard.previous_month_sales
                    )
                    / dashboard.previous_month_sales
                    * 100
                )
            else:
                dashboard.month_sales_change_percent = (
                    100.0 if dashboard.selected_month_sales else 0.0
                )
            if dashboard.month_sales_change_amount > 0:
                dashboard.month_sales_trend = "up"
            elif dashboard.month_sales_change_amount < 0:
                dashboard.month_sales_trend = "down"
            else:
                dashboard.month_sales_trend = "same"
            dashboard_user = (dashboard.user_id or self.env.user).sudo()
            is_owner = (
                owner_group in dashboard_user.all_group_ids
                or dashboard_user._is_admin()
            )
            dashboard.daily_closing_review_count = self.env[
                "restaurant.daily.closing"
            ].sudo().search_count(
                [
                    ("branch_id.company_id", "=", dashboard.company_id.id),
                    ("closing_date", ">=", dashboard.date_from),
                    ("closing_date", "<=", dashboard.date_to),
                ]
            )
            dashboard.attendance_review_count = self.env[
                "restaurant.attendance.sheet"
            ].sudo().search_count(company_domain + [
                ("attendance_date", ">=", dashboard.date_from),
                ("attendance_date", "<=", dashboard.date_to),
            ])
            dashboard.waiter_sales_review_count = self.env[
                "restaurant.waiter.sales.closing"
            ].sudo().search_count(
                [
                    ("branch_id.company_id", "=", dashboard.company_id.id),
                    ("business_date", ">=", dashboard.date_from),
                    ("business_date", "<=", dashboard.date_to),
                ]
            )
            dashboard.service_event_review_count = self.env[
                "restaurant.stock.event.sheet"
            ].sudo().search_count(
                company_domain + [
                    ("service_date", ">=", dashboard.date_from),
                    ("service_date", "<=", dashboard.date_to),
                ]
            )
            dashboard.daily_stock_review_count = self.env[
                "restaurant.stock.daily"
            ].sudo().search_count(
                [
                    ("branch_id.company_id", "=", dashboard.company_id.id),
                    ("stock_date", ">=", dashboard.date_from),
                    ("stock_date", "<=", dashboard.date_to),
                ]
            )
            dashboard.processed_payroll_count = (
                self.env["restaurant.payroll.input.batch"].sudo().search_count(
                    company_domain + [("state", "=", "processed")]
                )
                if is_owner
                else 0
            )
            if not is_owner:
                dashboard.purchase_spend = 0
                dashboard.supplier_invoice_pending_count = 0
                dashboard.supplier_invoice_pending_amount = 0
                dashboard.monthly_close_owner_count = 0
                dashboard.payroll_pending_count = 0
                dashboard.onboarding_active_count = 0
                dashboard.probation_active_count = 0
                dashboard.offboarding_active_count = 0
                dashboard.performance_pending_count = 0

    def _open_readonly_records(self, name, model, domain, views="list,form"):
        self.ensure_one()
        self._require_management()
        return {
            "type": "ir.actions.act_window",
            "name": name,
            "res_model": model,
            "view_mode": views,
            "domain": domain,
            "context": {"create": False, "edit": False, "delete": False},
        }

    def action_open_daily_closing_reviews(self):
        return self._open_readonly_records(
            self.env._("Daily Closings"),
            "restaurant.daily.closing",
            [
                ("branch_id.company_id", "=", self.company_id.id),
                ("closing_date", ">=", self.date_from),
                ("closing_date", "<=", self.date_to),
            ],
        )

    def action_open_attendance_reviews(self):
        return self._open_readonly_records(
            self.env._("Attendance"),
            "restaurant.attendance.sheet",
            [
                ("company_id", "=", self.company_id.id),
                ("attendance_date", ">=", self.date_from),
                ("attendance_date", "<=", self.date_to),
            ],
        )

    def action_open_waiter_sales_reviews(self):
        return self._open_readonly_records(
            self.env._("Waiter Sales & Tips"),
            "restaurant.waiter.sales.closing",
            [
                ("branch_id.company_id", "=", self.company_id.id),
                ("business_date", ">=", self.date_from),
                ("business_date", "<=", self.date_to),
            ],
        )

    def action_open_service_event_reviews(self):
        return self._open_readonly_records(
            self.env._("Cancellation / Complimentary"),
            "restaurant.stock.event.sheet",
            [
                ("company_id", "=", self.company_id.id),
                ("service_date", ">=", self.date_from),
                ("service_date", "<=", self.date_to),
            ],
        )

    def action_open_daily_stock_reviews(self):
        return self._open_readonly_records(
            self.env._("Daily Stock"),
            "restaurant.stock.daily",
            [
                ("branch_id.company_id", "=", self.company_id.id),
                ("stock_date", ">=", self.date_from),
                ("stock_date", "<=", self.date_to),
            ],
        )

    def action_open_processed_payroll(self):
        self.ensure_one()
        if not self.is_owner:
            return False
        return self._open_readonly_records(
            self.env._("Processed Payroll"),
            "restaurant.payroll.input.batch",
            [("company_id", "=", self.company_id.id), ("state", "=", "processed")],
        )

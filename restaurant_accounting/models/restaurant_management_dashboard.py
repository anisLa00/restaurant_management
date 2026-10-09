from datetime import timedelta

from odoo import api, fields, models
from odoo.exceptions import AccessError, ValidationError


OPERATIONS_GROUP = "restaurant_core.group_restaurant_operations_manager"
class RestaurantManagementDashboard(models.Model):
    _name = "restaurant.management.dashboard"
    _description = "Restaurant Operations and Owner Control"
    _rec_name = "company_id"

    user_id = fields.Many2one(
        "res.users", required=True, readonly=True, default=lambda self: self.env.user,
        ondelete="cascade", index=True,
    )
    company_id = fields.Many2one(
        "res.company", required=True, default=lambda self: self.env.company,
        ondelete="cascade", index=True,
    )
    currency_id = fields.Many2one(related="company_id.currency_id", readonly=True)
    date_from = fields.Date(
        required=True,
        default=lambda self: fields.Date.context_today(self) - timedelta(days=29),
    )
    date_to = fields.Date(required=True, default=fields.Date.context_today)
    last_refreshed_at = fields.Datetime(compute="_compute_metrics")

    total_sales = fields.Monetary(compute="_compute_metrics", currency_field="currency_id")
    total_tips = fields.Monetary(compute="_compute_metrics", currency_field="currency_id")
    daily_sales_pending_count = fields.Integer(compute="_compute_metrics")
    purchase_spend = fields.Monetary(compute="_compute_metrics", currency_field="currency_id")
    requisition_approval_count = fields.Integer(compute="_compute_metrics")
    stock_purchase_approval_count = fields.Integer(compute="_compute_metrics")
    purchase_operations_count = fields.Integer(compute="_compute_metrics")
    purchase_owner_count = fields.Integer(compute="_compute_metrics")
    supplier_invoice_pending_count = fields.Integer(compute="_compute_metrics")
    supplier_invoice_pending_amount = fields.Monetary(
        compute="_compute_metrics", currency_field="currency_id",
    )
    monthly_close_owner_count = fields.Integer(compute="_compute_metrics")
    payroll_pending_count = fields.Integer(compute="_compute_metrics")
    active_employee_count = fields.Integer(compute="_compute_metrics")
    onboarding_active_count = fields.Integer(compute="_compute_metrics")
    probation_active_count = fields.Integer(compute="_compute_metrics")
    offboarding_active_count = fields.Integer(compute="_compute_metrics")
    performance_pending_count = fields.Integer(compute="_compute_metrics")

    _user_company_unique = models.Constraint(
        "UNIQUE(user_id, company_id)",
        "Each manager has one control dashboard per company.",
    )

    def _require_management(self):
        if self.env.su or self.env.user.has_group("base.group_system"):
            return
        if not self.env.user.has_group(OPERATIONS_GROUP):
            raise AccessError(self.env._("Only Operations or the Owner can use Management Control."))

    @api.constrains("date_from", "date_to")
    def _check_dates(self):
        if any(record.date_from and record.date_to and record.date_to < record.date_from for record in self):
            raise ValidationError(self.env._("The end date cannot be before the start date."))

    @api.model_create_multi
    def create(self, vals_list):
        self._require_management()
        prepared = []
        for vals in vals_list:
            values = dict(vals)
            company = self.env["res.company"].browse(
                values.get("company_id", self.env.company.id)
            )
            if company not in self.env.companies:
                raise AccessError(self.env._("Switch to the selected company first."))
            if values.get("user_id", self.env.uid) != self.env.uid and not self.env.su:
                raise AccessError(self.env._("You cannot create another user's dashboard."))
            values.update({"user_id": self.env.uid, "company_id": company.id})
            prepared.append(values)
        return super().create(prepared)

    def write(self, vals):
        self._require_management()
        if set(vals) - {"company_id", "date_from", "date_to"}:
            raise AccessError(self.env._("Only the dashboard company and date range can be changed."))
        if "company_id" in vals:
            company = self.env["res.company"].browse(vals["company_id"])
            if company not in self.env.companies:
                raise AccessError(self.env._("Switch to the selected company first."))
        return super().write(vals)

    def unlink(self):
        raise AccessError(self.env._("Management Control dashboards are retained for each manager."))

    @api.depends("company_id", "date_from", "date_to")
    def _compute_metrics(self):
        DashboardEnv = self.env
        for dashboard in self:
            company_domain = [("company_id", "=", dashboard.company_id.id)]
            date_domain = [
                ("business_date", ">=", dashboard.date_from),
                ("business_date", "<=", dashboard.date_to),
            ]
            sales = DashboardEnv["restaurant.accounting.daily.sales"].sudo().search(
                company_domain + date_domain
            )
            dashboard.total_sales = sum(sales.mapped("total_sales"))
            dashboard.total_tips = sum(sales.mapped("total_tips"))
            dashboard.daily_sales_pending_count = len(
                sales.filtered(lambda record: record.state != "posted")
            )

            purchase_orders = DashboardEnv["purchase.order"].sudo().search(company_domain + [
                ("state", "in", ("purchase", "done")),
                ("date_order", ">=", fields.Datetime.to_datetime(dashboard.date_from)),
                ("date_order", "<", fields.Datetime.to_datetime(dashboard.date_to + timedelta(days=1))),
            ])
            dashboard.purchase_spend = sum(
                order.currency_id._convert(
                    order.amount_total,
                    dashboard.company_id.currency_id,
                    dashboard.company_id,
                    fields.Date.to_date(order.date_order),
                )
                for order in purchase_orders
            )

            requisitions = DashboardEnv["restaurant.purchase.requisition"].sudo()
            stock_requests = DashboardEnv["restaurant.stock.request"].sudo()
            requisition_operations = requisitions.search_count(
                company_domain + [("state", "=", "awaiting_operations")]
            )
            requisition_owner = requisitions.search_count(
                company_domain + [("state", "=", "awaiting_owner")]
            )
            stock_operations = stock_requests.search_count(
                company_domain + [("state", "=", "awaiting_operations")]
            )
            stock_owner = stock_requests.search_count(
                company_domain + [("state", "=", "awaiting_owner")]
            )
            dashboard.requisition_approval_count = requisition_operations + requisition_owner
            dashboard.stock_purchase_approval_count = stock_operations + stock_owner
            dashboard.purchase_operations_count = requisition_operations + stock_operations
            dashboard.purchase_owner_count = requisition_owner + stock_owner

            handoffs = DashboardEnv["restaurant.purchase.invoice.handoff"].sudo().search(
                company_domain + [
                    ("invoice_date", ">=", dashboard.date_from),
                    ("invoice_date", "<=", dashboard.date_to),
                    ("state", "not in", ("paid", "cancelled")),
                ]
            )
            dashboard.supplier_invoice_pending_count = len(handoffs)
            dashboard.supplier_invoice_pending_amount = sum(
                handoff.currency_id._convert(
                    handoff.invoice_total,
                    dashboard.company_id.currency_id,
                    dashboard.company_id,
                    handoff.invoice_date,
                )
                for handoff in handoffs
            )
            dashboard.monthly_close_owner_count = DashboardEnv[
                "restaurant.accounting.period.close"
            ].sudo().search_count(company_domain + [("state", "=", "awaiting_owner")])
            dashboard.payroll_pending_count = DashboardEnv[
                "restaurant.payroll.input.batch"
            ].sudo().search_count(company_domain + [("state", "!=", "processed")])

            dashboard.active_employee_count = DashboardEnv["hr.employee"].sudo().search_count(
                company_domain + [("active", "=", True)]
            )
            dashboard.onboarding_active_count = DashboardEnv[
                "restaurant.employee.onboarding"
            ].sudo().search_count(company_domain + [("state", "in", ("in_progress", "extended"))])
            dashboard.probation_active_count = DashboardEnv[
                "restaurant.employee.probation"
            ].sudo().search_count(company_domain + [("state", "in", ("active", "extended", "hr_review"))])
            dashboard.offboarding_active_count = DashboardEnv[
                "restaurant.employee.offboarding"
            ].sudo().search_count(company_domain + [("state", "in", ("draft", "in_progress"))])
            dashboard.performance_pending_count = DashboardEnv[
                "restaurant.employee.performance.review"
            ].sudo().search_count(company_domain + [("state", "in", ("manager_review", "hr_review"))])
            dashboard.last_refreshed_at = fields.Datetime.now()

    @api.model
    def action_open_dashboard(self):
        self._require_management()
        dashboard = self.search([
            ("user_id", "=", self.env.uid),
            ("company_id", "=", self.env.company.id),
        ], limit=1)
        if not dashboard:
            dashboard = self.create({"company_id": self.env.company.id})
        return {
            "type": "ir.actions.act_window",
            "name": self.env._("Management Control"),
            "res_model": self._name,
            "res_id": dashboard.id,
            "view_mode": "form",
            "target": "current",
        }

    def _open_records(self, name, model, domain, views="list,form"):
        self.ensure_one()
        self._require_management()
        return {
            "type": "ir.actions.act_window",
            "name": name,
            "res_model": model,
            "view_mode": views,
            "domain": domain,
            "context": {"create": False},
        }

    def action_open_requisition_approvals(self):
        return self._open_records(
            self.env._("Purchase Requisition Approvals"),
            "restaurant.purchase.requisition",
            [("company_id", "=", self.company_id.id),
             ("state", "in", ("awaiting_operations", "awaiting_owner"))],
        )

    def action_open_stock_purchase_approvals(self):
        return self._open_records(
            self.env._("Stock Purchase Approvals"),
            "restaurant.stock.request",
            [("company_id", "=", self.company_id.id),
             ("state", "in", ("awaiting_operations", "awaiting_owner"))],
        )

    def action_open_supplier_invoices(self):
        return self._open_records(
            self.env._("Supplier Invoices Requiring Action"),
            "restaurant.purchase.invoice.handoff",
            [("company_id", "=", self.company_id.id),
             ("state", "not in", ("paid", "cancelled"))],
        )

    def action_open_daily_sales(self):
        return self._open_records(
            self.env._("Daily Sales Accounting"),
            "restaurant.accounting.daily.sales",
            [("company_id", "=", self.company_id.id),
             ("business_date", ">=", self.date_from),
             ("business_date", "<=", self.date_to)],
        )

    def action_open_monthly_closes(self):
        return self._open_records(
            self.env._("Monthly Accounting Close"),
            "restaurant.accounting.period.close",
            [("company_id", "=", self.company_id.id)],
        )

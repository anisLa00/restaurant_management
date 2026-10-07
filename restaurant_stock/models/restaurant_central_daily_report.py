from odoo import api, fields, models

from .stock_security import (
    CENTRAL_STOREKEEPER_GROUP,
    get_company_business_date,
    require_role,
)


class RestaurantCentralDailyReport(models.TransientModel):
    _name = "restaurant.stock.central.daily.report"
    _description = "Central Branch Daily Stock Report"

    service_date = fields.Date(
        string="Service Date",
        required=True,
        default=lambda self: get_company_business_date(self.env.company),
    )
    company_id = fields.Many2one(
        "res.company",
        required=True,
        readonly=True,
        default=lambda self: self.env.company,
    )
    detail_line_ids = fields.Many2many(
        "restaurant.stock.daily.line",
        string="Branch Daily Stock",
        compute="_compute_report_rows",
    )
    product_total_ids = fields.Many2many(
        "restaurant.stock.daily.product.total",
        string="Daily Product Totals",
        compute="_compute_report_rows",
    )

    @api.depends("service_date", "company_id")
    def _compute_report_rows(self):
        Detail = self.env["restaurant.stock.daily.line"]
        Total = self.env["restaurant.stock.daily.product.total"]

        for report in self:
            if not report.service_date or not report.company_id:
                report.detail_line_ids = Detail
                report.product_total_ids = Total
                continue

            report.detail_line_ids = Detail.search([
                ("company_id", "=", report.company_id.id),
                ("stock_date", "=", report.service_date),
            ], order="branch_id, product_id")
            report.product_total_ids = Total.search([
                ("company_id", "=", report.company_id.id),
                ("stock_date", "=", report.service_date),
            ], order="product_id, uom_id")

    @api.model
    def action_open_report(self):
        require_role(self.env, CENTRAL_STOREKEEPER_GROUP)
        report = self.create({})
        return {
            "type": "ir.actions.act_window",
            "name": self.env._("Branch Daily Stock"),
            "res_model": self._name,
            "res_id": report.id,
            "view_mode": "form",
            "view_id": self.env.ref(
                "restaurant_stock.restaurant_central_daily_report_view_form"
            ).id,
            "target": "current",
            "context": {
                "form_view_initial_mode": "edit",
            },
        }


from odoo import api, models


class ProductTemplate(models.Model):
    _inherit = "product.template"

    @api.depends_context(
        "uid",
        "company",
        "allowed_company_ids",
        "location",
    )
    def _compute_restaurant_stock_dashboard(self):
        super()._compute_restaurant_stock_dashboard()

        branch = self._restaurant_dashboard_branch()
        if not branch:
            return

        effective_sheet = self.env["restaurant.stock.daily"].sudo().search(
            [
                ("branch_id", "=", branch.id),
                ("company_id", "=", branch.company_id.id),
                ("state", "in", ["opened", "closing_review", "closed"]),
            ],
            order="stock_date desc, id desc",
            limit=1,
        )
        lines_by_product = {
            line.product_id.id: line
            for line in effective_sheet.line_ids
        } if effective_sheet else {}

        for template in self:
            total_consumption = 0.0
            total_waste = 0.0
            for variant in template.sudo().product_variant_ids:
                line = lines_by_product.get(variant.id)
                if not line:
                    continue
                if effective_sheet.state == "closed":
                    total_consumption += line.total_consumption_qty
                total_waste += line.total_waste_qty
            template.restaurant_stock_consumption = total_consumption
            template.restaurant_stock_waste = total_waste

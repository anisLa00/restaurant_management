from odoo import api, fields, models


class RestaurantBranch(models.Model):
    _inherit = "restaurant.branch"

    warehouse_id = fields.Many2one(
        "stock.warehouse",
        string="Warehouse",
        ondelete="restrict",
        groups="base.group_system",
        help="Odoo warehouse linked to this restaurant branch.",
    )

    @api.model
    def _setup_restaurant_warehouses(self):
        central = self.env.ref(
            "stock.warehouse0",
            raise_if_not_found=False,
        )

        if central and central.name != "Central Warehouse":
            central.sudo().write({
                "name": "Central Warehouse",
            })

        mappings = {
            "restaurant_core.branch_mirdif":
                "restaurant_stock.warehouse_mirdif",

            "restaurant_core.branch_jafilya":
                "restaurant_stock.warehouse_jafilya",

            "restaurant_core.branch_global_village":
                "restaurant_stock.warehouse_global_village",
        }

        for branch_xmlid, warehouse_xmlid in mappings.items():
            branch = self.env.ref(
                branch_xmlid,
                raise_if_not_found=False,
            )

            warehouse = self.env.ref(
                warehouse_xmlid,
                raise_if_not_found=False,
            )

            if branch and warehouse and branch.warehouse_id != warehouse:
                branch.sudo().write({
                    "warehouse_id": warehouse.id,
                })

        return True
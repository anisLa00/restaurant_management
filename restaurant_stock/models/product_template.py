from odoo import api, fields, models
from odoo.exceptions import AccessError


CENTRAL_STOREKEEPER_GROUP = (
    "restaurant_core.group_restaurant_central_storekeeper"
)


class ProductTemplate(models.Model):
    _inherit = "product.template"

    def _restaurant_require_approved_category(self, category_id):
        category = self.env["product.category"].browse(category_id).exists()
        if not category or not category.is_restaurant_stock_category:
            raise AccessError(
                self.env._(
                    "Choose one of the approved Restaurant Product Categories."
                )
            )
        return category

    def _restaurant_storekeeper_allowed_fields(self):
        return {
            "name",
            "categ_id",
            "uom_id",
            "barcode",
            "default_code",
            "active",
            "purchase_ok",
        }

    @api.model_create_multi
    def create(self, vals_list):
        if (
            not self.env.su
            and self.env.user.has_group(CENTRAL_STOREKEEPER_GROUP)
        ):
            allowed = self._restaurant_storekeeper_allowed_fields()

            prepared = []

            for vals in vals_list:
                vals = dict(vals)

                forbidden = set(vals) - allowed

                if forbidden:
                    raise AccessError(
                        self.env._(
                            "Central Storekeepers may only create "
                            "restaurant inventory product information."
                        )
                    )

                self._restaurant_require_approved_category(
                    vals.get("categ_id")
                )
                vals.setdefault("purchase_ok", True)
                vals["type"] = "consu"
                vals["is_storable"] = True

                prepared.append(vals)

            # Controlled sudo:
            # needed because Odoo automatically creates product.product
            # variants when creating a product.template.
            records = super(
                ProductTemplate,
                self.sudo(),
            ).create(prepared)

            return records.with_user(self.env.user)

        return super().create(vals_list)

    def write(self, vals):
        if (
            not self.env.su
            and self.env.user.has_group(CENTRAL_STOREKEEPER_GROUP)
        ):
            allowed = self._restaurant_storekeeper_allowed_fields()

            if set(vals) - allowed:
                raise AccessError(
                    self.env._(
                        "Central Storekeepers may only edit "
                        "restaurant inventory product information."
                    )
                )

            if "categ_id" in vals:
                self._restaurant_require_approved_category(vals["categ_id"])

            # Some template changes are propagated by Odoo to
            # product.product variants, so use controlled sudo
            # after validating the allowed fields.
            return super(
                ProductTemplate,
                self.sudo(),
            ).write(vals)

        return super().write(vals)

    def unlink(self):
        if (
            not self.env.su
            and self.env.user.has_group(CENTRAL_STOREKEEPER_GROUP)
        ):
            raise AccessError(
                self.env._(
                    "Central Storekeepers cannot permanently delete products. "
                    "Archive the product instead."
                )
            )

        return super().unlink()


class ProductCategory(models.Model):
    _inherit = "product.category"

    is_restaurant_stock_category = fields.Boolean(
        string="Restaurant Product Category",
        default=False,
        index=True,
        help=(
            "Marks the fixed native Product Categories available in the "
            "Central Stock product form. Daily Stock automatically groups "
            "these categories into Kitchen, Bar, or Disposable."
        ),
    )

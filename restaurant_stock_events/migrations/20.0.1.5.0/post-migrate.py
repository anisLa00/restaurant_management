from odoo import SUPERUSER_ID, api
from odoo.tools import float_compare


def _profile_values_from_item(item):
    mappings = item.variant_ids.mapping_ids
    products = mappings.stock_product_id
    if len(products) != 1:
        return None

    product = products[0]
    values = {
        "name": item.name,
        "branch_id": item.branch_id.id,
        "category_id": item.category_id.id,
        "stock_product_id": product.id,
        "stock_uom_id": product.uom_id.id,
        "size_applicable": item.size_applicable,
    }
    field_by_size = {
        "standard": "standard_stock_qty",
        "half": "half_stock_qty",
        "full": "full_stock_qty",
    }
    for variant in item.variant_ids:
        mapping = variant.mapping_ids.filtered(
            lambda current: current.stock_product_id == product
        )
        if len(mapping) == 1:
            values[field_by_size[variant.size]] = (
                mapping.stock_uom_id._compute_quantity(
                    mapping.stock_qty,
                    product.uom_id,
                    round=False,
                )
            )
    return values


def migrate(cr, version):
    """Expose valid legacy mappings in the integrated form without guessing."""
    env = api.Environment(cr, SUPERUSER_ID, {"menu_setup_migration": True})
    Profile = env["restaurant.menu.mapping.profile"]
    ProfileLine = env["restaurant.menu.mapping.profile.line"]

    for profile in Profile.search([]):
        categories = profile.item_ids.category_id
        if not profile.category_id and len(categories) == 1:
            profile.write({"category_id": categories.id})
        profile._adopt_integrated_values_from_existing_lines()

    legacy_items = env["restaurant.menu.item"].search([
        ("mapping_profile_id", "=", False),
    ])
    for item in legacy_items:
        values = _profile_values_from_item(item)
        if not values:
            continue
        profile = Profile.search([
            ("branch_id", "=", item.branch_id.id),
            ("name", "=", item.name),
        ], limit=1)
        if profile and (
            profile.stock_product_id
            != item.variant_ids.mapping_ids.stock_product_id
            or (profile.category_id and profile.category_id != item.category_id)
        ):
            continue
        if not profile:
            profile = Profile.create(values)

        lines_by_size = {line.size: line for line in profile.line_ids}
        field_by_size = {
            "standard": "standard_stock_qty",
            "half": "half_stock_qty",
            "full": "full_stock_qty",
        }
        for size, field_name in field_by_size.items():
            quantity = values.get(field_name, 0)
            if quantity <= 0 or size in lines_by_size:
                continue
            lines_by_size[size] = ProfileLine.create({
                "profile_id": profile.id,
                "size": size,
                "stock_qty": quantity,
                "stock_uom_id": profile.stock_uom_id.id,
                "note": "Derived from an existing verified menu mapping.",
            })

        item.with_context(menu_base_sync_deferred=True).write({
            "mapping_profile_id": profile.id,
        })
        for variant in item.variant_ids:
            line = lines_by_size.get(variant.size)
            mapping = variant.mapping_ids.filtered(
                lambda current: current.stock_product_id == profile.stock_product_id
            )
            if line and len(mapping) == 1 and not mapping.source_profile_line_id:
                converted = mapping.stock_uom_id._compute_quantity(
                    mapping.stock_qty,
                    line.stock_uom_id,
                    round=False,
                )
                if float_compare(
                    converted,
                    line.stock_qty,
                    precision_digits=12,
                ) == 0:
                    mapping.source_profile_line_id = line

import logging

from odoo import SUPERUSER_ID, api


_logger = logging.getLogger(__name__)


def _unlink_obsolete_menu_ui(env):
    xmlids = (
        "restaurant_stock_events.restaurant_menu_category_action",
        "restaurant_stock_events.restaurant_menu_item_action",
        "restaurant_stock_events.restaurant_menu_variant_action",
        "restaurant_stock_events.restaurant_central_mapping_action",
        "restaurant_stock_events.restaurant_menu_mapping_profile_apply_action",
        "restaurant_stock_events.restaurant_menu_category_view_list",
        "restaurant_stock_events.restaurant_menu_category_view_form",
        "restaurant_stock_events.restaurant_menu_item_view_list",
        "restaurant_stock_events.restaurant_menu_item_view_form",
        "restaurant_stock_events.restaurant_menu_variant_view_list",
        "restaurant_stock_events.restaurant_menu_variant_view_form",
        "restaurant_stock_events.restaurant_central_mapping_variant_view_list",
        "restaurant_stock_events.restaurant_central_mapping_variant_view_form",
        "restaurant_stock_events.restaurant_menu_mapping_profile_apply_wizard_view_form",
    )
    for xmlid in xmlids:
        record = env.ref(xmlid, raise_if_not_found=False)
        if record:
            record.unlink()


def migrate(cr, version):
    """Move to one Menu Setup UI and archive the requested GV test setup.

    Business and stock records are never deleted. The existing Global Village
    configuration is archived so the manager starts with a clean selector, and
    can be recovered by reactivating the preserved records.
    """
    env = api.Environment(cr, SUPERUSER_ID, {"menu_setup_migration": True})

    mappings = env["restaurant.menu.stock.mapping"].search([
        ("setup_id", "=", False),
        ("source_profile_line_id", "!=", False),
    ])
    for mapping in mappings:
        mapping.setup_id = mapping.source_profile_line_id.profile_id

    branch = env["restaurant.branch"].search([("code", "=", "GV")], limit=1)
    if branch:
        Setup = env["restaurant.menu.mapping.profile"].with_context(
            active_test=False,
        )
        Category = env["restaurant.menu.category"].with_context(active_test=False)
        Item = env["restaurant.menu.item"].with_context(active_test=False)
        Variant = env["restaurant.menu.variant"].with_context(active_test=False)

        setups = Setup.search([("branch_id", "=", branch.id)])
        categories = Category.search([("branch_id", "=", branch.id)])
        items = Item.search([("branch_id", "=", branch.id)])
        variants = Variant.search([("branch_id", "=", branch.id)])
        menu_mappings = env["restaurant.menu.stock.mapping"].search([
            ("branch_id", "=", branch.id),
        ])
        event_refs = env["restaurant.stock.event"].search_count([
            ("variant_id", "in", variants.ids),
        ])
        legacy_sale_refs = env["restaurant.stock.daily.sale"].search_count([
            ("variant_id", "in", variants.ids),
        ])
        reception_sale_refs = env[
            "restaurant.reception.sold.menu.line"
        ].search_count([("variant_id", "in", variants.ids)])

        setups.write({"active": False})
        items.with_context(menu_base_sync_deferred=True).write({"active": False})
        variants.write({"active": False})
        categories.write({"active": False})

        _logger.info(
            "Archived Global Village legacy menu configuration for a clean "
            "Menu Setup start: %s setups, %s categories, %s flavors, %s "
            "variants, %s mappings. Preserved references: %s service events, "
            "%s legacy Daily Stock sales, %s Reception sold lines.",
            len(setups),
            len(categories),
            len(items),
            len(variants),
            len(menu_mappings),
            event_refs,
            legacy_sale_refs,
            reception_sale_refs,
        )

    _unlink_obsolete_menu_ui(env)

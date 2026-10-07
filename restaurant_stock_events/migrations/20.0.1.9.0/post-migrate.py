import logging

from odoo import Command, SUPERUSER_ID, api


_logger = logging.getLogger(__name__)


def migrate(cr, version):
    """Backfill the reusable flavor catalog without replacing menu history."""
    env = api.Environment(cr, SUPERUSER_ID, {
        "active_test": False,
        "menu_setup_migration": True,
        "menu_base_sync_deferred": True,
    })
    Flavor = env["restaurant.menu.flavor"]
    Item = env["restaurant.menu.item"]
    Setup = env["restaurant.menu.mapping.profile"]

    created_flavors = Flavor.browse()
    linked_items = Item.browse()
    skipped_items = Item.browse()

    for item in Item.search([("flavor_id", "=", False)], order="id"):
        if not item.branch_id or not item.name:
            skipped_items |= item
            continue
        flavor = Flavor.search([
            ("company_id", "=", item.company_id.id),
            ("branch_id", "=", item.branch_id.id),
            ("name", "=", item.name),
        ], order="active desc, id", limit=1)
        if not flavor:
            flavor = Flavor.create({
                "name": item.name,
                "branch_id": item.branch_id.id,
            })
            created_flavors |= flavor
        item.write({"flavor_id": flavor.id})
        linked_items |= item

    linked_setups = Setup.browse()
    plain_setups = Setup.browse()
    for setup in Setup.search([], order="id"):
        items = Item.search([("mapping_profile_id", "=", setup.id)])
        selected_items = items.filtered("active") if setup.active else items
        flavors = selected_items.flavor_id
        if setup.active and not flavors:
            flavors = setup._plain_flavor(setup.branch_id)
            plain_setups |= setup
        if flavors:
            setup.write({"flavor_ids": [Command.set(flavors.ids)]})
            linked_setups |= setup

    active_setups = Setup.search([("active", "=", True)])
    active_setups._sync_menu_items()

    _logger.info(
        "Reusable flavor catalog migration: %s catalog flavors created, "
        "%s menu items linked, %s Menu Setups linked, %s active empty setups "
        "defaulted to Plain, %s legacy items skipped because branch or name "
        "was missing.",
        len(created_flavors),
        len(linked_items),
        len(linked_setups),
        len(plain_setups),
        len(skipped_items),
    )

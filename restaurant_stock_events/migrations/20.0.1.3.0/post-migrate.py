import logging

from odoo import SUPERUSER_ID, api


_logger = logging.getLogger(__name__)


def migrate(cr, version):
    """Assign only the confirmed legacy live catalog to Global Village.

    Other branchless legacy rows stay untouched so an upgrade never guesses
    their ownership or duplicates menu content between branches.
    """
    env = api.Environment(cr, SUPERUSER_ID, {})
    global_village = env.ref(
        "restaurant_core.branch_global_village",
        raise_if_not_found=False,
    )
    if not global_village:
        _logger.warning(
            "Global Village branch was not found; legacy menu catalog was not assigned."
        )
        return

    legacy_categories = env["restaurant.menu.category"].with_context(
        active_test=False,
    ).search([
        ("company_id", "=", global_village.company_id.id),
        ("branch_id", "=", False),
    ])
    confirmed_names = {"main course", "refrechments"}
    confirmed_categories = legacy_categories.filtered(
        lambda category: (category.name or "").strip().casefold()
        in confirmed_names
    )
    if confirmed_categories:
        confirmed_categories.write({"branch_id": global_village.id})
        _logger.info(
            "Assigned confirmed legacy menu categories %s to Global Village.",
            confirmed_categories.mapped("display_name"),
        )

from odoo import SUPERUSER_ID, api


def migrate(cr, version):
    """Enable category groups without rewriting confirmed stock history."""
    env = api.Environment(cr, SUPERUSER_ID, {})
    branches = env["restaurant.branch"].search([])
    env["restaurant.stock.section"]._ensure_category_sections(branches)

    # Open legacy sheets remain one auditable sheet. Add only absent catalog
    # rows at zero; quantities, moves, counts, and closed sheets stay untouched.
    active_legacy = env["restaurant.stock.daily"].search([
        ("section_id", "=", False),
        ("state", "in", ["draft", "opened", "closing_review"]),
    ])
    active_legacy._ensure_catalog_lines()

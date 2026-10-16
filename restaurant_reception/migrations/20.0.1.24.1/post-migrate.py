from odoo import SUPERUSER_ID, api


def migrate(cr, version):
    env = api.Environment(cr, SUPERUSER_ID, {})
    open_sheets = env["restaurant.waiter.sales.closing"].sudo().search([
        ("state", "in", ["draft", "correction"]),
    ])
    open_sheets._rebuild_summaries()

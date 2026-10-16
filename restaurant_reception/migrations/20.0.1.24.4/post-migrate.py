from odoo import SUPERUSER_ID, api


def migrate(cr, version):
    """Enable order entry only on empty open waiter rows.

    Existing rows with daily figures keep their historical Daily Total meaning;
    those figures are never guessed into individual orders.
    """
    env = api.Environment(cr, SUPERUSER_ID, {})
    open_sheets = env["restaurant.waiter.sales.closing"].search([
        ("state", "in", ("draft", "correction")),
    ])
    if not open_sheets:
        return

    open_sheets._sync_daily_rows()
    empty_waiter_rows = open_sheets.bill_ids.filtered(
        lambda line: line.is_auto_waiter
        and line.entry_mode == "daily_total"
        and not line.order_ids
        and not any((
            line.bill_count,
            line.table_count,
            line.guest_count,
            line.bill_amount,
            line.tip_total,
            line.review_count,
        ))
    )
    if empty_waiter_rows:
        cr.execute(
            """
            UPDATE restaurant_waiter_sales_bill
               SET entry_mode = 'order_details'
             WHERE id = ANY(%s)
            """,
            [empty_waiter_rows.ids],
        )
        empty_waiter_rows.invalidate_recordset(["entry_mode"])
        empty_waiter_rows.modified(["entry_mode"])
    open_sheets._rebuild_summaries()

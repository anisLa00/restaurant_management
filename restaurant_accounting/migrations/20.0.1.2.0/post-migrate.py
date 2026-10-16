from odoo import SUPERUSER_ID, api


def _reconstructed_expected_quantity(line):
    """Roll the native PO billing ledger back to the handoff creation time."""
    purchase_line = line.purchase_line_id
    handoff = line.handoff_id
    if not purchase_line or not handoff.create_date:
        return None

    quantity = purchase_line.qty_to_invoice
    later_invoice_lines = purchase_line.invoice_lines.filtered(
        lambda invoice_line: (
            invoice_line.move_id.state != "cancel"
            and invoice_line.move_id.move_type in ("in_invoice", "in_refund")
            and invoice_line.move_id.create_date >= handoff.create_date
            and invoice_line.product_uom_id
        )
    )
    for invoice_line in later_invoice_lines:
        converted = invoice_line.product_uom_id._compute_quantity(
            invoice_line.quantity,
            purchase_line.uom_id,
        )
        quantity += (
            converted
            if invoice_line.move_id.move_type == "in_invoice"
            else -converted
        )
    return quantity


def migrate(cr, version):
    """Backfill stable baselines and recompute native-document projections."""
    env = api.Environment(cr, SUPERUSER_ID, {})
    Line = env["restaurant.purchase.invoice.handoff.line"].sudo()
    lines = Line.search([])
    for line in lines:
        expected = _reconstructed_expected_quantity(line)
        purchase_line = line.purchase_line_id
        if expected is None or not purchase_line:
            values = (
                line.expected_quantity or 0,
                line.purchase_unit_price or 0,
                "uncertain",
                "Historical PO evidence was insufficient; Accounting review is required.",
                line.id,
            )
        else:
            values = (
                expected,
                purchase_line.price_unit,
                "reconstructed",
                "Reconstructed from the approved PO and its native billing ledger.",
                line.id,
            )
        cr.execute("""
            UPDATE restaurant_purchase_invoice_handoff_line
               SET expected_quantity = %s,
                   purchase_unit_price = %s,
                   baseline_status = %s,
                   baseline_note = %s
             WHERE id = %s
        """, values)

    lines.invalidate_recordset([
        "expected_quantity", "purchase_unit_price", "baseline_status",
        "baseline_note",
    ])

    handoffs = env["restaurant.purchase.invoice.handoff"].sudo().search([])
    handoffs = handoffs._workflow_recordset()
    handoffs.invalidate_recordset()
    if handoffs:
        handoffs._compute_state()
        handoffs._compute_match()
        handoffs.flush_recordset([
            "state", "bill_link_issue", "match_state", "purchase_total",
            "invoice_total", "variance_amount",
        ])

    daily_sales = env["restaurant.accounting.daily.sales"].sudo().search([])
    daily_sales = daily_sales._workflow_recordset()
    daily_sales.invalidate_recordset()
    if daily_sales:
        daily_sales._compute_state()
        daily_sales.flush_recordset(["state", "journal_link_issue"])

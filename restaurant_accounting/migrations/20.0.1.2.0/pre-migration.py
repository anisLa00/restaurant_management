def migrate(cr, version):
    """Preserve approval states before the public states become computed."""
    cr.execute("""
        ALTER TABLE restaurant_purchase_invoice_handoff
        ADD COLUMN IF NOT EXISTS workflow_state varchar
    """)
    cr.execute("""
        UPDATE restaurant_purchase_invoice_handoff
           SET workflow_state = CASE state
               WHEN 'posted' THEN 'bill_draft'
               WHEN 'paid' THEN 'bill_draft'
               ELSE state
           END
         WHERE workflow_state IS NULL
    """)

    cr.execute("""
        ALTER TABLE restaurant_accounting_daily_sales
        ADD COLUMN IF NOT EXISTS workflow_state varchar
    """)
    cr.execute("""
        UPDATE restaurant_accounting_daily_sales
           SET workflow_state = CASE state
               WHEN 'posted' THEN 'draft_entry'
               ELSE state
           END
         WHERE workflow_state IS NULL
    """)

    cr.execute("""
        ALTER TABLE restaurant_purchase_invoice_handoff_line
        ADD COLUMN IF NOT EXISTS expected_quantity double precision,
        ADD COLUMN IF NOT EXISTS purchase_unit_price double precision,
        ADD COLUMN IF NOT EXISTS baseline_status varchar,
        ADD COLUMN IF NOT EXISTS baseline_note varchar
    """)
    cr.execute("""
        UPDATE restaurant_purchase_invoice_handoff_line
           SET expected_quantity = COALESCE(expected_quantity, 0),
               purchase_unit_price = COALESCE(
                   purchase_unit_price,
                   (SELECT pol.price_unit
                      FROM purchase_order_line pol
                     WHERE pol.id = purchase_line_id),
                   0
               ),
               baseline_status = COALESCE(baseline_status, 'uncertain'),
               baseline_note = COALESCE(
                   baseline_note,
                   'Awaiting historical PO-ledger reconstruction.'
               )
    """)

def migrate(cr, version):
    """Remove only the superseded hours-only check before day fields are added."""
    cr.execute("""
        ALTER TABLE restaurant_overtime_ledger
        DROP CONSTRAINT IF EXISTS restaurant_overtime_ledger_hours_active_consistency
    """)


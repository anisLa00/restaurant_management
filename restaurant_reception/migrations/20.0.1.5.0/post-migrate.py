def migrate(cr, version):
    """Preserve historical staging decisions without retroactive HR posting."""
    cr.execute(
        """
        UPDATE restaurant_attendance_entry
           SET sync_status = 'legacy'
         WHERE state IN ('approved', 'rejected')
           AND official_attendance_id IS NULL
           AND official_leave_id IS NULL
           AND overtime_ledger_id IS NULL
        """
    )

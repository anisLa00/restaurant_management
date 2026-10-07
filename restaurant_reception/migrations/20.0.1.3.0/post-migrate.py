def migrate(cr, version):
    """Keep every pre-1.3 closing on its historical manual workflow.

    The field default is deliberately used only for closings created after the
    upgrade. Existing records predate bill-level service entries, so changing
    their figures would invent operational detail that was never captured.
    """
    cr.execute(
        """
        UPDATE restaurant_daily_closing
           SET service_tracking_mode = 'legacy'
         WHERE service_tracking_mode IS NULL
            OR service_tracking_mode = 'service_entries'
        """
    )

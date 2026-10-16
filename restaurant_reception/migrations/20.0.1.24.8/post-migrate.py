ACCESS_NAMES = (
    "closing_operations_review",
    "attendance_operations_review",
    "attendance_sheet_operations_review",
    "waiter_closing_operations_review",
)


def migrate(cr, version):
    """Make Operations read-only in Reception monitoring workflows."""
    cr.execute(
        """
        DELETE FROM ir_access
         WHERE id IN (
             SELECT res_id
               FROM ir_model_data
              WHERE module = 'restaurant_reception'
                AND name = ANY(%s)
                AND model = 'ir.access'
         )
        """,
        (list(ACCESS_NAMES),),
    )
    cr.execute(
        """
        DELETE FROM ir_model_data
         WHERE module = 'restaurant_reception'
           AND name = ANY(%s)
           AND model = 'ir.access'
        """,
        (list(ACCESS_NAMES),),
    )

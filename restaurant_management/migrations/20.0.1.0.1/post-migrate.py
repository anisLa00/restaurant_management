ACCESS_NAMES = (
    "daily_stock_operations_review_management",
    "daily_stock_line_operations_review_management",
    "stock_event_operations_review_management",
    "stock_event_sheet_operations_review_management",
)


def migrate(cr, version):
    """Remove stale Operations write grants from management reviews."""
    cr.execute(
        """
        DELETE FROM ir_access
         WHERE id IN (
             SELECT res_id
               FROM ir_model_data
              WHERE module = 'restaurant_management'
                AND name = ANY(%s)
                AND model = 'ir.access'
         )
        """,
        (list(ACCESS_NAMES),),
    )
    cr.execute(
        """
        DELETE FROM ir_model_data
         WHERE module = 'restaurant_management'
           AND name = ANY(%s)
           AND model = 'ir.access'
        """,
        (list(ACCESS_NAMES),),
    )

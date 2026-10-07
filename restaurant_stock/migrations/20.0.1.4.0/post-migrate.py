def migrate(cr, version):
    """Preserve accepted history without guessing counts on open sheets."""
    cr.execute("""
        UPDATE restaurant_stock_daily_line AS line
           SET actual_closing_state = 'counted'
          FROM restaurant_stock_daily AS daily
         WHERE daily.id = line.daily_id
           AND (
               daily.state = 'closed'
               OR COALESCE(line.actual_closing_qty, 0.0) <> 0.0
           )
    """)

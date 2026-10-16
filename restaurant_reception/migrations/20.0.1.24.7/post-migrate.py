def migrate(cr, version):
    """Rank existing generated summaries without changing their source bills."""
    cr.execute(
        """
        WITH ranked AS (
            SELECT id,
                   RANK() OVER (
                       PARTITION BY waiter_closing_id
                       ORDER BY sales_amount DESC
                   ) AS sales_rank,
                   ROW_NUMBER() OVER (
                       PARTITION BY waiter_closing_id
                       ORDER BY sales_amount DESC, LOWER(summary_name), id
                   ) AS sort_sequence
              FROM restaurant_waiter_sales_summary
             WHERE summary_type = 'waiter'
        )
        UPDATE restaurant_waiter_sales_summary AS summary
           SET sales_rank = ranked.sales_rank,
               sort_sequence = ranked.sort_sequence
          FROM ranked
         WHERE summary.id = ranked.id
        """
    )
    cr.execute(
        """
        UPDATE restaurant_waiter_sales_summary
           SET sales_rank = 0,
               sort_sequence = 1000000
         WHERE summary_type = 'pickup_delivery'
        """
    )

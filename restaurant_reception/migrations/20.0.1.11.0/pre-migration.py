from dateutil.relativedelta import relativedelta


def migrate(cr, version):
    cr.execute("""
        ALTER TABLE restaurant_shift_roster
        ADD COLUMN IF NOT EXISTS month_start date,
        ADD COLUMN IF NOT EXISTS month_end date
    """)
    cr.execute("""
        SELECT id, week_start
          FROM restaurant_shift_roster
         WHERE month_start IS NULL
    """)
    for roster_id, week_start in cr.fetchall():
        month_start = week_start.replace(day=1)
        month_end = month_start + relativedelta(months=1, days=-1)
        cr.execute("""
            UPDATE restaurant_shift_roster
               SET month_start = %s,
                   month_end = %s
             WHERE id = %s
        """, (month_start, month_end, roster_id))
    cr.execute("""
        ALTER TABLE restaurant_shift_roster
        ALTER COLUMN month_start SET NOT NULL
    """)

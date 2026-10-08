def migrate(cr, version):
    """Finish the weekly-to-monthly roster conversion without guessing shifts."""
    cr.execute("""
        UPDATE restaurant_shift_roster roster
           SET name = branch.name || ' - ' ||
                      TO_CHAR(roster.month_start, 'FMMonth YYYY')
          FROM restaurant_branch branch
         WHERE branch.id = roster.branch_id
    """)
    cr.execute("""
        INSERT INTO restaurant_shift_roster_line (
            roster_id,
            employee_id,
            branch_id,
            company_id,
            staff_category_id,
            create_uid,
            create_date,
            write_uid,
            write_date
        )
        SELECT
            roster.id,
            employee.id,
            roster.branch_id,
            roster.company_id,
            employee.restaurant_staff_category_id,
            1,
            NOW() AT TIME ZONE 'UTC',
            1,
            NOW() AT TIME ZONE 'UTC'
        FROM restaurant_shift_roster roster
        JOIN hr_employee employee
          ON employee.restaurant_branch_id = roster.branch_id
         AND employee.company_id = roster.company_id
         AND employee.active = TRUE
        WHERE roster.state = 'draft'
        ON CONFLICT (roster_id, employee_id) DO NOTHING
    """)

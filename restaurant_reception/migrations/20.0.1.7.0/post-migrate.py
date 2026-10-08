def migrate(cr, version):
    """Add employee branch ownership and group legacy rows into daily sheets."""
    cr.execute(
        """
        UPDATE hr_employee employee
           SET restaurant_branch_id = users.default_restaurant_branch_id
          FROM res_users users
          JOIN restaurant_branch branch
            ON branch.id = users.default_restaurant_branch_id
         WHERE employee.user_id = users.id
           AND employee.restaurant_branch_id IS NULL
           AND branch.company_id = employee.company_id
        """
    )
    cr.execute(
        """
        INSERT INTO restaurant_attendance_sheet (
            branch_id,
            attendance_date,
            company_id,
            name,
            state,
            create_uid,
            create_date,
            write_uid,
            write_date
        )
        SELECT
            entry.branch_id,
            entry.attendance_date,
            branch.company_id,
            branch.name || ' - ' || entry.attendance_date::text,
            CASE
                WHEN BOOL_AND(entry.state IN ('approved', 'rejected')) THEN 'approved'
                WHEN BOOL_AND(entry.state = 'submitted_to_hr') THEN 'submitted_to_hr'
                WHEN BOOL_AND(entry.state = 'draft') THEN 'draft'
                ELSE 'manager_review'
            END,
            1,
            NOW() AT TIME ZONE 'UTC',
            1,
            NOW() AT TIME ZONE 'UTC'
        FROM restaurant_attendance_entry entry
        JOIN restaurant_branch branch ON branch.id = entry.branch_id
        WHERE entry.sheet_id IS NULL
        GROUP BY entry.branch_id, entry.attendance_date, branch.company_id, branch.name
        ON CONFLICT (branch_id, attendance_date) DO NOTHING
        """
    )
    cr.execute(
        """
        UPDATE restaurant_attendance_entry entry
           SET sheet_id = sheet.id
          FROM restaurant_attendance_sheet sheet
         WHERE entry.sheet_id IS NULL
           AND sheet.branch_id = entry.branch_id
           AND sheet.attendance_date = entry.attendance_date
        """
    )
    cr.execute(
        """
        UPDATE restaurant_overtime_ledger
           SET active = TRUE
         WHERE active IS NULL
        """
    )

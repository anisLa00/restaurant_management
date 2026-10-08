def migrate(cr, version):
    """Snapshot each employee's current staff category on historical rows."""
    cr.execute(
        """
        UPDATE restaurant_attendance_entry entry
           SET staff_category_id = employee.restaurant_staff_category_id
          FROM hr_employee employee
         WHERE entry.employee_id = employee.id
           AND entry.staff_category_id IS NULL
           AND employee.restaurant_staff_category_id IS NOT NULL
        """
    )

def migrate(cr, version):
    """Refresh current categories only on Reception-owned draft rows."""
    cr.execute(
        """
        UPDATE restaurant_attendance_entry entry
           SET staff_category_id = employee.restaurant_staff_category_id
          FROM hr_employee employee
         WHERE entry.employee_id = employee.id
           AND entry.state = 'draft'
           AND entry.staff_category_id IS DISTINCT FROM employee.restaurant_staff_category_id
        """
    )

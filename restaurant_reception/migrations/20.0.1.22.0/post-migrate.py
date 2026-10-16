def migrate(cr, version):
    """Keep all historical rows explicitly on their legacy calculation paths."""
    cr.execute("""
        UPDATE restaurant_attendance_entry
           SET overtime_measurement = 'legacy_hours'
         WHERE overtime_measurement IS NULL
    """)
    cr.execute("""
        UPDATE restaurant_overtime_ledger
           SET measurement_type = 'legacy_hours',
               days = COALESCE(days, 0),
               amount = COALESCE(amount, 0)
         WHERE measurement_type IS NULL
    """)
    cr.execute("""
        UPDATE restaurant_payroll_input_line
           SET calculation_version = 'legacy'
         WHERE calculation_version IS NULL
    """)
    cr.execute("""
        CREATE UNIQUE INDEX IF NOT EXISTS
            restaurant_overtime_employee_date_active_uniq
        ON restaurant_overtime_ledger (employee_id, attendance_date)
        WHERE active
    """)

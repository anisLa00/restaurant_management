def migrate(cr, version):
    """Clarify that the pre-employment document is not a work entry permit."""
    cr.execute(
        """
        UPDATE restaurant_employee_document_type
           SET name = jsonb_set(
               COALESCE(name, '{}'::jsonb),
               '{en_US}',
               to_jsonb(%s::text),
               TRUE
           )
         WHERE code = 'current_visa_entry_permit'
        """,
        ('Current Visa Copy (Visit / Tourist / Existing Residence)',),
    )
    cr.execute(
        """
        UPDATE hr_employee
           SET restaurant_work_authorized = TRUE,
               restaurant_work_authorized_by_id = 1,
               restaurant_work_authorized_on = NOW() AT TIME ZONE 'UTC'
         WHERE restaurant_immigration_status IS NULL
        """
    )

from odoo import SUPERUSER_ID, api


def migrate(cr, version):
    """Assign permanent employee numbers to records created before this version."""
    env = api.Environment(cr, SUPERUSER_ID, {})
    employees = env['hr.employee'].with_context(active_test=False).search([
        '|',
        ('restaurant_employee_number', '=', False),
        ('restaurant_employee_number', 'in', ('New', '/')),
    ], order='id')
    sequence = env['ir.sequence'].sudo().search([
        ('code', '=', 'restaurant.employee.number'),
        ('company_id', '=', False),
    ], limit=1)
    for employee in employees:
        employee.restaurant_employee_number = sequence.next_by_id()

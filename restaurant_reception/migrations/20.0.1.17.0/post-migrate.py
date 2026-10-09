from odoo import SUPERUSER_ID, api


def migrate(cr, version):
    """Start probation for onboarding completed before the workflow existed."""
    env = api.Environment(cr, SUPERUSER_ID, {})
    probation_model = env['restaurant.employee.probation'].sudo()
    completed_onboardings = env['restaurant.employee.onboarding'].sudo().search([
        ('state', '=', 'completed'),
    ], order='id')
    for onboarding in completed_onboardings:
        probation_model.ensure_from_onboarding(onboarding)

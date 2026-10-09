from datetime import timedelta

from dateutil.relativedelta import relativedelta

from odoo import SUPERUSER_ID, api


def migrate(cr, version):
    """Use the agreed three-month period for untouched auto-created probation."""
    env = api.Environment(cr, SUPERUSER_ID, {})
    probations = env['restaurant.employee.probation'].sudo().search([
        ('state', 'in', ['active', 'extended', 'hr_review']),
        ('created_from_onboarding_at', '!=', False),
    ])
    for probation in probations:
        six_month_end = probation.start_date + relativedelta(months=6, days=-1)
        if probation.planned_end_date != six_month_end:
            continue
        if probation.review_ids.filtered(lambda review: review.state == 'submitted'):
            continue
        three_month_end = probation.start_date + relativedelta(months=3, days=-1)
        span_days = (three_month_end - probation.start_date).days
        dates_by_type = {
            'first_month': probation.start_date + relativedelta(months=1, days=-1),
            'midterm': probation.start_date + timedelta(days=max(span_days // 2, 1)),
            'final': max(three_month_end - timedelta(days=14), probation.start_date),
        }
        probation.with_context(restaurant_probation_internal=True).write({
            'planned_end_date': three_month_end,
        })
        for review in probation.review_ids.filtered(lambda record: record.state == 'draft'):
            review.with_context(restaurant_probation_internal=True).write({
                'review_date': dates_by_type.get(review.review_type, review.review_date),
            })

from odoo import api, fields, models
from odoo.exceptions import AccessError, UserError, ValidationError

from .reception_security import HR_GROUP, MANAGER_GROUP, require_assigned_branches


MANAGER_EDITABLE_FIELDS = {
    'quality_rating', 'attendance_rating', 'teamwork_rating',
    'customer_service_rating', 'policy_compliance_rating',
    'strengths', 'concerns', 'goals', 'manager_comments', 'manager_recommendation',
}


class RestaurantEmployeePerformanceReview(models.Model):
    _name = 'restaurant.employee.performance.review'
    _description = 'Restaurant Employee Performance Review'
    _inherit = ['mail.thread', 'mail.activity.mixin']
    _order = 'period_end desc, id desc'
    _mail_post_access = 'read'

    reference = fields.Char(required=True, default='New', readonly=True, copy=False, index=True)
    employee_id = fields.Many2one(
        'hr.employee', required=True, ondelete='restrict', index=True, tracking=True,
        domain="[('active', '=', True)]",
    )
    employee_number = fields.Char(related='employee_id.restaurant_employee_number', readonly=True)
    company_id = fields.Many2one(related='employee_id.company_id', store=True, index=True)
    branch_id = fields.Many2one(
        related='employee_id.restaurant_branch_id', store=True, index=True,
        string='Restaurant Branch',
    )
    job_id = fields.Many2one(related='employee_id.job_id', store=True, string='Position')
    review_type = fields.Selection([
        ('quarterly', 'Quarterly'),
        ('semiannual', 'Semiannual'),
        ('annual', 'Annual'),
        ('ad_hoc', 'Ad Hoc'),
    ], required=True, default='quarterly', tracking=True)
    period_start = fields.Date(required=True, tracking=True)
    period_end = fields.Date(required=True, tracking=True)
    review_date = fields.Date(default=fields.Date.context_today, required=True)
    state = fields.Selection([
        ('draft', 'Draft'),
        ('manager_review', 'Manager Review'),
        ('hr_review', 'HR Review'),
        ('completed', 'Completed'),
    ], required=True, default='draft', readonly=True, copy=False, tracking=True, index=True)
    quality_rating = fields.Selection([(str(value), str(value)) for value in range(1, 6)], string='Work Quality')
    attendance_rating = fields.Selection([(str(value), str(value)) for value in range(1, 6)], string='Attendance & Reliability')
    teamwork_rating = fields.Selection([(str(value), str(value)) for value in range(1, 6)], string='Teamwork')
    customer_service_rating = fields.Selection([(str(value), str(value)) for value in range(1, 6)], string='Customer Service')
    policy_compliance_rating = fields.Selection([(str(value), str(value)) for value in range(1, 6)], string='Policy Compliance')
    overall_score = fields.Float(compute='_compute_overall_score', store=True)
    strengths = fields.Text()
    concerns = fields.Text()
    goals = fields.Text(string='Goals for Next Period')
    manager_comments = fields.Text()
    manager_recommendation = fields.Selection([
        ('continue', 'Continue in Role'),
        ('development', 'Development Plan'),
        ('promotion', 'Consider Promotion'),
        ('performance_action', 'Performance Action Required'),
    ])
    hr_comments = fields.Text()
    development_plan = fields.Text()
    final_outcome = fields.Selection([
        ('meets', 'Meets Expectations'),
        ('exceeds', 'Exceeds Expectations'),
        ('development', 'Development Plan Required'),
        ('performance_action', 'Performance Action Required'),
    ])
    sent_by = fields.Many2one('res.users', readonly=True, copy=False)
    sent_at = fields.Datetime(readonly=True, copy=False)
    manager_submitted_by = fields.Many2one('res.users', readonly=True, copy=False)
    manager_submitted_at = fields.Datetime(readonly=True, copy=False)
    completed_by = fields.Many2one('res.users', readonly=True, copy=False)
    completed_at = fields.Datetime(readonly=True, copy=False)

    _employee_period_unique = models.Constraint(
        'UNIQUE(employee_id, period_start, period_end, review_type)',
        'This employee already has the same performance review period and type.',
    )

    def _is_hr(self):
        return self.env.su or self.env.user.has_group(HR_GROUP) or self.env.user.has_group('base.group_system')

    def _is_manager(self):
        return self.env.user.has_group(MANAGER_GROUP)

    def _require_hr(self):
        if not self._is_hr():
            raise AccessError(self.env._('Only HR can manage the performance review workflow.'))

    @api.depends(
        'quality_rating', 'attendance_rating', 'teamwork_rating',
        'customer_service_rating', 'policy_compliance_rating',
    )
    def _compute_overall_score(self):
        for review in self:
            values = [
                int(value) for value in [
                    review.quality_rating, review.attendance_rating,
                    review.teamwork_rating, review.customer_service_rating,
                    review.policy_compliance_rating,
                ] if value
            ]
            review.overall_score = sum(values) / len(values) if values else 0.0

    @api.constrains('period_start', 'period_end')
    def _check_period(self):
        if any(review.period_start and review.period_end and review.period_end < review.period_start for review in self):
            raise ValidationError(self.env._('The review period end cannot be before its start.'))

    @api.model_create_multi
    def create(self, vals_list):
        self._require_hr()
        sequence = self.env['ir.sequence'].sudo().search([
            ('code', '=', 'restaurant.employee.performance.review'),
            ('company_id', '=', False),
        ], limit=1)
        if not sequence:
            raise UserError(self.env._('The performance review sequence is not configured.'))
        prepared = []
        for vals in vals_list:
            values = dict(vals)
            if values.get('state', 'draft') != 'draft':
                raise AccessError(self.env._('Performance reviews must start in Draft.'))
            if not values.get('reference') or values.get('reference') in ('New', '/'):
                values['reference'] = sequence.next_by_id()
            values['state'] = 'draft'
            prepared.append(values)
        return super().create(prepared)

    def write(self, vals):
        if self.env.context.get('restaurant_performance_internal'):
            return super().write(vals)
        if 'state' in vals:
            raise AccessError(self.env._('Use performance review workflow buttons to change status.'))
        if self._is_hr():
            if any(review.state == 'completed' for review in self):
                raise AccessError(self.env._('Completed performance reviews are locked.'))
            if any(review.state != 'draft' for review in self) and set(vals) & {
                'employee_id', 'review_type', 'period_start', 'period_end',
            }:
                raise AccessError(self.env._('The employee and review period are locked after submission.'))
            return super().write(vals)
        if self._is_manager():
            if set(vals) - MANAGER_EDITABLE_FIELDS:
                raise AccessError(self.env._('Managers can edit only their performance assessment.'))
            for review in self:
                require_assigned_branches(review.branch_id)
                if review.state != 'manager_review':
                    raise AccessError(self.env._('This review is not awaiting the manager.'))
            return super().write(vals)
        raise AccessError(self.env._('You cannot edit this performance review.'))

    def unlink(self):
        self._require_hr()
        if any(review.state != 'draft' for review in self):
            raise AccessError(self.env._('Only draft performance reviews can be deleted.'))
        return super().unlink()

    def action_send_to_manager(self):
        self.ensure_one()
        self._require_hr()
        if self.state != 'draft':
            raise UserError(self.env._('Only a draft review can be sent to the manager.'))
        if not self.branch_id:
            raise ValidationError(self.env._('Set the employee Restaurant Branch first.'))
        self.with_context(restaurant_performance_internal=True).write({
            'state': 'manager_review',
            'sent_by': self.env.uid,
            'sent_at': fields.Datetime.now(),
        })
        return True

    def action_submit_to_hr(self):
        self.ensure_one()
        if not self._is_manager() and not self._is_hr():
            raise AccessError(self.env._('Only the assigned Branch Manager can submit this review.'))
        if not self._is_hr():
            require_assigned_branches(self.branch_id)
        if self.state != 'manager_review':
            raise UserError(self.env._('This review is not awaiting manager submission.'))
        ratings = [
            self.quality_rating, self.attendance_rating, self.teamwork_rating,
            self.customer_service_rating, self.policy_compliance_rating,
        ]
        if not all(ratings) or not self.manager_recommendation:
            raise ValidationError(self.env._(
                'Complete all five ratings and the manager recommendation first.'
            ))
        self.with_context(restaurant_performance_internal=True).write({
            'state': 'hr_review',
            'manager_submitted_by': self.env.uid,
            'manager_submitted_at': fields.Datetime.now(),
        })
        return True

    def action_return_to_manager(self):
        self.ensure_one()
        self._require_hr()
        if self.state != 'hr_review':
            raise UserError(self.env._('Only a review awaiting HR can be returned.'))
        self.with_context(restaurant_performance_internal=True).write({'state': 'manager_review'})
        return True

    def action_complete(self):
        self.ensure_one()
        self._require_hr()
        if self.state != 'hr_review':
            raise UserError(self.env._('Only a review awaiting HR can be completed.'))
        if not self.final_outcome or not (self.hr_comments or '').strip():
            raise ValidationError(self.env._('Enter the final outcome and HR comments.'))
        self.with_context(restaurant_performance_internal=True).write({
            'state': 'completed',
            'completed_by': self.env.uid,
            'completed_at': fields.Datetime.now(),
        })
        return True

from datetime import timedelta

from dateutil.relativedelta import relativedelta

from odoo import api, fields, models
from odoo.exceptions import AccessError, UserError, ValidationError

from .reception_security import HR_GROUP, MANAGER_GROUP, require_assigned_branches


PROBATION_OPEN_STATES = ('active', 'extended', 'hr_review')
DEFAULT_PROBATION_MONTHS = 3


class RestaurantEmployeeProbation(models.Model):
    _name = 'restaurant.employee.probation'
    _description = 'Restaurant Employee Probation'
    _inherit = ['mail.thread', 'mail.activity.mixin']
    _order = 'start_date desc, id desc'
    _mail_post_access = 'read'

    reference = fields.Char(
        required=True, default='New', readonly=True, copy=False, index=True,
    )
    employee_id = fields.Many2one(
        'hr.employee', required=True, ondelete='restrict', index=True,
        tracking=True,
    )
    onboarding_id = fields.Many2one(
        'restaurant.employee.onboarding', required=True, ondelete='restrict',
        readonly=True, copy=False, index=True,
    )
    employee_number = fields.Char(
        related='employee_id.restaurant_employee_number', readonly=True,
    )
    company_id = fields.Many2one(
        related='employee_id.company_id', store=True, index=True,
    )
    branch_id = fields.Many2one(
        related='employee_id.restaurant_branch_id', store=True, index=True,
        string='Restaurant Branch',
    )
    job_id = fields.Many2one(
        related='employee_id.job_id', store=True, string='Position',
    )
    start_date = fields.Date(required=True, tracking=True)
    planned_end_date = fields.Date(required=True, tracking=True)
    legal_max_end_date = fields.Date(
        compute='_compute_legal_max_end_date', store=True,
        string='Legal Maximum End Date',
    )
    remaining_days = fields.Integer(compute='_compute_remaining_days')
    state = fields.Selection([
        ('active', 'Active'),
        ('extended', 'Extended'),
        ('hr_review', 'HR Review'),
        ('confirmed', 'Employment Confirmed'),
        ('ended', 'Ended During Probation'),
    ], required=True, default='active', readonly=True, copy=False, index=True,
        tracking=True)
    review_ids = fields.One2many(
        'restaurant.employee.probation.review', 'probation_id',
        string='Manager Reviews', copy=False,
    )
    review_count = fields.Integer(compute='_compute_review_count')
    extension_end_date = fields.Date(copy=False, tracking=True)
    extension_reason = fields.Text(copy=False)
    hr_decision_note = fields.Text(string='HR Decision Note', copy=False)
    termination_reason = fields.Text(copy=False)
    notice_date = fields.Date(copy=False, tracking=True)
    effective_end_date = fields.Date(copy=False, tracking=True)
    created_from_onboarding_at = fields.Datetime(readonly=True, copy=False)
    extended_by = fields.Many2one('res.users', readonly=True, copy=False)
    extended_at = fields.Datetime(readonly=True, copy=False)
    confirmed_by = fields.Many2one('res.users', readonly=True, copy=False)
    confirmed_at = fields.Datetime(readonly=True, copy=False)
    ended_by = fields.Many2one('res.users', readonly=True, copy=False)
    ended_at = fields.Datetime(readonly=True, copy=False)

    _onboarding_unique = models.Constraint(
        'UNIQUE(onboarding_id)',
        'A probation record already exists for this onboarding.',
    )
    _employee_unique = models.Constraint(
        'UNIQUE(employee_id)',
        'A probation record already exists for this employee.',
    )

    def _require_hr(self):
        if not self.env.su and not (
            self.env.user.has_group(HR_GROUP)
            or self.env.user.has_group('base.group_system')
        ):
            raise AccessError(self.env._('Only HR can manage employee probation.'))

    @api.depends('start_date')
    def _compute_legal_max_end_date(self):
        for probation in self:
            probation.legal_max_end_date = (
                probation.start_date + relativedelta(months=6, days=-1)
                if probation.start_date else False
            )

    @api.depends('planned_end_date', 'state')
    def _compute_remaining_days(self):
        today = fields.Date.context_today(self)
        for probation in self:
            probation.remaining_days = (
                max((probation.planned_end_date - today).days, 0)
                if probation.planned_end_date
                and probation.state in PROBATION_OPEN_STATES else 0
            )

    @api.depends('review_ids')
    def _compute_review_count(self):
        for probation in self:
            probation.review_count = len(probation.review_ids)

    @api.constrains('start_date', 'planned_end_date')
    def _check_probation_dates(self):
        for probation in self:
            if not probation.start_date or not probation.planned_end_date:
                continue
            if probation.planned_end_date < probation.start_date:
                raise ValidationError(self.env._(
                    'The probation end date cannot be before its start date.'
                ))
            if probation.planned_end_date > probation.legal_max_end_date:
                raise ValidationError(self.env._(
                    'Probation cannot exceed six months from the first work day.'
                ))

    @api.model_create_multi
    def create(self, vals_list):
        self._require_hr()
        sequence = self.env['ir.sequence'].sudo().search([
            ('code', '=', 'restaurant.employee.probation'),
            ('company_id', '=', False),
        ], limit=1)
        if not sequence:
            raise UserError(self.env._('The employee probation sequence is not configured.'))
        prepared = []
        for vals in vals_list:
            values = dict(vals)
            if values.get('state', 'active') != 'active':
                raise AccessError(self.env._('Probation must start in Active status.'))
            if not values.get('reference') or values.get('reference') in ('New', '/'):
                values['reference'] = sequence.next_by_id()
            start_date = fields.Date.to_date(values.get('start_date'))
            if start_date and not values.get('planned_end_date'):
                values['planned_end_date'] = start_date + relativedelta(
                    months=DEFAULT_PROBATION_MONTHS, days=-1,
                )
            values['state'] = 'active'
            prepared.append(values)
        return super().create(prepared)

    def write(self, vals):
        if self.env.context.get('restaurant_probation_internal'):
            return super().write(vals)
        self._require_hr()
        if 'state' in vals:
            raise AccessError(self.env._('Use probation workflow buttons to change status.'))
        if any(record.state in ('confirmed', 'ended') for record in self) and set(vals) - {
            'hr_decision_note',
        }:
            raise AccessError(self.env._('Closed probation records are locked.'))
        if set(vals) & {
            'employee_id', 'onboarding_id', 'start_date', 'planned_end_date',
        } and any(record.state != 'active' for record in self):
            raise AccessError(self.env._(
                'Core probation dates cannot change after the workflow advances.'
            ))
        return super().write(vals)

    def unlink(self):
        self._require_hr()
        raise AccessError(self.env._(
            'Probation records are audit records and cannot be deleted.'
        ))

    @api.model
    def ensure_from_onboarding(self, onboarding):
        onboarding.ensure_one()
        existing = self.sudo().search([
            ('onboarding_id', '=', onboarding.id),
        ], limit=1)
        if existing:
            return existing
        start_date = onboarding.start_date
        probation = self.with_context(restaurant_probation_internal=True).create({
            'employee_id': onboarding.employee_id.id,
            'onboarding_id': onboarding.id,
            'start_date': start_date,
            'planned_end_date': start_date + relativedelta(
                months=DEFAULT_PROBATION_MONTHS, days=-1,
            ),
            'created_from_onboarding_at': fields.Datetime.now(),
        })
        probation._create_review_schedule()
        return probation

    def _create_review_schedule(self):
        Review = self.env['restaurant.employee.probation.review'].sudo()
        for probation in self:
            if probation.review_ids:
                continue
            span_days = (probation.planned_end_date - probation.start_date).days
            schedules = [
                ('first_month', probation.start_date + relativedelta(months=1, days=-1)),
                ('midterm', probation.start_date + timedelta(days=max(span_days // 2, 1))),
                ('final', max(probation.planned_end_date - timedelta(days=14), probation.start_date)),
            ]
            Review.with_context(restaurant_probation_internal=True).create([
                {
                    'probation_id': probation.id,
                    'review_type': review_type,
                    'review_date': review_date,
                }
                for review_type, review_date in schedules
            ])

    def action_extend(self):
        self.ensure_one()
        self._require_hr()
        if self.state not in PROBATION_OPEN_STATES:
            raise UserError(self.env._('Only open probation can be extended.'))
        if not self.extension_reason:
            raise ValidationError(self.env._('Enter the extension reason.'))
        if not self.extension_end_date or self.extension_end_date <= self.planned_end_date:
            raise ValidationError(self.env._(
                'Choose an extension end date after the current end date.'
            ))
        if self.extension_end_date > self.legal_max_end_date:
            raise ValidationError(self.env._(
                'The extension cannot exceed the six-month legal maximum.'
            ))
        self.with_context(restaurant_probation_internal=True).write({
            'state': 'extended',
            'planned_end_date': self.extension_end_date,
            'extended_by': self.env.uid,
            'extended_at': fields.Datetime.now(),
        })
        self.message_post(body=self.env._(
            'Probation extended to %(date)s by %(user)s. Reason: %(reason)s',
            date=self.planned_end_date,
            user=self.env.user.name,
            reason=self.extension_reason,
        ))
        return True

    def _require_submitted_final_review(self):
        final_review = self.review_ids.filtered(
            lambda review: review.review_type == 'final' and review.state == 'submitted'
        )
        if not final_review:
            raise ValidationError(self.env._(
                'The Branch Manager must submit the final probation review first.'
            ))

    def action_confirm_employment(self):
        self.ensure_one()
        self._require_hr()
        if self.state not in PROBATION_OPEN_STATES:
            raise UserError(self.env._('Only open probation can be confirmed.'))
        self._require_submitted_final_review()
        if not self.hr_decision_note:
            raise ValidationError(self.env._('Enter the HR decision note.'))
        self.with_context(restaurant_probation_internal=True).write({
            'state': 'confirmed',
            'confirmed_by': self.env.uid,
            'confirmed_at': fields.Datetime.now(),
        })
        self.message_post(body=self.env._(
            'Employment confirmed after probation by %(user)s. Decision: %(note)s',
            user=self.env.user.name,
            note=self.hr_decision_note,
        ))
        return True

    def action_end_during_probation(self):
        self.ensure_one()
        self._require_hr()
        if self.state not in PROBATION_OPEN_STATES:
            raise UserError(self.env._('Only open probation can be ended.'))
        self._require_submitted_final_review()
        if not self.termination_reason:
            raise ValidationError(self.env._('Enter the documented termination reason.'))
        if not self.notice_date or not self.effective_end_date:
            raise ValidationError(self.env._(
                'Enter the written notice date and effective end date.'
            ))
        if self.effective_end_date < self.notice_date + timedelta(days=14):
            raise ValidationError(self.env._(
                'The effective end date must allow at least 14 days written notice.'
            ))
        if self.notice_date > self.legal_max_end_date:
            raise ValidationError(self.env._(
                'The written notice must be issued during the probation period.'
            ))
        if self.effective_end_date > self.legal_max_end_date:
            raise ValidationError(self.env._(
                'The effective end date must remain within the probation period.'
            ))
        self.with_context(restaurant_probation_internal=True).write({
            'state': 'ended',
            'ended_by': self.env.uid,
            'ended_at': fields.Datetime.now(),
        })
        self.message_post(body=self.env._(
            'Probation ended by %(user)s effective %(date)s after written notice. '
            'Reason: %(reason)s',
            user=self.env.user.name,
            date=self.effective_end_date,
            reason=self.termination_reason,
        ))
        return True


class RestaurantEmployeeProbationReview(models.Model):
    _name = 'restaurant.employee.probation.review'
    _description = 'Restaurant Employee Probation Review'
    _order = 'review_date, id'

    probation_id = fields.Many2one(
        'restaurant.employee.probation', required=True, ondelete='cascade', index=True,
    )
    employee_id = fields.Many2one(
        related='probation_id.employee_id', store=True, index=True,
    )
    company_id = fields.Many2one(
        related='probation_id.company_id', store=True, index=True,
    )
    branch_id = fields.Many2one(
        related='probation_id.branch_id', store=True, index=True,
    )
    review_type = fields.Selection([
        ('first_month', 'First Month'),
        ('midterm', 'Mid-Probation'),
        ('final', 'Final Review'),
        ('ad_hoc', 'Additional Review'),
    ], required=True, default='ad_hoc', index=True)
    review_date = fields.Date(required=True, default=fields.Date.context_today, index=True)
    rating = fields.Selection([
        ('1', '1 - Unsatisfactory'),
        ('2', '2 - Needs Improvement'),
        ('3', '3 - Meets Expectations'),
        ('4', '4 - Very Good'),
        ('5', '5 - Excellent'),
    ])
    recommendation = fields.Selection([
        ('confirm', 'Confirm Employment'),
        ('extend', 'Extend / Improvement Plan'),
        ('end', 'End During Probation'),
    ])
    strengths = fields.Text()
    concerns = fields.Text()
    improvement_plan = fields.Text()
    state = fields.Selection([
        ('draft', 'Draft'),
        ('submitted', 'Submitted to HR'),
    ], required=True, default='draft', readonly=True, index=True)
    submitted_by = fields.Many2one('res.users', readonly=True, copy=False)
    submitted_at = fields.Datetime(readonly=True, copy=False)
    can_submit = fields.Boolean(compute='_compute_can_submit')

    @api.depends('state', 'probation_id.state', 'branch_id')
    @api.depends_context('uid')
    def _compute_can_submit(self):
        is_hr = self.env.su or self.env.user.has_group(HR_GROUP) or self.env.user.has_group(
            'base.group_system'
        )
        is_manager = self.env.user.has_group(MANAGER_GROUP)
        branch_ids = set(self.env.user.restaurant_branch_ids.ids)
        for review in self:
            review.can_submit = (
                review.state == 'draft'
                and review.probation_id.state in PROBATION_OPEN_STATES
                and (is_hr or (is_manager and review.branch_id.id in branch_ids))
            )

    def _check_reviewer(self):
        if self.env.su or self.env.user.has_group(HR_GROUP) or self.env.user.has_group(
            'base.group_system'
        ):
            return
        if not self.env.user.has_group(MANAGER_GROUP):
            raise AccessError(self.env._('Only HR or the assigned Branch Manager can review.'))
        require_assigned_branches(self.branch_id)

    @api.model_create_multi
    def create(self, vals_list):
        records = super().create(vals_list)
        if not self.env.context.get('restaurant_probation_internal'):
            records._check_reviewer()
        if any(record.probation_id.state not in PROBATION_OPEN_STATES for record in records):
            raise AccessError(self.env._('Reviews can only be added to open probation.'))
        return records

    def write(self, vals):
        if self.env.context.get('restaurant_probation_internal'):
            return super().write(vals)
        self._check_reviewer()
        if 'state' in vals or set(vals) & {'submitted_by', 'submitted_at'}:
            raise AccessError(self.env._('Use Submit to HR to change review status.'))
        if any(review.state != 'draft' for review in self):
            raise AccessError(self.env._('Submitted probation reviews are locked.'))
        return super().write(vals)

    def unlink(self):
        self._check_reviewer()
        if any(review.state != 'draft' for review in self):
            raise AccessError(self.env._('Submitted probation reviews cannot be deleted.'))
        return super().unlink()

    def action_submit(self):
        for review in self:
            review._check_reviewer()
            if review.state != 'draft':
                continue
            if review.probation_id.state not in PROBATION_OPEN_STATES:
                raise UserError(self.env._('The probation is already closed.'))
            if not review.rating or not review.recommendation:
                raise ValidationError(self.env._('Enter the rating and recommendation.'))
            if review.recommendation == 'extend' and not review.improvement_plan:
                raise ValidationError(self.env._(
                    'Enter an improvement plan when recommending an extension.'
                ))
            if review.recommendation == 'end' and not review.concerns:
                raise ValidationError(self.env._(
                    'Document the concerns when recommending an end to probation.'
                ))
            review.with_context(restaurant_probation_internal=True).write({
                'state': 'submitted',
                'submitted_by': self.env.uid,
                'submitted_at': fields.Datetime.now(),
            })
            if review.review_type == 'final':
                review.probation_id.sudo().with_context(
                    restaurant_probation_internal=True
                ).write({'state': 'hr_review'})
            review.probation_id.message_post(body=self.env._(
                '%(type)s probation review submitted by %(user)s with recommendation: %(result)s.',
                type=dict(review._fields['review_type'].selection).get(review.review_type),
                user=self.env.user.name,
                result=dict(review._fields['recommendation'].selection).get(
                    review.recommendation
                ),
            ))
        return True

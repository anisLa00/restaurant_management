from datetime import timedelta

from odoo import api, fields, models
from odoo.exceptions import AccessError, UserError, ValidationError

from .reception_security import HR_GROUP, MANAGER_GROUP, require_assigned_branches


ONBOARDING_ACTIVE_STATES = ('in_progress', 'extended')


class RestaurantEmployeeOnboarding(models.Model):
    _name = 'restaurant.employee.onboarding'
    _description = 'Restaurant Employee Onboarding'
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
    employment_verified = fields.Boolean(
        related='employee_id.restaurant_work_authorized', readonly=True,
        string='Employment Documents Verified',
    )
    start_date = fields.Date(
        required=True, default=fields.Date.context_today, tracking=True,
    )
    planned_end_date = fields.Date(
        required=True, default=lambda self: fields.Date.context_today(self) + timedelta(days=6),
        tracking=True,
    )
    duration_days = fields.Integer(
        compute='_compute_progress', string='Planned Days',
    )
    state = fields.Selection([
        ('draft', 'Draft'),
        ('in_progress', 'In Progress'),
        ('extended', 'Extended'),
        ('completed', 'Completed'),
        ('failed', 'Failed'),
    ], required=True, default='draft', readonly=True, copy=False, index=True,
        tracking=True)
    task_ids = fields.One2many(
        'restaurant.employee.onboarding.task', 'onboarding_id',
        string='Onboarding Checklist', copy=False,
    )
    progress_percent = fields.Float(
        compute='_compute_progress', string='Progress',
    )
    pending_mandatory_count = fields.Integer(
        compute='_compute_progress', string='Required Tasks Remaining',
    )
    extension_end_date = fields.Date(copy=False, tracking=True)
    extension_reason = fields.Text(copy=False)
    failure_reason = fields.Text(copy=False)
    hr_note = fields.Text(string='HR Notes')
    started_by = fields.Many2one('res.users', readonly=True, copy=False)
    started_at = fields.Datetime(readonly=True, copy=False)
    extended_by = fields.Many2one('res.users', readonly=True, copy=False)
    extended_at = fields.Datetime(readonly=True, copy=False)
    completed_by = fields.Many2one('res.users', readonly=True, copy=False)
    completed_at = fields.Datetime(readonly=True, copy=False)
    failed_by = fields.Many2one('res.users', readonly=True, copy=False)
    failed_at = fields.Datetime(readonly=True, copy=False)

    _employee_unique = models.Constraint(
        'UNIQUE(employee_id)',
        'An onboarding record already exists for this employee.',
    )

    def _require_hr(self):
        if not self.env.su and not (
            self.env.user.has_group(HR_GROUP)
            or self.env.user.has_group('base.group_system')
        ):
            raise AccessError(self.env._('Only HR can manage employee onboarding.'))

    @api.depends(
        'start_date', 'planned_end_date', 'task_ids.state', 'task_ids.mandatory',
    )
    def _compute_progress(self):
        for onboarding in self:
            tasks = onboarding.task_ids
            onboarding.progress_percent = (
                100.0 * len(tasks.filtered(lambda task: task.state == 'done')) / len(tasks)
                if tasks else 0.0
            )
            onboarding.pending_mandatory_count = len(tasks.filtered(
                lambda task: task.mandatory and task.state != 'done'
            ))
            onboarding.duration_days = (
                (onboarding.planned_end_date - onboarding.start_date).days + 1
                if onboarding.start_date and onboarding.planned_end_date else 0
            )

    @api.constrains('start_date', 'planned_end_date')
    def _check_dates(self):
        for onboarding in self:
            if (
                onboarding.start_date
                and onboarding.planned_end_date
                and onboarding.planned_end_date < onboarding.start_date
            ):
                raise ValidationError(self.env._(
                    'The onboarding end date cannot be before its start date.'
                ))

    @api.model_create_multi
    def create(self, vals_list):
        self._require_hr()
        sequence = self.env['ir.sequence'].sudo().search([
            ('code', '=', 'restaurant.employee.onboarding'),
            ('company_id', '=', False),
        ], limit=1)
        if not sequence:
            raise UserError(self.env._('The employee onboarding sequence is not configured.'))
        prepared = []
        for vals in vals_list:
            values = dict(vals)
            if values.get('state', 'draft') != 'draft':
                raise AccessError(self.env._('Onboarding must start in Draft.'))
            if not values.get('reference') or values.get('reference') in ('New', '/'):
                values['reference'] = sequence.next_by_id()
            values['state'] = 'draft'
            prepared.append(values)
        records = super().create(prepared)
        records._create_default_tasks()
        return records

    def write(self, vals):
        if self.env.context.get('restaurant_onboarding_internal'):
            return super().write(vals)
        self._require_hr()
        if 'state' in vals:
            raise AccessError(self.env._('Use onboarding workflow buttons to change status.'))
        locked = self.filtered(lambda record: record.state in ('completed', 'failed'))
        if locked and set(vals) - {'hr_note'}:
            raise AccessError(self.env._('Completed or failed onboarding records are locked.'))
        if any(record.state != 'draft' for record in self) and set(vals) & {
            'employee_id', 'start_date', 'task_ids',
        }:
            raise AccessError(self.env._(
                'Employee, start date and checklist cannot change after onboarding starts.'
            ))
        return super().write(vals)

    def unlink(self):
        self._require_hr()
        if any(record.state != 'draft' for record in self):
            raise AccessError(self.env._('Only draft onboarding records can be deleted.'))
        return super().unlink()

    def _create_default_tasks(self):
        Task = self.env['restaurant.employee.onboarding.task'].sudo()
        definitions = [
            (10, 'Confirm employee profile, branch and position', 'hr'),
            (20, 'Verify signed job offer and employment contract', 'hr'),
            (30, 'Review employee document checklist', 'hr'),
            (40, 'Deliver uniform and assigned equipment', 'manager'),
            (50, 'Complete workplace and policy orientation', 'manager'),
            (60, 'Complete role training', 'manager'),
            (70, 'Record first-week manager feedback', 'manager'),
        ]
        for onboarding in self:
            if onboarding.task_ids:
                continue
            Task.create([
                {
                    'onboarding_id': onboarding.id,
                    'sequence': sequence,
                    'name': name,
                    'responsible_role': role,
                    'mandatory': True,
                }
                for sequence, name, role in definitions
            ])

    def action_start(self):
        self.ensure_one()
        self._require_hr()
        if self.state != 'draft':
            raise UserError(self.env._('Only a draft onboarding can be started.'))
        if not self.branch_id or not self.job_id:
            raise ValidationError(self.env._(
                'Set the employee Restaurant Branch and Position before onboarding starts.'
            ))
        if not self.task_ids:
            raise ValidationError(self.env._('Add at least one onboarding task.'))
        self.with_context(restaurant_onboarding_internal=True).write({
            'state': 'in_progress',
            'started_by': self.env.uid,
            'started_at': fields.Datetime.now(),
        })
        self.message_post(body=self.env._('Onboarding started by %s.', self.env.user.name))
        return True

    def action_extend(self):
        self.ensure_one()
        self._require_hr()
        if self.state not in ONBOARDING_ACTIVE_STATES:
            raise UserError(self.env._('Only active onboarding can be extended.'))
        if not self.extension_reason:
            raise ValidationError(self.env._('Enter the extension reason.'))
        if not self.extension_end_date or self.extension_end_date <= self.planned_end_date:
            raise ValidationError(self.env._(
                'Choose an extension end date after the current planned end date.'
            ))
        self.with_context(restaurant_onboarding_internal=True).write({
            'state': 'extended',
            'planned_end_date': self.extension_end_date,
            'extended_by': self.env.uid,
            'extended_at': fields.Datetime.now(),
        })
        self.message_post(body=self.env._(
            'Onboarding extended to %(date)s by %(user)s. Reason: %(reason)s',
            date=self.planned_end_date,
            user=self.env.user.name,
            reason=self.extension_reason,
        ))
        return True

    def action_complete(self):
        self.ensure_one()
        self._require_hr()
        if self.state not in ONBOARDING_ACTIVE_STATES:
            raise UserError(self.env._('Only active onboarding can be completed.'))
        if self.pending_mandatory_count:
            raise ValidationError(self.env._(
                'Complete all required onboarding tasks first. %s task(s) remain.',
                self.pending_mandatory_count,
            ))
        self.with_context(restaurant_onboarding_internal=True).write({
            'state': 'completed',
            'completed_by': self.env.uid,
            'completed_at': fields.Datetime.now(),
        })
        self.message_post(body=self.env._(
            'Onboarding completed by %s.', self.env.user.name,
        ))
        return True

    def action_fail(self):
        self.ensure_one()
        self._require_hr()
        if self.state not in ONBOARDING_ACTIVE_STATES:
            raise UserError(self.env._('Only active onboarding can be marked Failed.'))
        if not self.failure_reason:
            raise ValidationError(self.env._('Enter the documented failure reason.'))
        self.with_context(restaurant_onboarding_internal=True).write({
            'state': 'failed',
            'failed_by': self.env.uid,
            'failed_at': fields.Datetime.now(),
        })
        self.message_post(body=self.env._(
            'Onboarding marked Failed by %(user)s. Reason: %(reason)s',
            user=self.env.user.name,
            reason=self.failure_reason,
        ))
        return True


class RestaurantEmployeeOnboardingTask(models.Model):
    _name = 'restaurant.employee.onboarding.task'
    _description = 'Restaurant Employee Onboarding Task'
    _order = 'sequence, id'

    onboarding_id = fields.Many2one(
        'restaurant.employee.onboarding', required=True, ondelete='cascade', index=True,
    )
    company_id = fields.Many2one(
        related='onboarding_id.company_id', store=True, index=True,
    )
    branch_id = fields.Many2one(
        related='onboarding_id.branch_id', store=True, index=True,
    )
    sequence = fields.Integer(default=10)
    name = fields.Char(required=True)
    responsible_role = fields.Selection([
        ('hr', 'HR'),
        ('manager', 'Branch Manager'),
    ], required=True, default='hr', index=True)
    mandatory = fields.Boolean(default=True)
    state = fields.Selection([
        ('pending', 'Pending'),
        ('done', 'Done'),
        ('failed', 'Failed'),
    ], required=True, default='pending', readonly=True, index=True)
    note = fields.Char()
    completed_by = fields.Many2one('res.users', readonly=True, copy=False)
    completed_at = fields.Datetime(readonly=True, copy=False)
    failure_reason = fields.Text(readonly=True, copy=False)
    failed_by = fields.Many2one('res.users', readonly=True, copy=False)
    failed_at = fields.Datetime(readonly=True, copy=False)
    can_complete = fields.Boolean(compute='_compute_can_complete')
    can_fail = fields.Boolean(compute='_compute_can_complete')

    @api.depends('state', 'responsible_role', 'onboarding_id.state', 'branch_id')
    @api.depends_context('uid')
    def _compute_can_complete(self):
        is_hr = self.env.su or self.env.user.has_group(HR_GROUP) or self.env.user.has_group(
            'base.group_system'
        )
        is_manager = self.env.user.has_group(MANAGER_GROUP)
        assigned_branch_ids = set(self.env.user.restaurant_branch_ids.ids)
        for task in self:
            role_allowed = (
                is_hr if task.responsible_role == 'hr'
                else is_manager and task.branch_id.id in assigned_branch_ids
            )
            task.can_complete = (
                task.state == 'pending'
                and task.onboarding_id.state in ONBOARDING_ACTIVE_STATES
                and role_allowed
            )
            task.can_fail = task.can_complete and task.responsible_role == 'manager'

    def _check_completion_role(self):
        for task in self:
            if task.responsible_role == 'hr':
                if not self.env.su and not (
                    self.env.user.has_group(HR_GROUP)
                    or self.env.user.has_group('base.group_system')
                ):
                    raise AccessError(self.env._('This onboarding task belongs to HR.'))
            else:
                if not self.env.user.has_group(MANAGER_GROUP) and not self.env.su:
                    raise AccessError(self.env._(
                        'This onboarding task belongs to the Branch Manager.'
                    ))
                require_assigned_branches(task.branch_id)

    @api.model_create_multi
    def create(self, vals_list):
        if not self.env.su and not (
            self.env.user.has_group(HR_GROUP)
            or self.env.user.has_group('base.group_system')
        ):
            raise AccessError(self.env._('Only HR can add onboarding tasks.'))
        return super().create(vals_list)

    def write(self, vals):
        if self.env.context.get('restaurant_onboarding_task_internal'):
            return super().write(vals)
        if not self.env.su and not (
            self.env.user.has_group(HR_GROUP)
            or self.env.user.has_group('base.group_system')
        ):
            raise AccessError(self.env._('Use the Complete button for assigned tasks.'))
        if any(task.onboarding_id.state != 'draft' for task in self):
            raise AccessError(self.env._('Checklist definitions are locked after onboarding starts.'))
        if set(vals) & {
            'state', 'completed_by', 'completed_at',
            'failure_reason', 'failed_by', 'failed_at',
        }:
            raise AccessError(self.env._('Use onboarding task workflow buttons.'))
        return super().write(vals)

    def unlink(self):
        if not self.env.su and not (
            self.env.user.has_group(HR_GROUP)
            or self.env.user.has_group('base.group_system')
        ):
            raise AccessError(self.env._('Only HR can remove onboarding tasks.'))
        if any(task.onboarding_id.state != 'draft' for task in self):
            raise AccessError(self.env._('Checklist tasks are locked after onboarding starts.'))
        return super().unlink()

    def action_mark_done(self):
        for task in self:
            if task.onboarding_id.state not in ONBOARDING_ACTIVE_STATES:
                raise UserError(self.env._('The onboarding is not active.'))
            if task.state == 'done':
                continue
            task._check_completion_role()
            task.with_context(restaurant_onboarding_task_internal=True).write({
                'state': 'done',
                'completed_by': self.env.uid,
                'completed_at': fields.Datetime.now(),
            })
        return True

    def action_open_failure_wizard(self):
        self.ensure_one()
        if self.onboarding_id.state not in ONBOARDING_ACTIVE_STATES:
            raise UserError(self.env._('The onboarding is not active.'))
        if self.state != 'pending':
            raise UserError(self.env._('Only a pending task can be marked Failed.'))
        if self.responsible_role != 'manager':
            raise AccessError(self.env._('Only Branch Manager tasks can report a failed result.'))
        self._check_completion_role()
        return {
            'type': 'ir.actions.act_window',
            'name': self.env._('Report Failed Onboarding Task'),
            'res_model': 'restaurant.onboarding.task.failure.wizard',
            'view_mode': 'form',
            'view_id': self.env.ref(
                'restaurant_reception.restaurant_onboarding_task_failure_wizard_view_form'
            ).id,
            'target': 'new',
            'context': {
                'default_task_id': self.id,
            },
        }

    def action_mark_failed(self, reason):
        self.ensure_one()
        if self.onboarding_id.state not in ONBOARDING_ACTIVE_STATES:
            raise UserError(self.env._('The onboarding is not active.'))
        if self.state != 'pending':
            raise UserError(self.env._('Only a pending task can be marked Failed.'))
        if self.responsible_role != 'manager':
            raise AccessError(self.env._('Only Branch Manager tasks can report a failed result.'))
        self._check_completion_role()
        failure_reason = (reason or '').strip()
        if not failure_reason:
            raise ValidationError(self.env._('Enter the reason the task failed.'))
        self.with_context(restaurant_onboarding_task_internal=True).write({
            'state': 'failed',
            'failure_reason': failure_reason,
            'failed_by': self.env.uid,
            'failed_at': fields.Datetime.now(),
            'completed_by': False,
            'completed_at': False,
        })
        self.onboarding_id.message_post(body=self.env._(
            'Branch Manager %(user)s reported task "%(task)s" as Failed. Reason: %(reason)s',
            user=self.env.user.name,
            task=self.name,
            reason=failure_reason,
        ))
        return True

    def action_reset(self):
        if not self.env.su and not (
            self.env.user.has_group(HR_GROUP)
            or self.env.user.has_group('base.group_system')
        ):
            raise AccessError(self.env._('Only HR can reopen an onboarding task.'))
        for task in self:
            if task.onboarding_id.state not in ONBOARDING_ACTIVE_STATES:
                raise UserError(self.env._('The onboarding is not active.'))
        self.with_context(restaurant_onboarding_task_internal=True).write({
            'state': 'pending',
            'completed_by': False,
            'completed_at': False,
            'failure_reason': False,
            'failed_by': False,
            'failed_at': False,
        })
        return True


class RestaurantOnboardingTaskFailureWizard(models.TransientModel):
    _name = 'restaurant.onboarding.task.failure.wizard'
    _description = 'Report Failed Onboarding Task'

    task_id = fields.Many2one(
        'restaurant.employee.onboarding.task', required=True, readonly=True,
        ondelete='cascade',
    )
    failure_reason = fields.Text(string='Failure Reason', required=True)

    def action_confirm(self):
        self.ensure_one()
        self.task_id.action_mark_failed(self.failure_reason)
        return {'type': 'ir.actions.act_window_close'}

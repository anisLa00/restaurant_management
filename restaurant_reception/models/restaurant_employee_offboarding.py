from odoo import api, fields, models
from odoo.exceptions import AccessError, UserError, ValidationError

from .reception_security import HR_GROUP, MANAGER_GROUP, require_assigned_branches


OFFBOARDING_ACTIVE_STATES = ('in_progress',)


class RestaurantEmployeeOffboarding(models.Model):
    _name = 'restaurant.employee.offboarding'
    _description = 'Restaurant Employee Offboarding'
    _inherit = ['mail.thread', 'mail.activity.mixin']
    _order = 'last_working_date desc, id desc'
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
    reason = fields.Selection([
        ('resignation', 'Resignation'),
        ('termination', 'Termination'),
        ('contract_end', 'End of Contract'),
        ('other', 'Other'),
    ], required=True, tracking=True)
    notice_date = fields.Date(required=True, default=fields.Date.context_today, tracking=True)
    last_working_date = fields.Date(required=True, tracking=True)
    reason_note = fields.Text()
    separation_document = fields.Binary(attachment=True, copy=False)
    separation_document_filename = fields.Char(copy=False)
    final_settlement_document = fields.Binary(attachment=True, copy=False)
    final_settlement_filename = fields.Char(copy=False)
    exit_clearance_document = fields.Binary(attachment=True, copy=False)
    exit_clearance_filename = fields.Char(copy=False)
    state = fields.Selection([
        ('draft', 'Draft'),
        ('in_progress', 'In Progress'),
        ('completed', 'Completed'),
        ('cancelled', 'Cancelled'),
    ], required=True, default='draft', readonly=True, copy=False, tracking=True, index=True)
    task_ids = fields.One2many(
        'restaurant.employee.offboarding.task', 'offboarding_id',
        string='Exit Checklist', copy=False,
    )
    progress_percent = fields.Float(compute='_compute_progress')
    pending_mandatory_count = fields.Integer(compute='_compute_progress')
    hr_note = fields.Text(string='HR Notes')
    started_by = fields.Many2one('res.users', readonly=True, copy=False)
    started_at = fields.Datetime(readonly=True, copy=False)
    completed_by = fields.Many2one('res.users', readonly=True, copy=False)
    completed_at = fields.Datetime(readonly=True, copy=False)
    cancelled_by = fields.Many2one('res.users', readonly=True, copy=False)
    cancelled_at = fields.Datetime(readonly=True, copy=False)
    cancellation_reason = fields.Text(copy=False)

    def _require_hr(self):
        if not self.env.su and not (
            self.env.user.has_group(HR_GROUP)
            or self.env.user.has_group('base.group_system')
        ):
            raise AccessError(self.env._('Only HR can manage employee offboarding.'))

    @api.depends('task_ids.state', 'task_ids.mandatory')
    def _compute_progress(self):
        for offboarding in self:
            tasks = offboarding.task_ids
            done = tasks.filtered(lambda task: task.state == 'done')
            offboarding.progress_percent = 100.0 * len(done) / len(tasks) if tasks else 0.0
            offboarding.pending_mandatory_count = len(tasks.filtered(
                lambda task: task.mandatory and task.state != 'done'
            ))

    @api.constrains('notice_date', 'last_working_date')
    def _check_dates(self):
        for record in self:
            if record.notice_date and record.last_working_date and record.last_working_date < record.notice_date:
                raise ValidationError(self.env._(
                    'The last working date cannot be before the notice date.'
                ))

    @api.constrains('employee_id', 'state')
    def _check_single_active_offboarding(self):
        for record in self:
            if record.state not in ('draft', 'in_progress'):
                continue
            duplicate = self.search_count([
                ('id', '!=', record.id),
                ('employee_id', '=', record.employee_id.id),
                ('state', 'in', ['draft', 'in_progress']),
            ])
            if duplicate:
                raise ValidationError(self.env._(
                    'An active offboarding record already exists for this employee.'
                ))

    @api.model_create_multi
    def create(self, vals_list):
        self._require_hr()
        sequence = self.env['ir.sequence'].sudo().search([
            ('code', '=', 'restaurant.employee.offboarding'),
            ('company_id', '=', False),
        ], limit=1)
        if not sequence:
            raise UserError(self.env._('The employee offboarding sequence is not configured.'))
        prepared = []
        for vals in vals_list:
            values = dict(vals)
            if values.get('state', 'draft') != 'draft':
                raise AccessError(self.env._('Offboarding must start in Draft.'))
            if not values.get('reference') or values.get('reference') in ('New', '/'):
                values['reference'] = sequence.next_by_id()
            values['state'] = 'draft'
            prepared.append(values)
        records = super().create(prepared)
        records._create_default_tasks()
        return records

    def write(self, vals):
        if self.env.context.get('restaurant_offboarding_internal'):
            return super().write(vals)
        self._require_hr()
        if 'state' in vals:
            raise AccessError(self.env._('Use offboarding workflow buttons to change status.'))
        if any(record.state in ('completed', 'cancelled') for record in self) and set(vals) - {'hr_note'}:
            raise AccessError(self.env._('Completed or cancelled offboarding records are locked.'))
        if any(record.state != 'draft' for record in self) and set(vals) & {
            'employee_id', 'reason', 'notice_date', 'last_working_date', 'task_ids',
        }:
            raise AccessError(self.env._('Core offboarding details cannot change after it starts.'))
        return super().write(vals)

    def unlink(self):
        self._require_hr()
        if any(record.state != 'draft' for record in self):
            raise AccessError(self.env._('Only draft offboarding records can be deleted.'))
        return super().unlink()

    def _create_default_tasks(self):
        definitions = [
            (10, 'Verify separation notice and last working date', 'hr'),
            (20, 'Complete duty and responsibility handover', 'manager'),
            (30, 'Return uniform, equipment, keys and company assets', 'manager'),
            (40, 'Confirm final attendance, leave and overtime inputs', 'hr'),
            (50, 'Prepare and approve final settlement', 'hr'),
            (60, 'Close visa, insurance and work-authorisation procedures', 'hr'),
            (70, 'Close system access and archive the employee file', 'hr'),
        ]
        Task = self.env['restaurant.employee.offboarding.task'].sudo()
        for offboarding in self:
            if offboarding.task_ids:
                continue
            Task.create([{
                'offboarding_id': offboarding.id,
                'sequence': sequence,
                'name': name,
                'responsible_role': role,
                'mandatory': True,
            } for sequence, name, role in definitions])

    def action_start(self):
        self.ensure_one()
        self._require_hr()
        if self.state != 'draft':
            raise UserError(self.env._('Only draft offboarding can be started.'))
        if not self.branch_id:
            raise ValidationError(self.env._('Set the employee Restaurant Branch first.'))
        if not self.separation_document:
            raise ValidationError(self.env._(
                'Upload the resignation, termination decision, or contract-end document first.'
            ))
        self.with_context(restaurant_offboarding_internal=True).write({
            'state': 'in_progress',
            'started_by': self.env.uid,
            'started_at': fields.Datetime.now(),
        })
        self.message_post(body=self.env._('Offboarding started by %s.', self.env.user.name))
        return True

    def action_complete(self):
        self.ensure_one()
        self._require_hr()
        if self.state != 'in_progress':
            raise UserError(self.env._('Only active offboarding can be completed.'))
        if self.pending_mandatory_count:
            raise ValidationError(self.env._(
                'Complete every required offboarding task first. %s task(s) remain.',
                self.pending_mandatory_count,
            ))
        if not self.final_settlement_document or not self.exit_clearance_document:
            raise ValidationError(self.env._(
                'Upload both the final settlement and signed exit clearance before completion.'
            ))
        self.with_context(restaurant_offboarding_internal=True).write({
            'state': 'completed',
            'completed_by': self.env.uid,
            'completed_at': fields.Datetime.now(),
        })
        self.employee_id.with_context(active_test=False).write({'active': False})
        self.message_post(body=self.env._(
            'Offboarding completed by %s. The employee was archived as a former employee.',
            self.env.user.name,
        ))
        return True

    def action_cancel(self):
        self.ensure_one()
        self._require_hr()
        if self.state not in ('draft', 'in_progress'):
            raise UserError(self.env._('Only draft or active offboarding can be cancelled.'))
        if not (self.cancellation_reason or '').strip():
            raise ValidationError(self.env._('Enter the cancellation reason.'))
        self.with_context(restaurant_offboarding_internal=True).write({
            'state': 'cancelled',
            'cancelled_by': self.env.uid,
            'cancelled_at': fields.Datetime.now(),
        })
        self.message_post(body=self.env._(
            'Offboarding cancelled by %(user)s: %(reason)s',
            user=self.env.user.name, reason=self.cancellation_reason,
        ))
        return True


class RestaurantEmployeeOffboardingTask(models.Model):
    _name = 'restaurant.employee.offboarding.task'
    _description = 'Restaurant Employee Offboarding Task'
    _order = 'sequence, id'

    offboarding_id = fields.Many2one(
        'restaurant.employee.offboarding', required=True, ondelete='cascade', index=True,
    )
    company_id = fields.Many2one(related='offboarding_id.company_id', store=True, index=True)
    branch_id = fields.Many2one(related='offboarding_id.branch_id', store=True, index=True)
    employee_id = fields.Many2one(related='offboarding_id.employee_id', store=True, index=True)
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
    ], required=True, default='pending', readonly=True, index=True)
    note = fields.Char()
    completed_by = fields.Many2one('res.users', readonly=True, copy=False)
    completed_at = fields.Datetime(readonly=True, copy=False)
    can_complete = fields.Boolean(compute='_compute_can_complete')

    @api.depends('state', 'responsible_role', 'offboarding_id.state', 'branch_id')
    @api.depends_context('uid')
    def _compute_can_complete(self):
        is_hr = self.env.su or self.env.user.has_group(HR_GROUP) or self.env.user.has_group('base.group_system')
        is_manager = self.env.user.has_group(MANAGER_GROUP)
        branch_ids = set(self.env.user.restaurant_branch_ids.ids)
        for task in self:
            allowed = is_hr or (
                task.responsible_role == 'manager'
                and is_manager
                and task.branch_id.id in branch_ids
            )
            task.can_complete = (
                task.state == 'pending'
                and task.offboarding_id.state in OFFBOARDING_ACTIVE_STATES
                and allowed
            )

    def _check_role(self):
        for task in self:
            if self.env.su or self.env.user.has_group('base.group_system') or self.env.user.has_group(HR_GROUP):
                continue
            if task.responsible_role != 'manager' or not self.env.user.has_group(MANAGER_GROUP):
                raise AccessError(self.env._('This offboarding task belongs to HR.'))
            require_assigned_branches(task.branch_id)

    @api.model_create_multi
    def create(self, vals_list):
        if not self.env.su and not (
            self.env.user.has_group(HR_GROUP) or self.env.user.has_group('base.group_system')
        ):
            raise AccessError(self.env._('Only HR can create offboarding tasks.'))
        return super().create(vals_list)

    def write(self, vals):
        if self.env.context.get('restaurant_offboarding_task_internal'):
            return super().write(vals)
        if set(vals) - {'note'}:
            raise AccessError(self.env._('Use the task workflow buttons.'))
        self._check_role()
        return super().write(vals)

    def unlink(self):
        if not self.env.su and not (
            self.env.user.has_group(HR_GROUP) or self.env.user.has_group('base.group_system')
        ):
            raise AccessError(self.env._('Only HR can delete draft offboarding tasks.'))
        if any(task.offboarding_id.state != 'draft' for task in self):
            raise AccessError(self.env._('Tasks cannot be deleted after offboarding starts.'))
        return super().unlink()

    def action_mark_done(self):
        self._check_role()
        for task in self:
            if task.offboarding_id.state != 'in_progress' or task.state != 'pending':
                raise UserError(self.env._('Only pending tasks in active offboarding can be completed.'))
        self.with_context(restaurant_offboarding_task_internal=True).write({
            'state': 'done',
            'completed_by': self.env.uid,
            'completed_at': fields.Datetime.now(),
        })
        return True

    def action_reset(self):
        if not self.env.su and not (
            self.env.user.has_group(HR_GROUP) or self.env.user.has_group('base.group_system')
        ):
            raise AccessError(self.env._('Only HR can reopen offboarding tasks.'))
        if any(task.offboarding_id.state != 'in_progress' for task in self):
            raise UserError(self.env._('Only tasks in active offboarding can be reopened.'))
        self.with_context(restaurant_offboarding_task_internal=True).write({
            'state': 'pending', 'completed_by': False, 'completed_at': False,
        })
        return True

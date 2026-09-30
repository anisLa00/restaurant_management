from odoo import api, fields, models
from odoo.exceptions import AccessError, UserError

from .reception_security import (
    HR_GROUP, MANAGER_GROUP, RECEPTION_GROUP, check_employee_company,
    lock_records, require_assigned_branches, require_draft, require_role,
)


class RestaurantAttendanceEntry(models.Model):
    _name = 'restaurant.attendance.entry'
    _description = 'Restaurant Attendance and Overtime Staging'
    _inherit = ['mail.thread', 'mail.activity.mixin']
    _rec_name = 'employee_id'
    _order = 'attendance_date desc, id desc'
    _mail_post_access = 'read'

    employee_id = fields.Many2one('hr.employee', required=True, ondelete='restrict', index=True, tracking=True)
    branch_id = fields.Many2one('restaurant.branch', required=True, ondelete='restrict', index=True, tracking=True)
    attendance_date = fields.Date(required=True, default=fields.Date.context_today, index=True, tracking=True)
    status = fields.Selection([
        ('present', 'Present'), ('absent', 'Absent'), ('late', 'Late'),
        ('day_off', 'Day Off'), ('sick_leave', 'Sick Leave'),
        ('annual_leave', 'Annual Leave'), ('emergency_leave', 'Emergency Leave'),
    ], required=True, default='present', tracking=True)
    check_in = fields.Datetime(tracking=True)
    check_out = fields.Datetime(tracking=True)
    overtime_hours = fields.Float(default=0, required=True, tracking=True)
    reception_note = fields.Text(tracking=True)
    manager_note = fields.Text(tracking=True)
    hr_note = fields.Text(tracking=True)
    entered_by = fields.Many2one('res.users', required=True, readonly=True, default=lambda self: self.env.user)
    manager_reviewed_by = fields.Many2one('res.users', readonly=True, copy=False, tracking=True)
    hr_reviewed_by = fields.Many2one('res.users', readonly=True, copy=False, tracking=True)
    manager_reviewed_at = fields.Datetime(readonly=True, copy=False, tracking=True)
    hr_reviewed_at = fields.Datetime(readonly=True, copy=False, tracking=True)
    state = fields.Selection([
        ('draft', 'Draft'), ('manager_review', 'Manager Review'),
        ('submitted_to_hr', 'Submitted to HR'), ('approved', 'Approved'), ('rejected', 'Rejected'),
    ], required=True, default='draft', readonly=True, copy=False, tracking=True)
    company_id = fields.Many2one(related='branch_id.company_id', store=True, index=True)
    can_review_manager = fields.Boolean(compute='_compute_review_permissions')
    can_review_hr = fields.Boolean(compute='_compute_review_permissions')

    _employee_branch_date_unique = models.Constraint(
        'UNIQUE(employee_id, branch_id, attendance_date)',
        'An attendance entry already exists for this employee, branch and date.',
    )
    _overtime_nonnegative = models.Constraint('CHECK (overtime_hours >= 0)', 'Overtime cannot be negative.')
    _check_time_order = models.Constraint(
        'CHECK (check_out IS NULL OR check_in IS NULL OR check_out >= check_in)',
        'Check-out cannot be before check-in.',
    )

    @api.depends_context('uid')
    def _compute_review_permissions(self):
        manager = self.env.user.has_group(MANAGER_GROUP)
        hr = self.env.user.has_group(HR_GROUP) and not manager
        for entry in self:
            entry.can_review_manager = manager
            entry.can_review_hr = hr

    @api.constrains('employee_id', 'branch_id')
    def _check_employee_company(self):
        check_employee_company(self)

    @api.model_create_multi
    def create(self, vals_list):
        require_role(self.env, RECEPTION_GROUP)
        editable = {'employee_id', 'branch_id', 'attendance_date', 'status', 'check_in',
                    'check_out', 'overtime_hours', 'reception_note', 'state'}
        prepared = []
        for values in vals_list:
            values = dict(values)
            if values.get('state', 'draft') != 'draft' or set(values) - editable:
                raise AccessError(self.env._('Attendance must start in draft without review fields.'))
            values.update({
                'state': 'draft', 'entered_by': self.env.uid,
                'manager_reviewed_by': False, 'hr_reviewed_by': False,
                'manager_reviewed_at': False, 'hr_reviewed_at': False,
                'manager_note': False, 'hr_note': False,
            })
            prepared.append(values)
        records = super().create(prepared)
        require_assigned_branches(records.branch_id)
        return records

    def write(self, vals):
        lock_records(self)
        if 'state' in vals:
            if set(vals) != {'state'}:
                raise AccessError(self.env._('Save your notes before changing the workflow state.'))
            target = vals['state']
            for entry in self:
                entry._check_transition(target)
            values = dict(vals)
            if target in ('submitted_to_hr', 'draft'):
                values.update(manager_reviewed_by=self.env.uid, manager_reviewed_at=fields.Datetime.now())
            elif target in ('approved', 'rejected'):
                values.update(hr_reviewed_by=self.env.uid, hr_reviewed_at=fields.Datetime.now())
            result = super().write(values)
            for entry in self:
                entry.message_post(body=self.env._('Workflow changed to %s by %s.',
                    dict(self._fields['state'].selection)[target], self.env.user.name))
            return result
        operational = {'employee_id', 'branch_id', 'attendance_date', 'status', 'check_in',
                       'check_out', 'overtime_hours', 'reception_note'}
        if not set(vals) - operational:
            require_role(self.env, RECEPTION_GROUP)
            require_draft(self)
            require_assigned_branches(self.branch_id)
            if 'branch_id' in vals:
                require_assigned_branches(self.env['restaurant.branch'].browse(vals['branch_id']))
        elif set(vals) == {'manager_note'}:
            require_role(self.env, MANAGER_GROUP)
            require_assigned_branches(self.branch_id)
            if any(entry.state != 'manager_review' for entry in self):
                raise AccessError(self.env._('Manager notes can only be edited during manager review.'))
        elif set(vals) == {'hr_note'}:
            self._require_hr_reviewer()
            if any(entry.state != 'submitted_to_hr' for entry in self):
                raise AccessError(self.env._('HR notes can only be edited while submitted to HR.'))
        else:
            raise AccessError(self.env._('Review and system fields cannot be modified manually.'))
        return super().write(vals)

    def unlink(self):
        lock_records(self, 'unlink')
        require_role(self.env, RECEPTION_GROUP)
        require_draft(self)
        require_assigned_branches(self.branch_id)
        return super().unlink()

    def _require_hr_reviewer(self):
        require_role(self.env, HR_GROUP)
        if not self.env.su and self.env.user.has_group(MANAGER_GROUP):
            raise AccessError(self.env._('Branch managers cannot perform HR approval or rejection.'))
        if not self.env.su and any(
            entry.manager_reviewed_by == self.env.user for entry in self
        ):
            raise AccessError(self.env._(
                'You cannot perform HR approval or rejection on an entry you reviewed as manager.'
            ))

    def _check_transition(self, target):
        self.ensure_one()
        transitions = {
            ('draft', 'manager_review'): RECEPTION_GROUP,
            ('manager_review', 'submitted_to_hr'): MANAGER_GROUP,
            ('manager_review', 'draft'): MANAGER_GROUP,
            ('submitted_to_hr', 'approved'): HR_GROUP,
            ('submitted_to_hr', 'rejected'): HR_GROUP,
        }
        group = transitions.get((self.state, target))
        if not group:
            raise UserError(self.env._('This attendance workflow transition is not allowed.'))
        if group == HR_GROUP:
            self._require_hr_reviewer()
        else:
            require_role(self.env, group)
            require_assigned_branches(self.branch_id)

    def action_submit(self):
        self.ensure_one()
        return self.write({'state': 'manager_review'})

    def action_submit_to_hr(self):
        self.ensure_one()
        return self.write({'state': 'submitted_to_hr'})

    def action_return_to_draft(self):
        self.ensure_one()
        return self.write({'state': 'draft'})

    def action_approve(self):
        self.ensure_one()
        return self.write({'state': 'approved'})

    def action_reject(self):
        self.ensure_one()
        return self.write({'state': 'rejected'})

import logging

from odoo import api, fields, models
from odoo.exceptions import AccessError, UserError, ValidationError

from .reception_security import (
    HR_GROUP, MANAGER_GROUP, RECEPTION_GROUP, check_employee_company,
    lock_records, require_assigned_branches, require_draft, require_role,
)


_logger = logging.getLogger(__name__)


class RestaurantAttendanceEntry(models.Model):
    _name = 'restaurant.attendance.entry'
    _description = 'Restaurant Attendance and Overtime'
    _inherit = ['mail.thread', 'mail.activity.mixin']
    _rec_name = 'employee_id'
    _order = 'attendance_date desc, id desc'
    _mail_post_access = 'read'

    employee_id = fields.Many2one(
        'hr.employee', required=True, ondelete='restrict', index=True, tracking=True,
    )
    branch_id = fields.Many2one(
        'restaurant.branch', required=True, ondelete='restrict', index=True, tracking=True,
    )
    attendance_date = fields.Date(
        required=True, default=fields.Date.context_today, index=True, tracking=True,
    )
    status = fields.Selection([
        ('present', 'Present'), ('absent', 'Absent'), ('late', 'Late'),
        ('day_off', 'Day Off'), ('sick_leave', 'Sick Leave'),
        ('annual_leave', 'Annual Leave'), ('emergency_leave', 'Emergency Leave'),
    ], required=True, default='present', index=True, tracking=True)
    check_in = fields.Datetime(tracking=True)
    check_out = fields.Datetime(tracking=True)
    overtime_hours = fields.Float(default=0, required=True, tracking=True)
    reception_note = fields.Text(tracking=True)
    manager_note = fields.Text(tracking=True)
    hr_note = fields.Text(tracking=True)
    decision_reason = fields.Text(
        tracking=True,
        help='Required for HR rejection or return to Reception.',
    )
    last_hr_return_reason = fields.Text(readonly=True, copy=False, tracking=True)
    official_leave_type_id = fields.Many2one(
        'hr.work.entry.type', string='Official Leave Type', copy=False, tracking=True,
        ondelete='restrict', domain="[('time_off_selectable', '=', True)]",
        help='Optional HR override. If empty, the company mapping is used.',
    )
    entered_by = fields.Many2one(
        'res.users', required=True, readonly=True, default=lambda self: self.env.user,
    )
    manager_reviewed_by = fields.Many2one(
        'res.users', readonly=True, copy=False, tracking=True,
    )
    hr_reviewed_by = fields.Many2one(
        'res.users', readonly=True, copy=False, tracking=True,
    )
    manager_reviewed_at = fields.Datetime(readonly=True, copy=False, tracking=True)
    hr_reviewed_at = fields.Datetime(readonly=True, copy=False, tracking=True)
    hr_returned_by = fields.Many2one(
        'res.users', readonly=True, copy=False, tracking=True,
    )
    hr_returned_at = fields.Datetime(readonly=True, copy=False, tracking=True)
    hr_return_count = fields.Integer(readonly=True, copy=False, tracking=True)
    state = fields.Selection([
        ('draft', 'Draft'), ('manager_review', 'Manager Review'),
        ('submitted_to_hr', 'Submitted to HR'), ('approved', 'Approved'),
        ('rejected', 'Rejected'),
    ], required=True, default='draft', readonly=True, copy=False, index=True, tracking=True)
    company_id = fields.Many2one(
        related='branch_id.company_id', store=True, index=True,
    )

    sync_status = fields.Selection([
        ('pending', 'Pending'),
        ('synced', 'Synced'),
        ('not_required', 'No Official Record Required'),
        ('error', 'Sync Error'),
        ('legacy', 'Legacy Decision'),
    ], required=True, default='pending', readonly=True, copy=False, index=True, tracking=True)
    sync_error = fields.Text(readonly=True, copy=False, tracking=True)
    sync_attempt_count = fields.Integer(readonly=True, copy=False, tracking=True)
    last_sync_attempt_at = fields.Datetime(readonly=True, copy=False, tracking=True)
    last_sync_attempt_by = fields.Many2one(
        'res.users', readonly=True, copy=False, tracking=True,
    )
    official_attendance_id = fields.Many2one(
        'hr.attendance', readonly=True, copy=False, ondelete='restrict', tracking=True,
    )
    official_leave_id = fields.Many2one(
        'hr.leave', readonly=True, copy=False, ondelete='restrict', tracking=True,
    )
    overtime_ledger_id = fields.Many2one(
        'restaurant.overtime.ledger', readonly=True, copy=False,
        ondelete='restrict', tracking=True,
    )

    can_review_manager = fields.Boolean(compute='_compute_review_permissions')
    can_review_hr = fields.Boolean(compute='_compute_review_permissions')

    _employee_branch_date_unique = models.Constraint(
        'UNIQUE(employee_id, branch_id, attendance_date)',
        'An attendance entry already exists for this employee, branch and date.',
    )
    _overtime_nonnegative = models.Constraint(
        'CHECK (overtime_hours >= 0)', 'Overtime cannot be negative.',
    )
    _check_time_order = models.Constraint(
        'CHECK (check_out IS NULL OR check_in IS NULL OR check_out >= check_in)',
        'Check-out cannot be before check-in.',
    )

    @api.depends('manager_reviewed_by')
    @api.depends_context('uid')
    def _compute_review_permissions(self):
        manager = self.env.user.has_group(MANAGER_GROUP)
        hr = self.env.user.has_group(HR_GROUP)
        for entry in self:
            entry.can_review_manager = manager
            entry.can_review_hr = hr and entry.manager_reviewed_by != self.env.user

    @api.constrains('employee_id', 'branch_id')
    def _check_employee_company(self):
        check_employee_company(self)

    @api.constrains('official_leave_type_id', 'status', 'company_id')
    def _check_official_leave_type(self):
        leave_statuses = {'annual_leave', 'sick_leave', 'emergency_leave'}
        for entry in self:
            leave_type = entry.official_leave_type_id
            if not leave_type:
                continue
            if entry.status not in leave_statuses:
                raise ValidationError(self.env._(
                    'An official leave type can only be selected for a leave status.'
                ))
            if not leave_type.time_off_selectable:
                raise ValidationError(self.env._(
                    'The official leave type must be selectable in Time Off.'
                ))
            if leave_type.country_id and leave_type.country_id != entry.company_id.country_id:
                raise ValidationError(self.env._(
                    'The official leave type must match the entry company country.'
                ))

    @api.model_create_multi
    def create(self, vals_list):
        require_role(self.env, RECEPTION_GROUP)
        editable = {
            'employee_id', 'branch_id', 'attendance_date', 'status', 'check_in',
            'check_out', 'overtime_hours', 'reception_note', 'state',
        }
        prepared = []
        for values in vals_list:
            values = dict(values)
            if values.get('state', 'draft') != 'draft' or set(values) - editable:
                raise AccessError(self.env._(
                    'Attendance must start in draft without review or sync fields.'
                ))
            values.update({
                'state': 'draft',
                'sync_status': 'pending',
                'entered_by': self.env.uid,
                'manager_reviewed_by': False,
                'hr_reviewed_by': False,
                'manager_reviewed_at': False,
                'hr_reviewed_at': False,
                'manager_note': False,
                'hr_note': False,
                'decision_reason': False,
                'official_leave_type_id': False,
            })
            prepared.append(values)
        records = super().create(prepared)
        require_assigned_branches(records.branch_id)
        return records

    def write(self, vals):
        lock_records(self)
        if 'state' in vals:
            raise AccessError(self.env._('Use the attendance workflow actions to change state.'))

        fields_to_write = set(vals)
        operational = {
            'employee_id', 'branch_id', 'attendance_date', 'status', 'check_in',
            'check_out', 'overtime_hours', 'reception_note',
        }
        hr_editable = {'hr_note', 'decision_reason', 'official_leave_type_id'}

        if fields_to_write and fields_to_write <= operational:
            require_role(self.env, RECEPTION_GROUP)
            require_draft(self)
            require_assigned_branches(self.branch_id)
            if 'branch_id' in vals:
                require_assigned_branches(
                    self.env['restaurant.branch'].browse(vals['branch_id'])
                )
            values = dict(vals)
            if 'status' in values and any(entry.status != values['status'] for entry in self):
                values['official_leave_type_id'] = False
            return super().write(values)

        if fields_to_write == {'manager_note'}:
            require_role(self.env, MANAGER_GROUP)
            require_assigned_branches(self.branch_id)
            if any(entry.state != 'manager_review' for entry in self):
                raise AccessError(self.env._(
                    'Manager notes can only be edited during manager review.'
                ))
            return super().write(vals)

        if fields_to_write and fields_to_write <= hr_editable:
            self._require_hr_reviewer()
            if any(entry.state != 'submitted_to_hr' for entry in self):
                raise AccessError(self.env._(
                    'HR fields can only be edited while awaiting HR.'
                ))
            return super().write(vals)

        raise AccessError(self.env._(
            'Review, audit, link, and sync fields cannot be modified manually.'
        ))

    def unlink(self):
        lock_records(self, 'unlink')
        require_role(self.env, RECEPTION_GROUP)
        require_draft(self)
        require_assigned_branches(self.branch_id)
        return super().unlink()

    def _require_hr_reviewer(self):
        require_role(self.env, HR_GROUP)
        if not self.env.su and any(
            entry.manager_reviewed_by == self.env.user for entry in self
        ):
            raise AccessError(self.env._(
                'You cannot make the HR decision on an entry you reviewed as manager.'
            ))

    def _check_transition(self, target):
        self.ensure_one()
        transitions = {
            ('draft', 'manager_review'): RECEPTION_GROUP,
            ('manager_review', 'submitted_to_hr'): MANAGER_GROUP,
            ('manager_review', 'draft'): MANAGER_GROUP,
            ('submitted_to_hr', 'draft'): HR_GROUP,
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

    def _transition(self, target, extra_values=None):
        self.ensure_one()
        self._check_transition(target)
        source = self.state
        now = fields.Datetime.now()
        values = {'state': target, **(extra_values or {})}

        if target == 'submitted_to_hr':
            values.update({
                'manager_reviewed_by': self.env.uid,
                'manager_reviewed_at': now,
                'decision_reason': False,
                'sync_status': 'pending',
                'sync_error': False,
            })
        elif target in ('approved', 'rejected'):
            values.update({
                'hr_reviewed_by': self.env.uid,
                'hr_reviewed_at': now,
            })
        elif source == 'submitted_to_hr' and target == 'draft':
            values.update({
                'hr_returned_by': self.env.uid,
                'hr_returned_at': now,
                'hr_return_count': self.hr_return_count + 1,
                'last_hr_return_reason': self.decision_reason,
                'sync_status': 'pending',
                'sync_error': False,
            })

        result = super(RestaurantAttendanceEntry, self).write(values)
        label = dict(self._fields['state']._description_selection(self.env))[target]
        self.sudo().message_post(
            body=self.env._('Workflow changed to %(state)s by %(user)s.',
                            state=label, user=self.env.user.name),
            author_id=self.env.user.partner_id.id,
        )
        return result

    def action_submit(self):
        self.ensure_one()
        lock_records(self)
        return self._transition('manager_review')

    def action_submit_to_hr(self):
        self.ensure_one()
        lock_records(self)
        return self._transition('submitted_to_hr')

    def action_return_to_draft(self):
        self.ensure_one()
        lock_records(self)
        return self._transition('draft')

    def action_return_to_reception(self):
        self.ensure_one()
        lock_records(self)
        self._require_hr_reviewer()
        if self.state != 'submitted_to_hr':
            raise UserError(self.env._('Only an entry awaiting HR can be returned.'))
        if not (self.decision_reason or '').strip():
            raise ValidationError(self.env._(
                'Enter a decision reason before returning the entry to Reception.'
            ))
        return self._transition('draft')

    def action_reject(self):
        self.ensure_one()
        lock_records(self)
        self._require_hr_reviewer()
        if self.state != 'submitted_to_hr':
            raise UserError(self.env._('Only an entry awaiting HR can be rejected.'))
        if not (self.decision_reason or '').strip():
            raise ValidationError(self.env._(
                'Enter a decision reason before rejecting the entry.'
            ))
        return self._transition('rejected', {
            'sync_status': 'not_required',
            'sync_error': False,
        })

    def action_approve(self):
        self.ensure_one()
        return self._attempt_approval_sync(retry=False)

    def action_retry_sync(self):
        self.ensure_one()
        if self.sync_status != 'error':
            raise UserError(self.env._('Only a failed HR sync can be retried.'))
        return self._attempt_approval_sync(retry=True)

    def _attempt_approval_sync(self, retry=False):
        self.ensure_one()
        lock_records(self)
        self._require_hr_reviewer()
        if self.state != 'submitted_to_hr':
            raise UserError(self.env._('Only an entry awaiting HR can be approved.'))
        if retry and self.sync_status != 'error':
            raise UserError(self.env._('Only a failed HR sync can be retried.'))

        now = fields.Datetime.now()
        super(RestaurantAttendanceEntry, self).write({
            'sync_attempt_count': self.sync_attempt_count + 1,
            'last_sync_attempt_at': now,
            'last_sync_attempt_by': self.env.uid,
            'sync_status': 'pending',
            'sync_error': False,
        })

        try:
            with self.env.cr.savepoint():
                sync_status = self._sync_official_records()
                self._transition('approved', {
                    'sync_status': sync_status,
                    'sync_error': False,
                })
        except Exception as error:  # noqa: BLE001 - the failure must remain retryable
            _logger.exception('Restaurant HR sync failed for attendance entry %s', self.id)
            self.invalidate_recordset()
            error_message = str(error)[:2000] or error.__class__.__name__
            super(RestaurantAttendanceEntry, self).write({
                'sync_status': 'error',
                'sync_error': error_message,
            })
            self.sudo().message_post(
                body=self.env._('Official HR sync failed: %s', error_message),
                author_id=self.env.user.partner_id.id,
            )
            return {
                'type': 'ir.actions.client',
                'tag': 'display_notification',
                'params': {
                    'title': self.env._('HR Sync Failed'),
                    'message': error_message,
                    'type': 'danger',
                    'sticky': True,
                },
            }

        return {
            'type': 'ir.actions.client',
            'tag': 'display_notification',
            'params': {
                'title': self.env._('Attendance Approved'),
                'message': self.env._('Official HR records were synchronized successfully.'),
                'type': 'success',
                'sticky': False,
            },
        }

    def _sync_official_records(self):
        self.ensure_one()
        needs_attendance = self.status in ('present', 'late')
        needs_leave = self.status in ('annual_leave', 'sick_leave', 'emergency_leave')
        needs_overtime = self.overtime_hours > 0

        if (
            (needs_attendance or needs_leave or needs_overtime)
            and not self.company_id.restaurant_hr_official_sync_enabled
        ):
            raise ValidationError(self.env._(
                'Official Restaurant HR sync is disabled for %(company)s. '
                'Enable it in Restaurant HR settings after configuring leave mappings.',
                company=self.company_id.display_name,
            ))

        if needs_attendance:
            self._sync_attendance()
        if needs_leave:
            self._sync_leave()
        if needs_overtime:
            self._sync_overtime()

        return 'synced' if (needs_attendance or needs_leave or needs_overtime) else 'not_required'

    def _sync_attendance(self):
        self.ensure_one()
        if not self.check_in or not self.check_out:
            raise ValidationError(self.env._(
                'Present and late entries require both check-in and check-out.'
            ))
        if self.check_out <= self.check_in:
            raise ValidationError(self.env._('Check-out must be after check-in.'))

        attendance_model = self.env['hr.attendance'].sudo().with_company(
            self.company_id
        ).with_context(restaurant_hr_sync=True)
        attendance = self.official_attendance_id.sudo().exists()
        if not attendance:
            attendance = attendance_model.search([
                ('restaurant_attendance_entry_id', '=', self.id),
            ], limit=1)

        values = {
            'check_in': self.check_in,
            'check_out': self.check_out,
            'restaurant_attendance_entry_id': self.id,
        }
        if attendance:
            if attendance.employee_id != self.employee_id:
                raise ValidationError(self.env._(
                    'The linked official attendance belongs to another employee.'
                ))
            # Existing official attendances may already have time-rule output
            # slices that overlap their source. Core Attendance provides the
            # skip_time_rules update path for this case and marks surviving
            # outputs stale for the standard review pipeline.
            attendance.with_context(
                restaurant_hr_sync=True,
                skip_time_rules=True,
            ).write(values)
        else:
            attendance = attendance_model.create({
                **values,
                'employee_id': self.employee_id.id,
            })
        super(RestaurantAttendanceEntry, self).write({
            'official_attendance_id': attendance.id,
        })

    def _mapped_leave_type(self):
        self.ensure_one()
        mapping = {
            'annual_leave': self.company_id.restaurant_annual_leave_type_id,
            'sick_leave': self.company_id.restaurant_sick_leave_type_id,
            'emergency_leave': self.company_id.restaurant_emergency_leave_type_id,
        }
        return (self.official_leave_type_id or mapping.get(self.status)).sudo()

    def _sync_leave(self):
        self.ensure_one()
        leave_type = self._mapped_leave_type()
        if not leave_type:
            raise ValidationError(self.env._(
                'Configure the %(status)s Time Off type for %(company)s or select '
                'an official leave type on this entry.',
                status=dict(self._fields['status']._description_selection(self.env))[self.status],
                company=self.company_id.display_name,
            ))
        if not leave_type.time_off_selectable:
            raise ValidationError(self.env._(
                'The configured leave type is not selectable in Time Off.'
            ))
        if leave_type.country_id and leave_type.country_id != self.company_id.country_id:
            raise ValidationError(self.env._(
                'The configured leave type does not match the entry company country.'
            ))

        leave_model = self.env['hr.leave'].sudo().with_company(self.company_id).with_context(
            restaurant_hr_sync=True,
            leave_fast_create=True,
        )
        leave = self.official_leave_id.sudo().exists()
        if not leave:
            leave = leave_model.search([
                ('restaurant_attendance_entry_id', '=', self.id),
            ], limit=1)
        values = {
            'employee_id': self.employee_id.id,
            'work_entry_type_id': leave_type.id,
            'request_date_from': self.attendance_date,
            'request_date_to': self.attendance_date,
            'notes': self.hr_note or self.reception_note or self.env._(
                'Submitted from restaurant attendance entry.'
            ),
            'restaurant_attendance_entry_id': self.id,
        }
        if leave:
            if leave.employee_id != self.employee_id:
                raise ValidationError(self.env._(
                    'The linked Time Off request belongs to another employee.'
                ))
        else:
            leave = leave_model.create(values)
            if not leave:
                raise ValidationError(self.env._(
                    'Odoo could not create a valid Time Off request. Check allocations '
                    'and the employee work schedule.'
                ))
            # Avoid sudo's officer auto-approval while still using standard
            # Time Off validity, follower, and approval-activity behavior.
            leave.sudo().add_follower(self.employee_id.id)
            if leave.validation_type == 'manager':
                leave.sudo().message_subscribe(
                    partner_ids=self.employee_id.leave_manager_id.partner_id.ids,
                )
            leave.sudo().activity_update()
        if leave.state not in ('confirm', 'validate1', 'validate'):
            raise ValidationError(self.env._(
                'The linked Time Off request is not in an approvable or approved state.'
            ))
        super(RestaurantAttendanceEntry, self).write({
            'official_leave_type_id': leave_type.id,
            'official_leave_id': leave.id,
        })

    def _sync_overtime(self):
        self.ensure_one()
        ledger_model = self.env['restaurant.overtime.ledger'].sudo().with_company(
            self.company_id
        ).with_context(restaurant_hr_sync=True)
        ledger = self.overtime_ledger_id.sudo().exists()
        if not ledger:
            ledger = ledger_model.search([('source_entry_id', '=', self.id)], limit=1)
        values = {
            'source_entry_id': self.id,
            'employee_id': self.employee_id.id,
            'company_id': self.company_id.id,
            'branch_id': self.branch_id.id,
            'attendance_date': self.attendance_date,
            'hours': self.overtime_hours,
            'approved_by': self.env.uid,
            'approved_at': fields.Datetime.now(),
        }
        if ledger:
            ledger.with_context(restaurant_hr_sync=True).write(values)
        else:
            ledger = ledger_model.create(values)
        super(RestaurantAttendanceEntry, self).write({
            'overtime_ledger_id': ledger.id,
        })

    def action_open_official_attendance(self):
        self.ensure_one()
        if not self.official_attendance_id:
            raise UserError(self.env._('No official attendance is linked.'))
        return {
            'type': 'ir.actions.act_window',
            'name': self.env._('Official Attendance'),
            'res_model': 'hr.attendance',
            'res_id': self.official_attendance_id.id,
            'view_mode': 'form',
        }

    def action_open_official_leave(self):
        self.ensure_one()
        if not self.official_leave_id:
            raise UserError(self.env._('No Time Off request is linked.'))
        return {
            'type': 'ir.actions.act_window',
            'name': self.env._('Time Off Request'),
            'res_model': 'hr.leave',
            'res_id': self.official_leave_id.id,
            'view_mode': 'form',
        }

    def action_open_overtime_ledger(self):
        self.ensure_one()
        if not self.overtime_ledger_id:
            raise UserError(self.env._('No overtime ledger row is linked.'))
        return {
            'type': 'ir.actions.act_window',
            'name': self.env._('Approved Overtime'),
            'res_model': 'restaurant.overtime.ledger',
            'res_id': self.overtime_ledger_id.id,
            'view_mode': 'form',
        }

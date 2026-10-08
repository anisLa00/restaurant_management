import calendar
from datetime import date

from odoo import api, fields, models
from odoo.exceptions import AccessError, UserError, ValidationError

from .reception_security import (
    HR_GROUP,
    MANAGER_GROUP,
    RECEPTION_GROUP,
    lock_records,
    require_assigned_branches,
    require_role,
)


RESOLVED_STATES = {'approved', 'rejected'}


class RestaurantAttendanceSheet(models.Model):
    _name = 'restaurant.attendance.sheet'
    _description = 'Restaurant Daily Attendance Sheet'
    _inherit = ['mail.thread', 'mail.activity.mixin']
    _order = 'attendance_date desc, branch_id, id desc'
    _mail_post_access = 'read'

    name = fields.Char(compute='_compute_name', store=True)
    branch_id = fields.Many2one(
        'restaurant.branch', required=True, ondelete='restrict', index=True,
        tracking=True,
    )
    attendance_date = fields.Date(
        required=True, default=fields.Date.context_today, index=True, tracking=True,
    )
    company_id = fields.Many2one(
        related='branch_id.company_id', store=True, index=True,
    )
    line_ids = fields.One2many(
        'restaurant.attendance.entry', 'sheet_id', string='All Staff', copy=False,
    )
    exception_line_ids = fields.One2many(
        'restaurant.attendance.entry', 'sheet_id', string='Exceptions', copy=False,
        domain=[('is_exception', '=', True)],
    )
    state = fields.Selection([
        ('draft', 'Reception Entry'),
        ('manager_review', 'Manager Review'),
        ('submitted_to_hr', 'HR Review'),
        ('approved', 'Approved'),
    ], required=True, default='draft', readonly=True, copy=False, index=True,
        tracking=True)

    submitted_by = fields.Many2one('res.users', readonly=True, copy=False)
    submitted_at = fields.Datetime(readonly=True, copy=False)
    manager_submitted_by = fields.Many2one('res.users', readonly=True, copy=False)
    manager_submitted_at = fields.Datetime(readonly=True, copy=False)
    approved_by = fields.Many2one('res.users', readonly=True, copy=False)
    approved_at = fields.Datetime(readonly=True, copy=False)
    last_roster_refresh_by = fields.Many2one('res.users', readonly=True, copy=False)
    last_roster_refresh_at = fields.Datetime(readonly=True, copy=False)

    total_count = fields.Integer(compute='_compute_summary')
    present_count = fields.Integer(compute='_compute_summary')
    late_count = fields.Integer(compute='_compute_summary')
    absent_count = fields.Integer(compute='_compute_summary')
    day_off_count = fields.Integer(compute='_compute_summary')
    leave_count = fields.Integer(compute='_compute_summary')
    pending_count = fields.Integer(compute='_compute_summary')
    overtime_count = fields.Integer(compute='_compute_summary')
    exception_count = fields.Integer(compute='_compute_summary')
    unresolved_count = fields.Integer(compute='_compute_summary')
    roster_unlinked_employee_count = fields.Integer(compute='_compute_roster_warning')
    roster_warning = fields.Text(compute='_compute_roster_warning')

    _branch_date_unique = models.Constraint(
        'UNIQUE(branch_id, attendance_date)',
        'Only one daily attendance sheet is allowed per branch and date.',
    )

    @api.depends('branch_id', 'attendance_date')
    def _compute_name(self):
        for sheet in self:
            sheet.name = (
                f'{sheet.branch_id.display_name} - {sheet.attendance_date}'
                if sheet.branch_id and sheet.attendance_date
                else self.env._('New Daily Attendance Sheet')
            )

    @api.depends(
        'line_ids.status', 'line_ids.state', 'line_ids.overtime_hours',
        'line_ids.is_exception',
    )
    def _compute_summary(self):
        leave_statuses = {'annual_leave', 'sick_leave', 'emergency_leave'}
        for sheet in self:
            lines = sheet.line_ids
            sheet.total_count = len(lines)
            sheet.present_count = len(lines.filtered(lambda line: line.status == 'present'))
            sheet.late_count = len(lines.filtered(lambda line: line.status == 'late'))
            sheet.absent_count = len(lines.filtered(lambda line: line.status == 'absent'))
            sheet.day_off_count = len(lines.filtered(lambda line: line.status == 'day_off'))
            sheet.leave_count = len(lines.filtered(lambda line: line.status in leave_statuses))
            sheet.pending_count = len(lines.filtered(lambda line: line.status == 'pending'))
            sheet.overtime_count = len(lines.filtered(lambda line: line.overtime_hours > 0))
            sheet.exception_count = len(lines.filtered('is_exception'))
            sheet.unresolved_count = len(lines.filtered(
                lambda line: line.state not in RESOLVED_STATES
            ))

    @api.depends('company_id', 'branch_id')
    def _compute_roster_warning(self):
        Employee = self.env['hr.employee'].sudo().with_context(active_test=False)
        for sheet in self:
            if not sheet.company_id:
                sheet.roster_unlinked_employee_count = 0
                sheet.roster_warning = False
                continue
            unlinked = Employee.search([
                ('company_id', '=', sheet.company_id.id),
                ('active', '=', True),
                ('restaurant_branch_id', '=', False),
            ])
            sheet.roster_unlinked_employee_count = len(unlinked)
            if unlinked:
                names = ', '.join(unlinked[:8].mapped('name'))
                remainder = len(unlinked) - 8
                suffix = self.env._(' and %s more', remainder) if remainder else ''
                sheet.roster_warning = self.env._(
                    '%(count)s active employee(s) in this company have no Restaurant Branch '
                    'and will not appear in a daily roster: %(names)s%(suffix)s. '
                    'HR must assign their branch on the employee card.',
                    count=len(unlinked), names=names, suffix=suffix,
                )
            else:
                sheet.roster_warning = False

    @api.model_create_multi
    def create(self, vals_list):
        require_role(self.env, RECEPTION_GROUP)
        prepared = []
        for vals in vals_list:
            values = dict(vals)
            if values.get('state', 'draft') != 'draft':
                raise AccessError(self.env._('Daily attendance sheets must start in Reception Entry.'))
            branch = self.env['restaurant.branch'].browse(values.get('branch_id'))
            require_assigned_branches(branch)
            values['state'] = 'draft'
            prepared.append(values)
        sheets = super().create(prepared)
        for sheet in sheets:
            roster = sheet._published_shift_roster()
            if roster:
                sheet._sync_from_shift_roster(roster)
        return sheets

    def write(self, vals):
        lock_records(self)
        if 'state' in vals:
            raise AccessError(self.env._('Use attendance sheet workflow actions to change state.'))
        line_fields = {'line_ids', 'exception_line_ids'}
        if vals and set(vals) <= line_fields:
            for sheet in self:
                if sheet.state == 'draft':
                    require_role(self.env, RECEPTION_GROUP)
                    require_assigned_branches(sheet.branch_id)
                elif sheet.state == 'manager_review':
                    require_role(self.env, MANAGER_GROUP)
                    require_assigned_branches(sheet.branch_id)
                elif sheet.state == 'submitted_to_hr':
                    require_role(self.env, HR_GROUP)
                else:
                    raise AccessError(self.env._(
                        'Approved attendance sheets cannot be edited.'
                    ))
            return super().write(vals)
        if set(vals) <= {'branch_id', 'attendance_date'}:
            require_role(self.env, RECEPTION_GROUP)
            if any(sheet.state != 'draft' or sheet.line_ids for sheet in self):
                raise AccessError(self.env._(
                    'Branch and date can only change on an empty Reception Entry sheet.'
                ))
            if 'branch_id' in vals:
                require_assigned_branches(
                    self.env['restaurant.branch'].browse(vals['branch_id'])
                )
            return super().write(vals)
        raise AccessError(self.env._('Attendance sheet audit fields cannot be edited manually.'))

    def unlink(self):
        lock_records(self, 'unlink')
        require_role(self.env, RECEPTION_GROUP)
        if any(sheet.state != 'draft' or sheet.line_ids for sheet in self):
            raise AccessError(self.env._('Only empty Reception Entry sheets can be deleted.'))
        return super().unlink()

    def _internal_write(self, values):
        return super(RestaurantAttendanceSheet, self).write(values)

    def _published_shift_roster(self):
        self.ensure_one()
        return self.env['restaurant.shift.roster'].sudo().search([
            ('branch_id', '=', self.branch_id.id),
            ('month_start', '<=', self.attendance_date),
            ('month_end', '>=', self.attendance_date),
            ('state', '=', 'published'),
        ], limit=1)

    def _sync_from_shift_roster(self, roster):
        self.ensure_one()
        roster.ensure_one()
        if (
            self.state != 'draft'
            or roster.state != 'published'
            or roster.branch_id != self.branch_id
            or not (roster.month_start <= self.attendance_date <= roster.month_end)
        ):
            raise ValidationError(self.env._(
                'The published shift roster does not match this draft attendance sheet.'
            ))

        Entry = self.env['restaurant.attendance.entry'].sudo().with_context(
            restaurant_roster_sync=True,
        )
        added = 0
        attached = 0
        for roster_line in roster.line_ids:
            employee = roster_line.employee_id
            existing = Entry.search([
                ('employee_id', '=', employee.id),
                ('branch_id', '=', self.branch_id.id),
                ('attendance_date', '=', self.attendance_date),
            ], limit=1)
            if existing:
                if existing.sheet_id and existing.sheet_id != self:
                    raise ValidationError(self.env._(
                        '%(employee)s already belongs to another sheet for this branch and date.',
                        employee=employee.display_name,
                    ))
                values = {
                    'staff_category_id': employee.restaurant_staff_category_id.id,
                    'staff_shift': roster_line.staff_shift,
                    'shift_roster_line_id': roster_line.id,
                }
                if not existing.sheet_id:
                    values['sheet_id'] = self.id
                    attached += 1
                if existing.state == 'draft':
                    existing.with_context(restaurant_roster_sync=True).write(values)
                continue
            Entry.create({
                'sheet_id': self.id,
                'employee_id': employee.id,
                'branch_id': self.branch_id.id,
                'attendance_date': self.attendance_date,
                'status': 'pending',
                'staff_shift': roster_line.staff_shift,
                'shift_roster_line_id': roster_line.id,
            })
            added += 1
        return added, attached

    def action_populate_roster(self):
        self.ensure_one()
        lock_records(self)
        require_role(self.env, RECEPTION_GROUP)
        require_assigned_branches(self.branch_id)
        if self.state != 'draft':
            raise AccessError(self.env._('The roster can only be refreshed before submission.'))

        roster = self._published_shift_roster()
        if not roster:
            raise ValidationError(self.env._(
                'The branch manager must publish the monthly Shift Roster before '
                'Reception can prepare this daily attendance sheet.'
            ))
        added, attached = self._sync_from_shift_roster(roster)

        self._internal_write({
            'last_roster_refresh_by': self.env.uid,
            'last_roster_refresh_at': fields.Datetime.now(),
        })
        return {
            'type': 'ir.actions.client',
            'tag': 'display_notification',
            'params': {
                'title': self.env._('Attendance Refreshed'),
                'message': self.env._(
                    '%(added)s new row(s) added and %(attached)s existing row(s) attached '
                    'from the published Shift Roster.',
                    added=added, attached=attached,
                ),
                'type': 'success',
                'sticky': False,
                'next': {'type': 'ir.actions.client', 'tag': 'reload'},
            },
        }

    def action_submit(self):
        self.ensure_one()
        lock_records(self)
        require_role(self.env, RECEPTION_GROUP)
        require_assigned_branches(self.branch_id)
        if self.state != 'draft':
            raise UserError(self.env._('Only a Reception Entry sheet can be submitted.'))
        if not self.line_ids:
            raise ValidationError(self.env._('Populate the staff roster before submission.'))
        pending = self.line_ids.filtered(lambda line: line.status == 'pending')
        if pending:
            raise ValidationError(self.env._(
                'Resolve all Pending rows before submitting the daily sheet. '
                '%s row(s) are still pending.', len(pending),
            ))
        blocking = self.line_ids.filtered('has_blocking_exception')
        if blocking:
            raise ValidationError(self.env._(
                'Correct blocking attendance issues before submission: %s',
                ', '.join(blocking[:8].mapped('employee_id.name')),
            ))
        for line in self.line_ids.filtered(lambda row: row.state == 'draft'):
            line.action_submit()
        self._internal_write({
            'state': 'manager_review',
            'submitted_by': self.env.uid,
            'submitted_at': fields.Datetime.now(),
        })
        self.message_post(body=self.env._('Daily attendance sheet submitted to the branch manager.'))
        return True

    def action_manager_accept_clean(self):
        self.ensure_one()
        lock_records(self)
        require_role(self.env, MANAGER_GROUP)
        require_assigned_branches(self.branch_id)
        if self.state != 'manager_review':
            raise UserError(self.env._('Clean rows can only be accepted during manager review.'))
        clean = self.line_ids.filtered(
            lambda row: row.state == 'manager_review' and not row.is_exception
        )
        clean.action_manager_accept_selected()
        return {
            'type': 'ir.actions.client',
            'tag': 'display_notification',
            'params': {
                'title': self.env._('Clean Rows Accepted'),
                'message': self.env._('%s clean row(s) are ready for HR.', len(clean)),
                'type': 'success',
                'sticky': False,
                'next': {'type': 'ir.actions.client', 'tag': 'reload'},
            },
        }

    def action_submit_to_hr(self):
        self.ensure_one()
        lock_records(self)
        require_role(self.env, MANAGER_GROUP)
        require_assigned_branches(self.branch_id)
        if self.state != 'manager_review':
            raise UserError(self.env._('Only a sheet in manager review can be sent to HR.'))
        unresolved = self.line_ids.filtered(
            lambda row: row.state not in {'submitted_to_hr', *RESOLVED_STATES}
        )
        if unresolved:
            raise ValidationError(self.env._(
                'Every row must be accepted or re-reviewed before sending the sheet to HR. '
                '%s row(s) are not ready.', len(unresolved),
            ))
        self._internal_write({
            'state': 'submitted_to_hr',
            'manager_submitted_by': self.env.uid,
            'manager_submitted_at': fields.Datetime.now(),
        })
        self.message_post(body=self.env._('Daily attendance sheet submitted to HR.'))
        return True

    def action_hr_approve_clean(self):
        self.ensure_one()
        lock_records(self)
        require_role(self.env, HR_GROUP)
        if self.state != 'submitted_to_hr':
            raise UserError(self.env._('Clean rows can only be approved during HR review.'))
        clean = self.line_ids.filtered(
            lambda row: row.state == 'submitted_to_hr' and not row.is_exception
        )
        clean.action_hr_approve_selected()
        return {
            'type': 'ir.actions.client',
            'tag': 'display_notification',
            'params': {
                'title': self.env._('Clean Rows Approved'),
                'message': self.env._('%s clean row(s) were approved and synchronized.', len(clean)),
                'type': 'success',
                'sticky': False,
                'next': {'type': 'ir.actions.client', 'tag': 'reload'},
            },
        }

    def _return_to_manager_review(self):
        for sheet in self.filtered(lambda item: item.state == 'submitted_to_hr'):
            sheet._internal_write({'state': 'manager_review'})
            sheet.message_post(body=self.env._(
                'A row was returned for correction; manager re-review is required.'
            ))

    def _complete_if_resolved(self):
        for sheet in self.filtered(lambda item: item.state == 'submitted_to_hr'):
            if sheet.line_ids and all(
                line.state in RESOLVED_STATES for line in sheet.line_ids
            ):
                sheet._internal_write({
                    'state': 'approved',
                    'approved_by': self.env.uid,
                    'approved_at': fields.Datetime.now(),
                })
                sheet.message_post(body=self.env._(
                    'Every attendance row is resolved; the daily sheet is approved.'
                ))

    def _reopen_for_month_correction(self):
        for sheet in self.filtered(lambda item: item.state == 'approved'):
            sheet._internal_write({'state': 'manager_review'})
            sheet.message_post(body=self.env._(
                'The daily sheet was reopened by an explicit monthly correction.'
            ))

    def action_open_exceptions(self):
        self.ensure_one()
        return {
            'type': 'ir.actions.act_window',
            'name': self.env._('Attendance Exceptions'),
            'res_model': 'restaurant.attendance.entry',
            'view_mode': 'list,form',
            'domain': [('sheet_id', '=', self.id), ('is_exception', '=', True)],
            'context': {'create': False, 'group_by': ['staff_category_id']},
        }

    def action_open_all_staff(self):
        self.ensure_one()
        return {
            'type': 'ir.actions.act_window',
            'name': self.env._('All Staff'),
            'res_model': 'restaurant.attendance.entry',
            'view_mode': 'list,form',
            'domain': [('sheet_id', '=', self.id)],
            'context': {'create': False, 'group_by': ['staff_category_id']},
        }


class RestaurantAttendanceMonth(models.Model):
    _name = 'restaurant.attendance.month'
    _description = 'Restaurant Monthly Attendance Summary'
    _inherit = ['mail.thread', 'mail.activity.mixin']
    _order = 'month_start desc, branch_id, id desc'
    _mail_post_access = 'read'

    name = fields.Char(compute='_compute_period', store=True)
    branch_id = fields.Many2one(
        'restaurant.branch', required=True, ondelete='restrict', index=True,
        tracking=True,
    )
    company_id = fields.Many2one(
        related='branch_id.company_id', store=True, index=True,
    )
    month_start = fields.Date(
        required=True, default=lambda self: fields.Date.context_today(self).replace(day=1),
        index=True, tracking=True,
    )
    month_end = fields.Date(compute='_compute_period', store=True)
    line_ids = fields.One2many(
        'restaurant.attendance.month.line', 'month_id', copy=False,
    )
    state = fields.Selection([
        ('draft', 'Open'),
        ('closed', 'Closed'),
        ('correction', 'Correction in Progress'),
    ], required=True, default='draft', readonly=True, copy=False, tracking=True,
        index=True)
    closed_by = fields.Many2one('res.users', readonly=True, copy=False)
    closed_at = fields.Datetime(readonly=True, copy=False)
    correction_reason = fields.Text(
        copy=False, help='Required before opening a closed month for correction.',
    )
    last_correction_reason = fields.Text(readonly=True, copy=False, tracking=True)
    correction_opened_by = fields.Many2one('res.users', readonly=True, copy=False)
    correction_opened_at = fields.Datetime(readonly=True, copy=False)
    correction_count = fields.Integer(readonly=True, copy=False)
    last_refreshed_at = fields.Datetime(readonly=True, copy=False)
    employee_count = fields.Integer(compute='_compute_totals')
    pending_missing_total = fields.Integer(compute='_compute_totals')
    overtime_total = fields.Float(compute='_compute_totals')

    _branch_month_unique = models.Constraint(
        'UNIQUE(branch_id, month_start)',
        'Only one monthly attendance summary is allowed per branch and month.',
    )

    @api.depends('branch_id', 'month_start')
    def _compute_period(self):
        for summary in self:
            if summary.month_start:
                last_day = calendar.monthrange(
                    summary.month_start.year, summary.month_start.month,
                )[1]
                summary.month_end = summary.month_start.replace(day=last_day)
            else:
                summary.month_end = False
            summary.name = (
                f'{summary.branch_id.display_name} - '
                f'{summary.month_start.strftime("%B %Y")}'
                if summary.branch_id and summary.month_start
                else self.env._('New Monthly Attendance Summary')
            )

    @api.depends('line_ids.pending_missing_rows', 'line_ids.overtime_hours')
    def _compute_totals(self):
        for summary in self:
            summary.employee_count = len(summary.line_ids)
            summary.pending_missing_total = sum(summary.line_ids.mapped('pending_missing_rows'))
            summary.overtime_total = sum(summary.line_ids.mapped('overtime_hours'))

    @api.constrains('month_start')
    def _check_month_start(self):
        if any(summary.month_start and summary.month_start.day != 1 for summary in self):
            raise ValidationError(self.env._('The monthly period must start on the first day.'))

    @api.model_create_multi
    def create(self, vals_list):
        require_role(self.env, HR_GROUP)
        prepared = []
        for vals in vals_list:
            values = dict(vals)
            if values.get('state', 'draft') != 'draft':
                raise AccessError(self.env._('Monthly summaries must start Open.'))
            values['state'] = 'draft'
            prepared.append(values)
        return super().create(prepared)

    def write(self, vals):
        lock_records(self)
        require_role(self.env, HR_GROUP)
        if 'state' in vals:
            raise AccessError(self.env._('Use monthly attendance actions to change state.'))
        fields_to_write = set(vals)
        if fields_to_write == {'correction_reason'} and all(
            summary.state == 'closed' for summary in self
        ):
            return super().write(vals)
        if fields_to_write <= {'branch_id', 'month_start'} and all(
            summary.state == 'draft' and not summary.line_ids for summary in self
        ):
            return super().write(vals)
        raise AccessError(self.env._('Closed and audit fields cannot be edited manually.'))

    def unlink(self):
        lock_records(self, 'unlink')
        require_role(self.env, HR_GROUP)
        if any(summary.state != 'draft' or summary.line_ids for summary in self):
            raise AccessError(self.env._('Only empty Open monthly summaries can be deleted.'))
        return super().unlink()

    def _internal_write(self, values):
        return super(RestaurantAttendanceMonth, self).write(values)

    def _source_entries(self):
        self.ensure_one()
        return self.env['restaurant.attendance.entry'].sudo().search([
            ('branch_id', '=', self.branch_id.id),
            ('attendance_date', '>=', self.month_start),
            ('attendance_date', '<=', self.month_end),
        ], order='employee_id, attendance_date, id')

    def _refresh_summary_lines(self):
        self.ensure_one()
        entries = self._source_entries()
        Line = self.env['restaurant.attendance.month.line'].sudo().with_context(
            attendance_month_internal=True,
        )
        existing_by_employee = {line.employee_id.id: line for line in self.line_ids.sudo()}
        seen = set()
        for employee in entries.employee_id:
            employee_entries = entries.filtered(lambda entry: entry.employee_id == employee)
            approved = employee_entries.filtered(lambda entry: entry.state == 'approved')
            values = {
                'month_id': self.id,
                'employee_id': employee.id,
                'present_days': len(approved.filtered(lambda entry: entry.status == 'present')),
                'late_count': len(approved.filtered(lambda entry: entry.status == 'late')),
                'late_minutes': sum(approved.filtered(
                    lambda entry: entry.status == 'late'
                ).mapped('late_minutes')),
                'absence_days': len(approved.filtered(lambda entry: entry.status == 'absent')),
                'annual_leave_days': len(approved.filtered(
                    lambda entry: entry.status == 'annual_leave'
                )),
                'sick_leave_days': len(approved.filtered(
                    lambda entry: entry.status == 'sick_leave'
                )),
                'emergency_leave_days': len(approved.filtered(
                    lambda entry: entry.status == 'emergency_leave'
                )),
                'day_off_days': len(approved.filtered(lambda entry: entry.status == 'day_off')),
                'overtime_hours': sum(approved.mapped('overtime_hours')),
                'pending_missing_rows': len(employee_entries.filtered(
                    lambda entry: (
                        entry.status == 'pending'
                        or entry.state not in RESOLVED_STATES
                        or entry.sync_status == 'error'
                    )
                )),
            }
            line = existing_by_employee.get(employee.id)
            if line:
                line.with_context(attendance_month_internal=True).write(values)
            else:
                Line.create(values)
            seen.add(employee.id)
        stale = self.line_ids.sudo().filtered(lambda line: line.employee_id.id not in seen)
        stale.with_context(attendance_month_internal=True).unlink()
        self._internal_write({'last_refreshed_at': fields.Datetime.now()})

    def action_refresh(self):
        self.ensure_one()
        lock_records(self)
        require_role(self.env, HR_GROUP)
        if self.state == 'closed':
            raise AccessError(self.env._('Open an explicit correction before refreshing a closed month.'))
        self._refresh_summary_lines()
        return True

    def action_close(self):
        self.ensure_one()
        lock_records(self)
        require_role(self.env, HR_GROUP)
        if self.state not in ('draft', 'correction'):
            raise UserError(self.env._('Only an Open or correction month can be closed.'))
        entries = self._source_entries()
        if not entries:
            raise ValidationError(self.env._('There are no attendance rows for this branch and month.'))
        unresolved = entries.filtered(lambda entry: (
            entry.status == 'pending'
            or entry.state not in RESOLVED_STATES
            or entry.sync_status == 'error'
        ))
        if unresolved:
            raise ValidationError(self.env._(
                'Resolve every pending, missing, or failed row before closing the month. '
                '%s row(s) remain unresolved.', len(unresolved),
            ))
        open_sheets = self.env['restaurant.attendance.sheet'].sudo().search_count([
            ('branch_id', '=', self.branch_id.id),
            ('attendance_date', '>=', self.month_start),
            ('attendance_date', '<=', self.month_end),
            ('state', '!=', 'approved'),
        ])
        if open_sheets:
            raise ValidationError(self.env._(
                'Approve every daily sheet before closing the month. '
                '%s daily sheet(s) remain open.', open_sheets,
            ))
        self._refresh_summary_lines()
        self._internal_write({
            'state': 'closed',
            'closed_by': self.env.uid,
            'closed_at': fields.Datetime.now(),
            'correction_reason': False,
        })
        self.message_post(body=self.env._('Monthly attendance summary closed by %s.', self.env.user.name))
        return True

    def action_open_correction(self):
        self.ensure_one()
        lock_records(self)
        require_role(self.env, HR_GROUP)
        if self.state != 'closed':
            raise UserError(self.env._('Only a closed month can be opened for correction.'))
        reason = (self.correction_reason or '').strip()
        if not reason:
            raise ValidationError(self.env._('Enter a correction reason before opening the month.'))
        self._internal_write({
            'state': 'correction',
            'last_correction_reason': reason,
            'correction_reason': False,
            'correction_opened_by': self.env.uid,
            'correction_opened_at': fields.Datetime.now(),
            'correction_count': self.correction_count + 1,
        })
        self.message_post(body=self.env._(
            'Monthly correction opened by %(user)s: %(reason)s',
            user=self.env.user.name, reason=reason,
        ))
        return True

    def action_open_entries(self):
        self.ensure_one()
        domain = [
            ('branch_id', '=', self.branch_id.id),
            ('attendance_date', '>=', self.month_start),
            ('attendance_date', '<=', self.month_end),
        ]
        filter_name = self.env.context.get('attendance_month_filter')
        if filter_name == 'pending':
            domain += [
                '|', '|',
                ('status', '=', 'pending'),
                ('state', 'not in', list(RESOLVED_STATES)),
                ('sync_status', '=', 'error'),
            ]
        elif filter_name == 'overtime':
            domain += [('overtime_hours', '>', 0)]
        return {
            'type': 'ir.actions.act_window',
            'name': self.env._('Monthly Attendance Rows'),
            'res_model': 'restaurant.attendance.entry',
            'view_mode': 'list,form',
            'domain': domain,
            'context': {'create': False},
        }


class RestaurantAttendanceMonthLine(models.Model):
    _name = 'restaurant.attendance.month.line'
    _description = 'Restaurant Monthly Attendance Employee Summary'
    _order = 'employee_id, id'

    month_id = fields.Many2one(
        'restaurant.attendance.month', required=True, ondelete='cascade', index=True,
    )
    company_id = fields.Many2one(related='month_id.company_id', store=True, index=True)
    branch_id = fields.Many2one(related='month_id.branch_id', store=True, index=True)
    employee_id = fields.Many2one(
        'hr.employee', required=True, readonly=True, ondelete='restrict', index=True,
    )
    job_id = fields.Many2one(related='employee_id.job_id', readonly=True)
    present_days = fields.Integer(readonly=True)
    late_count = fields.Integer(readonly=True)
    late_minutes = fields.Integer(readonly=True)
    absence_days = fields.Integer(readonly=True)
    annual_leave_days = fields.Integer(readonly=True)
    sick_leave_days = fields.Integer(readonly=True)
    emergency_leave_days = fields.Integer(readonly=True)
    day_off_days = fields.Integer(readonly=True)
    overtime_hours = fields.Float(readonly=True)
    pending_missing_rows = fields.Integer(readonly=True)

    _month_employee_unique = models.Constraint(
        'UNIQUE(month_id, employee_id)',
        'An employee can appear only once in a monthly attendance summary.',
    )

    @api.model_create_multi
    def create(self, vals_list):
        if not (self.env.su and self.env.context.get('attendance_month_internal')):
            raise AccessError(self.env._('Monthly summary rows are generated automatically.'))
        return super().create(vals_list)

    def write(self, vals):
        if not (self.env.su and self.env.context.get('attendance_month_internal')):
            raise AccessError(self.env._('Monthly summary rows are generated automatically.'))
        return super().write(vals)

    def unlink(self):
        if not (self.env.su and self.env.context.get('attendance_month_internal')):
            raise AccessError(self.env._('Monthly summary rows are generated automatically.'))
        return super().unlink()

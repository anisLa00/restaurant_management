import logging

from odoo import api, fields, models
from odoo.exceptions import AccessError, UserError, ValidationError

from .reception_security import (
    HR_GROUP, MANAGER_GROUP, RECEPTION_GROUP, check_employee_company,
    lock_records, require_assigned_branches, require_draft, require_role,
)


_logger = logging.getLogger(__name__)


OPERATIONAL_STATUSES = [
    ('pending', 'Pending'),
    ('present', 'Present'),
    ('absent', 'Absent'),
    ('late', 'Late'),
    ('day_off', 'Day Off'),
    ('overtime_day', 'Overtime Day'),
]
LEGACY_LEAVE_STATUSES = [
    ('sick_leave', 'Sick Leave'),
    ('annual_leave', 'Annual Leave'),
    ('emergency_leave', 'Emergency Leave'),
]
LEGACY_LEAVE_STATUS_KEYS = {key for key, _label in LEGACY_LEAVE_STATUSES}


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
    sheet_id = fields.Many2one(
        'restaurant.attendance.sheet', string='Daily Sheet', readonly=True,
        copy=False, index=True, ondelete='restrict', tracking=True,
    )
    source_type = fields.Selection([
        ('manual', 'Manual'),
        ('biometric', 'Biometric'),
        ('api', 'API Import'),
    ], required=True, default='manual', readonly=True, copy=False, tracking=True,
        help='Manual is the active source. Other values reserve a stable import boundary '
             'for a later integration without changing review stages.')
    source_reference = fields.Char(readonly=True, copy=False, index=True)
    job_id = fields.Many2one(
        related='employee_id.job_id', string='Job / Role', readonly=True,
    )
    staff_category_id = fields.Many2one(
        'restaurant.staff.category',
        string='Staff Category',
        readonly=True,
        copy=False,
        index=True,
        ondelete='restrict',
        help='Category copied from the employee when this attendance row is created.',
    )
    staff_shift = fields.Selection(
        [
            ('morning', 'Morning'),
            ('evening', 'Evening'),
            ('one_shift', 'One Shift'),
        ],
        string='Shift',
        copy=False,
        index=True,
        tracking=True,
        help='Daily shift selected by the branch manager during manager review.',
    )
    branch_id = fields.Many2one(
        'restaurant.branch', required=True, ondelete='restrict', index=True, tracking=True,
    )
    attendance_date = fields.Date(
        required=True, default=fields.Date.context_today, index=True, tracking=True,
    )
    status = fields.Selection(
        OPERATIONAL_STATUSES + LEGACY_LEAVE_STATUSES,
        string='Internal Status',
        required=True, default='present', index=True, tracking=True,
    )
    operational_status = fields.Selection(
        OPERATIONAL_STATUSES, string='Status',
        compute='_compute_operational_status',
        inverse='_inverse_operational_status',
        search='_search_operational_status',
    )
    check_in = fields.Datetime(tracking=True)
    check_out = fields.Datetime(tracking=True)
    late_minutes = fields.Integer(default=0, required=True, tracking=True)
    overtime_hours = fields.Float(default=0, required=True, tracking=True)
    reception_note = fields.Text(tracking=True)
    manager_note = fields.Text(tracking=True)
    manager_return_reason = fields.Text(
        tracking=True, help='Required when a manager returns a daily-sheet row.',
    )
    last_manager_return_reason = fields.Text(readonly=True, copy=False, tracking=True)
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
    manager_returned_by = fields.Many2one(
        'res.users', readonly=True, copy=False, tracking=True,
    )
    manager_returned_at = fields.Datetime(readonly=True, copy=False, tracking=True)
    manager_return_count = fields.Integer(readonly=True, copy=False, tracking=True)
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

    exception_summary = fields.Char(compute='_compute_exception_info')
    is_exception = fields.Boolean(
        compute='_compute_exception_info', search='_search_is_exception',
    )
    has_blocking_exception = fields.Boolean(compute='_compute_exception_info')
    correction_count = fields.Integer(readonly=True, copy=False, tracking=True)
    last_correction_reason = fields.Text(readonly=True, copy=False, tracking=True)
    correction_opened_by = fields.Many2one(
        'res.users', readonly=True, copy=False, tracking=True,
    )
    correction_opened_at = fields.Datetime(readonly=True, copy=False, tracking=True)

    can_review_manager = fields.Boolean(compute='_compute_review_permissions')
    can_review_hr = fields.Boolean(compute='_compute_review_permissions')

    _employee_branch_date_unique = models.Constraint(
        'UNIQUE(employee_id, branch_id, attendance_date)',
        'An attendance entry already exists for this employee, branch and date.',
    )
    _overtime_nonnegative = models.Constraint(
        'CHECK (overtime_hours >= 0)', 'Overtime cannot be negative.',
    )
    _late_minutes_nonnegative = models.Constraint(
        'CHECK (late_minutes >= 0)', 'Late minutes cannot be negative.',
    )
    _check_time_order = models.Constraint(
        'CHECK (check_out IS NULL OR check_in IS NULL OR check_out >= check_in)',
        'Check-out cannot be before check-in.',
    )

    @api.depends('status')
    def _compute_operational_status(self):
        operational_keys = {key for key, _label in OPERATIONAL_STATUSES}
        for entry in self:
            entry.operational_status = (
                entry.status if entry.status in operational_keys else False
            )

    def _inverse_operational_status(self):
        for entry in self:
            if entry.operational_status:
                entry.status = entry.operational_status

    @api.model
    def _search_operational_status(self, operator, value):
        return [('status', operator, value)]

    @api.onchange('operational_status')
    def _onchange_operational_status(self):
        if self.operational_status:
            self.status = self.operational_status
            self._onchange_overtime_day_hours()

    @api.onchange('status')
    def _onchange_status_clear_nonworking_times(self):
        if self.status in ('absent', 'day_off'):
            self.check_in = False
            self.check_out = False
            self.overtime_hours = 0

    @api.onchange('status', 'check_in', 'check_out')
    def _onchange_overtime_day_hours(self):
        for entry in self:
            if entry.status != 'overtime_day':
                continue
            if (
                entry.check_in
                and entry.check_out
                and entry.check_out > entry.check_in
            ):
                entry.overtime_hours = (
                    entry.check_out - entry.check_in
                ).total_seconds() / 3600
            else:
                entry.overtime_hours = 0

    @api.depends('manager_reviewed_by')
    @api.depends_context('uid')
    def _compute_review_permissions(self):
        manager = self.env.user.has_group(MANAGER_GROUP)
        hr = self.env.user.has_group(HR_GROUP)
        for entry in self:
            entry.can_review_manager = manager
            entry.can_review_hr = hr and entry.manager_reviewed_by != self.env.user

    @api.depends(
        'status', 'check_in', 'check_out', 'late_minutes', 'overtime_hours',
        'manager_return_count', 'hr_return_count', 'employee_id',
        'attendance_date',
    )
    def _compute_exception_info(self):
        duplicate_ids = set()
        employees = self.employee_id.ids
        dates = [value for value in self.mapped('attendance_date') if value]
        if employees and dates:
            self.env.cr.execute(
                """
                SELECT DISTINCT first.id
                  FROM restaurant_attendance_entry first
                  JOIN restaurant_attendance_entry second
                    ON second.employee_id = first.employee_id
                   AND second.attendance_date = first.attendance_date
                   AND second.id != first.id
                 WHERE first.employee_id = ANY(%s)
                   AND first.attendance_date BETWEEN %s AND %s
                """,
                (employees, min(dates), max(dates)),
            )
            duplicate_ids = {row[0] for row in self.env.cr.fetchall()}

        leave_statuses = {'annual_leave', 'sick_leave', 'emergency_leave'}
        for entry in self:
            messages = []
            blocking = False
            if entry.status == 'pending':
                messages.append(self.env._('Pending'))
                blocking = True
            if entry.status in ('present', 'late', 'overtime_day') and (
                not entry.check_in or not entry.check_out
            ):
                messages.append(self.env._('Missing check-in/out'))
                blocking = True
            if entry.check_in and entry.check_out and entry.check_out <= entry.check_in:
                messages.append(self.env._('Illogical time range'))
                blocking = True
            if entry.status not in ('present', 'late', 'overtime_day', 'pending') and (
                entry.check_in or entry.check_out
            ):
                messages.append(self.env._('Conflicting time values'))
                blocking = True
            if entry.status == 'late':
                messages.append(self.env._('Late'))
            elif entry.late_minutes:
                messages.append(self.env._('Late minutes conflict with status'))
                blocking = True
            if entry.status == 'absent':
                messages.append(self.env._('Absent'))
            if entry.status in leave_statuses:
                messages.append(self.env._('Leave'))
            if entry.overtime_hours > 0:
                messages.append(self.env._('Overtime'))
            if entry.id in duplicate_ids:
                messages.append(self.env._('Duplicate employee/date record'))
                blocking = True
            if entry.manager_return_count or entry.hr_return_count:
                messages.append(self.env._('Returned row'))
            entry.exception_summary = ', '.join(messages) or False
            entry.is_exception = bool(messages)
            entry.has_blocking_exception = blocking

    @api.model
    def _search_is_exception(self, operator, value):
        if operator not in ('=', '!=') or not isinstance(value, bool):
            return NotImplemented
        self.env.cr.execute(
            """
            SELECT entry.id
              FROM restaurant_attendance_entry entry
             WHERE entry.status IN (
                       'pending', 'late', 'absent', 'annual_leave',
                       'sick_leave', 'emergency_leave'
                   )
                OR entry.overtime_hours > 0
                OR (
                    entry.status IN ('present', 'late', 'overtime_day')
                    AND (entry.check_in IS NULL OR entry.check_out IS NULL)
                )
                OR (entry.check_out IS NOT NULL AND entry.check_in IS NOT NULL
                    AND entry.check_out <= entry.check_in)
                OR (
                    entry.status NOT IN ('present', 'late', 'overtime_day', 'pending')
                    AND (entry.check_in IS NOT NULL OR entry.check_out IS NOT NULL)
                )
                OR (entry.status != 'late' AND entry.late_minutes > 0)
                OR entry.manager_return_count > 0
                OR entry.hr_return_count > 0
                OR EXISTS (
                    SELECT 1
                      FROM restaurant_attendance_entry duplicate
                     WHERE duplicate.employee_id = entry.employee_id
                       AND duplicate.attendance_date = entry.attendance_date
                       AND duplicate.id != entry.id
                )
            """
        )
        exception_ids = [row[0] for row in self.env.cr.fetchall()]
        wants_exception = (operator == '=' and value) or (operator == '!=' and not value)
        return [('id', 'in' if wants_exception else 'not in', exception_ids)]

    @api.constrains('employee_id', 'branch_id')
    def _check_employee_company(self):
        check_employee_company(self)

    @api.constrains('status', 'check_in', 'check_out', 'overtime_hours')
    def _check_nonworking_time_values(self):
        for entry in self:
            if entry.status in ('absent', 'day_off') and (
                entry.check_in or entry.check_out or entry.overtime_hours
            ):
                raise ValidationError(self.env._(
                    'Absent and day-off entries cannot contain check-in, '
                    'check-out, or overtime.'
                ))

    @api.constrains('sheet_id', 'branch_id', 'attendance_date')
    def _check_sheet_consistency(self):
        for entry in self:
            if entry.sheet_id and (
                entry.branch_id != entry.sheet_id.branch_id
                or entry.attendance_date != entry.sheet_id.attendance_date
            ):
                raise ValidationError(self.env._(
                    'The employee row must match its daily sheet branch and date.'
                ))

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
            'check_out', 'late_minutes', 'overtime_hours', 'reception_note',
            'sheet_id', 'state',
        }
        prepared = []
        for values in vals_list:
            values = dict(values)
            operational_status = values.pop('operational_status', False)
            if operational_status:
                values['status'] = operational_status
            if (
                values.get('status') in LEGACY_LEAVE_STATUS_KEYS
                and not (
                    self.env.su
                    and self.env.context.get('restaurant_legacy_leave_import')
                )
            ):
                raise ValidationError(self.env._(
                    'Leave requests are entered by the branch manager in '
                    'Leave Requests, not by Reception.'
                ))
            if values.get('state', 'draft') != 'draft' or set(values) - editable:
                raise AccessError(self.env._(
                    'Attendance must start in draft without review or sync fields.'
                ))
            employee = self.env['hr.employee'].sudo().browse(values.get('employee_id'))
            values.update({
                'state': 'draft',
                'sync_status': 'pending',
                'entered_by': self.env.uid,
                'staff_category_id': employee.restaurant_staff_category_id.id,
                'staff_shift': employee.restaurant_shift,
                'manager_reviewed_by': False,
                'hr_reviewed_by': False,
                'manager_reviewed_at': False,
                'hr_reviewed_at': False,
                'manager_note': False,
                'hr_note': False,
                'decision_reason': False,
                'official_leave_type_id': False,
                'manager_return_reason': False,
                'source_type': 'manual',
                'source_reference': False,
            })
            if values.get('sheet_id'):
                sheet = self.env['restaurant.attendance.sheet'].browse(values['sheet_id'])
                if sheet.state != 'draft':
                    raise AccessError(self.env._(
                        'Rows can only be added while the daily sheet is in Reception Entry.'
                    ))
                values.update({
                    'branch_id': sheet.branch_id.id,
                    'attendance_date': sheet.attendance_date,
                })
            self._ensure_month_open_for_values(values)
            prepared.append(values)
        records = super().create(prepared)
        require_assigned_branches(records.branch_id)
        return records

    def write(self, vals):
        lock_records(self)
        vals = dict(vals)
        operational_status = vals.pop('operational_status', False)
        if operational_status:
            vals['status'] = operational_status
        if 'state' in vals:
            raise AccessError(self.env._('Use the attendance workflow actions to change state.'))

        fields_to_write = set(vals)
        operational = {
            'employee_id', 'branch_id', 'attendance_date', 'status', 'check_in',
            'check_out', 'late_minutes', 'overtime_hours', 'reception_note',
        }
        hr_editable = {'hr_note', 'decision_reason', 'official_leave_type_id'}

        if fields_to_write and fields_to_write <= operational:
            require_role(self.env, RECEPTION_GROUP)
            require_draft(self)
            self._ensure_month_open()
            require_assigned_branches(self.branch_id)
            if vals.get('status') in LEGACY_LEAVE_STATUS_KEYS:
                raise ValidationError(self.env._(
                    'Leave requests are entered by the branch manager in '
                    'Leave Requests, not by Reception.'
                ))
            if self.sheet_id and fields_to_write & {
                'employee_id', 'branch_id', 'attendance_date',
            }:
                raise AccessError(self.env._(
                    'Employee, branch, and date are controlled by the daily sheet.'
                ))
            if 'branch_id' in vals:
                require_assigned_branches(
                    self.env['restaurant.branch'].browse(vals['branch_id'])
                )
            values = dict(vals)
            if 'status' in values:
                self._check_correction_target_category(values['status'])
            if 'status' in values and any(entry.status != values['status'] for entry in self):
                values['official_leave_type_id'] = False
            return super().write(values)

        if fields_to_write and fields_to_write <= {
            'manager_note', 'manager_return_reason', 'staff_shift',
        }:
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
        self._ensure_month_open()
        require_assigned_branches(self.branch_id)
        return super().unlink()

    @api.model
    def _ensure_month_open_for_values(self, values):
        branch_id = values.get('branch_id')
        attendance_date = fields.Date.to_date(values.get('attendance_date'))
        if not branch_id or not attendance_date:
            return
        closed = self.env['restaurant.attendance.month'].sudo().search_count([
            ('branch_id', '=', branch_id),
            ('month_start', '<=', attendance_date),
            ('month_end', '>=', attendance_date),
            ('state', '=', 'closed'),
        ], limit=1)
        if closed:
            raise AccessError(self.env._(
                'This attendance month is closed. HR must open an explicit correction first.'
            ))

    def _ensure_month_open(self):
        for entry in self:
            self._ensure_month_open_for_values({
                'branch_id': entry.branch_id.id,
                'attendance_date': entry.attendance_date,
            })

    def _check_correction_target_category(self, new_status):
        attendance_statuses = {'present', 'late', 'overtime_day'}
        leave_statuses = {'annual_leave', 'sick_leave', 'emergency_leave'}
        for entry in self:
            if (
                entry.official_attendance_id
                and entry.status in attendance_statuses
                and new_status not in attendance_statuses
            ):
                raise ValidationError(self.env._(
                    'A corrected row with an official Attendance can only switch '
                    'between Present and Late. Correct or reverse the official record '
                    'through HR before changing its category.'
                ))
            if (
                entry.official_leave_id
                and entry.status in leave_statuses
                and new_status not in leave_statuses
            ):
                raise ValidationError(self.env._(
                    'A corrected row with an official Time Off request can only switch '
                    'between leave types. Correct or reverse the official request through '
                    'HR before changing its category.'
                ))

    def _link_to_sheet(self, sheet):
        self.ensure_one()
        sheet.ensure_one()
        require_role(self.env, RECEPTION_GROUP)
        require_assigned_branches(sheet.branch_id)
        if self.state != 'draft' or sheet.state != 'draft':
            raise AccessError(self.env._('Only draft rows can be attached to a Reception Entry sheet.'))
        if self.branch_id != sheet.branch_id or self.attendance_date != sheet.attendance_date:
            raise ValidationError(self.env._('The existing row does not match the daily sheet.'))
        values = {'sheet_id': sheet.id}
        if not self.staff_category_id:
            values['staff_category_id'] = self.employee_id.restaurant_staff_category_id.id
        if not self.staff_shift:
            values['staff_shift'] = self.employee_id.restaurant_shift
        return super(RestaurantAttendanceEntry, self).write(values)

    def _refresh_staff_details_from_employee(self):
        """Refresh employee classification while Reception owns the draft row."""
        for entry in self:
            if entry.state != 'draft':
                continue
            category = entry.employee_id.restaurant_staff_category_id
            values = {}
            if entry.staff_category_id != category:
                values['staff_category_id'] = category.id
            if values:
                super(RestaurantAttendanceEntry, entry).write(values)

    def _refresh_staff_category_from_employee(self):
        """Backward-compatible alias for the roster refresh helper."""
        self._refresh_staff_details_from_employee()

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
        self._ensure_month_open()
        self._check_transition(target)
        source = self.state
        now = fields.Datetime.now()
        values = {'state': target, **(extra_values or {})}

        if target == 'submitted_to_hr':
            values.update({
                'manager_reviewed_by': self.env.uid,
                'manager_reviewed_at': now,
                'manager_return_reason': False,
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
        elif source == 'manager_review' and target == 'draft':
            values.update({
                'manager_returned_by': self.env.uid,
                'manager_returned_at': now,
                'manager_return_count': self.manager_return_count + 1,
                'last_manager_return_reason': self.manager_return_reason,
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
        if source == 'submitted_to_hr' and target == 'draft' and self.sheet_id:
            self.sheet_id._return_to_manager_review()
        if target in ('approved', 'rejected') and self.sheet_id:
            self.sheet_id._complete_if_resolved()
        return result

    def action_submit(self):
        self.ensure_one()
        lock_records(self)
        if (
            self.status == 'overtime_day'
            and self.check_in
            and self.check_out
            and self.check_out > self.check_in
        ):
            super(RestaurantAttendanceEntry, self).write({
                'overtime_hours': (
                    self.check_out - self.check_in
                ).total_seconds() / 3600,
            })
        self._validate_operational_submission()
        if self.status == 'pending':
            raise ValidationError(self.env._('Choose a resolved attendance status before submission.'))
        if self.sheet_id and self.has_blocking_exception:
            raise ValidationError(self.env._(
                'Correct the blocking attendance issue before submission: %s',
                self.exception_summary,
            ))
        return self._transition('manager_review')

    def _validate_operational_submission(self):
        self.ensure_one()
        if (
            self.status in LEGACY_LEAVE_STATUS_KEYS
            and not (
                self.env.su
                and self.env.context.get('restaurant_legacy_leave_import')
            )
        ):
            raise ValidationError(self.env._(
                'Legacy leave attendance entries cannot be submitted. '
                'Create a manager Leave Request instead.'
            ))
        if self.status in ('present', 'late', 'overtime_day') and (
            not self.check_in or not self.check_out
        ):
            raise ValidationError(self.env._(
                'Present, late, and overtime-day entries require both check-in and check-out '
                'before they are sent to the manager.'
            ))
        if self.status == 'overtime_day' and self.check_out <= self.check_in:
            raise ValidationError(self.env._(
                'Overtime-day check-out must be after check-in.'
            ))
        if self.status == 'overtime_day' and self.overtime_hours <= 0:
            raise ValidationError(self.env._(
                'Overtime-day hours must be calculated from valid check-in and check-out times.'
            ))
        if self.overtime_hours and (not self.check_in or not self.check_out):
            raise ValidationError(self.env._(
                'Overtime requires both check-in and check-out.'
            ))

    def action_submit_to_hr(self):
        self.ensure_one()
        lock_records(self)
        if self.status == 'pending' or (self.sheet_id and self.has_blocking_exception):
            raise ValidationError(self.env._(
                'This row has a blocking attendance issue: %s',
                self.exception_summary or self.env._('Pending'),
            ))
        if self.sheet_id and not self.staff_shift:
            raise ValidationError(self.env._(
                'Select Morning, Evening, or One Shift before sending this row to HR.'
            ))
        if self.sheet_id:
            self.employee_id.sudo().write({'restaurant_shift': self.staff_shift})
        return self._transition('submitted_to_hr')

    def action_return_to_draft(self):
        self.ensure_one()
        lock_records(self)
        if self.sheet_id and not (self.manager_return_reason or '').strip():
            raise ValidationError(self.env._(
                'Enter a manager return reason before returning this row.'
            ))
        return self._transition('draft')

    def action_manager_return_to_reception(self):
        self.ensure_one()
        return self.action_return_to_draft()

    def action_manager_accept_selected(self):
        if not self:
            return True
        lock_records(self)
        require_role(self.env, MANAGER_GROUP)
        require_assigned_branches(self.branch_id)
        if any(entry.state != 'manager_review' for entry in self):
            raise UserError(self.env._('Select only rows currently in manager review.'))
        if any(
            entry.sheet_id and entry.sheet_id.state != 'manager_review'
            for entry in self
        ):
            raise UserError(self.env._('The daily sheet is not in manager review.'))
        missing_shift = self.filtered(lambda entry: not entry.staff_shift)
        if missing_shift:
            raise ValidationError(self.env._(
                'Select a shift for every accepted row: %s',
                ', '.join(missing_shift[:8].mapped('employee_id.name')),
            ))
        blocking = self.filtered('has_blocking_exception')
        if blocking:
            raise ValidationError(self.env._(
                'Correct blocking issues before accepting: %s',
                ', '.join(blocking[:8].mapped('employee_id.name')),
            ))
        for entry in self:
            entry.employee_id.sudo().write({'restaurant_shift': entry.staff_shift})
            entry._transition('submitted_to_hr')
        return True

    def action_return_to_reception(self):
        self.ensure_one()
        lock_records(self)
        self._require_hr_reviewer()
        if self.state != 'submitted_to_hr':
            raise UserError(self.env._('Only an entry awaiting HR can be returned.'))
        if self.sheet_id and self.sheet_id.state != 'submitted_to_hr':
            raise UserError(self.env._('The daily sheet has not been submitted to HR.'))
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
        if self.sheet_id and self.sheet_id.state != 'submitted_to_hr':
            raise UserError(self.env._('The daily sheet has not been submitted to HR.'))
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

    def action_hr_approve_selected(self):
        if not self:
            return True
        lock_records(self)
        require_role(self.env, HR_GROUP)
        if any(entry.state != 'submitted_to_hr' for entry in self):
            raise UserError(self.env._('Select only rows awaiting HR.'))
        blocking = self.filtered('has_blocking_exception')
        if blocking:
            raise ValidationError(self.env._(
                'The selected rows contain blocking issues: %s',
                ', '.join(blocking[:8].mapped('employee_id.name')),
            ))
        for entry in self:
            entry._attempt_approval_sync(retry=entry.sync_status == 'error')
        return True

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
        if self.sheet_id and self.sheet_id.state != 'submitted_to_hr':
            raise UserError(self.env._(
                'The daily sheet must be submitted to HR before its rows can be approved.'
            ))
        if self.status == 'pending' or (self.sheet_id and self.has_blocking_exception):
            raise ValidationError(self.env._(
                'Resolve blocking attendance issues before approval: %s',
                self.exception_summary or self.env._('Pending'),
            ))
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

    def action_reopen_for_month_correction(self):
        self.ensure_one()
        lock_records(self)
        require_role(self.env, HR_GROUP)
        if self.state not in ('approved', 'rejected'):
            raise UserError(self.env._('Only a resolved row can be reopened for correction.'))
        month = self.env['restaurant.attendance.month'].sudo().search([
            ('branch_id', '=', self.branch_id.id),
            ('month_start', '<=', self.attendance_date),
            ('month_end', '>=', self.attendance_date),
            ('state', '=', 'correction'),
        ], limit=1)
        if not month:
            raise ValidationError(self.env._(
                'HR must open this branch month for correction before reopening a row.'
            ))
        reason = (month.last_correction_reason or '').strip()
        if not reason:
            raise ValidationError(self.env._('The monthly correction must have an audit reason.'))
        super(RestaurantAttendanceEntry, self).write({
            'state': 'draft',
            'sync_status': 'pending',
            'sync_error': False,
            'manager_reviewed_by': False,
            'manager_reviewed_at': False,
            'decision_reason': False,
            'manager_return_reason': False,
            'correction_count': self.correction_count + 1,
            'last_correction_reason': reason,
            'correction_opened_by': self.env.uid,
            'correction_opened_at': fields.Datetime.now(),
        })
        if self.sheet_id:
            self.sheet_id._reopen_for_month_correction()
        self.sudo().message_post(
            body=self.env._(
                'Row reopened for monthly correction by %(user)s: %(reason)s',
                user=self.env.user.name, reason=reason,
            ),
            author_id=self.env.user.partner_id.id,
        )
        return True

    def _sync_official_records(self):
        self.ensure_one()
        if self.status == 'pending':
            raise ValidationError(self.env._('Pending rows cannot create official HR records.'))
        needs_attendance = self.status in ('present', 'late', 'overtime_day')
        needs_leave = self.status in ('annual_leave', 'sick_leave', 'emergency_leave')
        needs_overtime = self.overtime_hours > 0

        if (
            (
                needs_attendance or needs_leave or needs_overtime
                or self.overtime_ledger_id
            )
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
        elif self.overtime_ledger_id:
            self._void_overtime()

        return 'synced' if (
            needs_attendance or needs_leave or needs_overtime or self.overtime_ledger_id
        ) else 'not_required'

    def _sync_attendance(self):
        self.ensure_one()
        if not self.check_in or not self.check_out:
            raise ValidationError(self.env._(
                'Present, late, and overtime-day entries require both check-in and check-out.'
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
            leave.with_context(
                restaurant_hr_sync=True,
                leave_fast_create=True,
            ).write(values)
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
            'active': True,
        }
        if ledger:
            ledger.with_context(restaurant_hr_sync=True).write(values)
        else:
            ledger = ledger_model.create(values)
        super(RestaurantAttendanceEntry, self).write({
            'overtime_ledger_id': ledger.id,
        })

    def _void_overtime(self):
        self.ensure_one()
        ledger = self.overtime_ledger_id.sudo().exists()
        if not ledger:
            return
        ledger.with_context(restaurant_hr_sync=True).write({
            'hours': 0,
            'active': False,
            'approved_by': self.env.uid,
            'approved_at': fields.Datetime.now(),
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

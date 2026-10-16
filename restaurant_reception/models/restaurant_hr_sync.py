from odoo import api, fields, models
from odoo.exceptions import AccessError, ValidationError


class ResCompany(models.Model):
    _inherit = 'res.company'

    restaurant_hr_official_sync_enabled = fields.Boolean(
        string='Enable Restaurant HR Official Sync',
        help=(
            'Allow approved restaurant attendance entries to create official '
            'Attendances, Time Off requests, and overtime ledger rows.'
        ),
    )
    restaurant_annual_leave_type_id = fields.Many2one(
        'hr.work.entry.type', string='Annual Leave Type', ondelete='restrict',
        domain="[('time_off_selectable', '=', True)]",
    )
    restaurant_sick_leave_type_id = fields.Many2one(
        'hr.work.entry.type', string='Sick Leave Type', ondelete='restrict',
        domain="[('time_off_selectable', '=', True)]",
    )
    restaurant_emergency_leave_type_id = fields.Many2one(
        'hr.work.entry.type', string='Emergency Leave Type', ondelete='restrict',
        domain="[('time_off_selectable', '=', True)]",
    )

    @api.constrains(
        'restaurant_annual_leave_type_id',
        'restaurant_sick_leave_type_id',
        'restaurant_emergency_leave_type_id',
        'country_id',
    )
    def _check_restaurant_leave_types(self):
        for company in self:
            leave_types = (
                company.restaurant_annual_leave_type_id
                | company.restaurant_sick_leave_type_id
                | company.restaurant_emergency_leave_type_id
            )
            for leave_type in leave_types:
                if not leave_type.time_off_selectable:
                    raise ValidationError(self.env._(
                        'Restaurant leave mappings must use types selectable in Time Off.'
                    ))
                if leave_type.country_id and leave_type.country_id != company.country_id:
                    raise ValidationError(self.env._(
                        'Restaurant leave mappings must match the company country.'
                    ))


class ResConfigSettings(models.TransientModel):
    _inherit = 'res.config.settings'

    restaurant_hr_official_sync_enabled = fields.Boolean(
        related='company_id.restaurant_hr_official_sync_enabled', readonly=False,
    )
    restaurant_annual_leave_type_id = fields.Many2one(
        related='company_id.restaurant_annual_leave_type_id', readonly=False,
    )
    restaurant_sick_leave_type_id = fields.Many2one(
        related='company_id.restaurant_sick_leave_type_id', readonly=False,
    )
    restaurant_emergency_leave_type_id = fields.Many2one(
        related='company_id.restaurant_emergency_leave_type_id', readonly=False,
    )


class HrWorkEntryType(models.Model):
    _inherit = 'hr.work.entry.type'

    restaurant_payroll_treatment = fields.Selection([
        ('paid', 'Paid Leave'),
        ('unpaid', 'Unpaid Leave'),
    ],
        string='Restaurant Payroll Treatment',
        required=True,
        default='paid',
        help=(
            'Controls whether approved restaurant leave remains paid or is included '
            'in the automatic salary/30 deduction. The approved leave request keeps '
            'a snapshot so later configuration changes do not rewrite payroll history.'
        ),
    )


class HrAttendance(models.Model):
    _inherit = 'hr.attendance'

    restaurant_attendance_entry_id = fields.Many2one(
        'restaurant.attendance.entry', string='Restaurant Source Entry',
        readonly=True, copy=False, index=True, ondelete='restrict',
    )

    _restaurant_source_unique = models.Constraint(
        'UNIQUE(restaurant_attendance_entry_id)',
        'A restaurant attendance entry can link to only one official attendance.',
    )

    @api.constrains('restaurant_attendance_entry_id', 'employee_id')
    def _check_restaurant_source_employee(self):
        for attendance in self:
            if (
                attendance.restaurant_attendance_entry_id
                and attendance.employee_id
                != attendance.restaurant_attendance_entry_id.employee_id
            ):
                raise ValidationError(self.env._(
                    'Official attendance and restaurant source must use the same employee.'
                ))

    @api.model_create_multi
    def create(self, vals_list):
        if (
            any(values.get('restaurant_attendance_entry_id') for values in vals_list)
            and not (self.env.su and self.env.context.get('restaurant_hr_sync'))
        ):
            raise AccessError(self.env._('Restaurant source links are managed by the HR sync.'))
        return super().create(vals_list)

    def write(self, vals):
        if (
            'restaurant_attendance_entry_id' in vals
            and not (self.env.su and self.env.context.get('restaurant_hr_sync'))
        ):
            raise AccessError(self.env._('Restaurant source links are managed by the HR sync.'))
        return super().write(vals)


class HrLeave(models.Model):
    _inherit = 'hr.leave'

    restaurant_attendance_entry_id = fields.Many2one(
        'restaurant.attendance.entry', string='Restaurant Source Entry',
        readonly=True, copy=False, index=True, ondelete='restrict',
    )
    restaurant_leave_request_id = fields.Many2one(
        'restaurant.leave.request', string='Restaurant Leave Request',
        readonly=True, copy=False, index=True, ondelete='restrict',
    )

    _restaurant_source_unique = models.Constraint(
        'UNIQUE(restaurant_attendance_entry_id)',
        'A restaurant attendance entry can link to only one Time Off request.',
    )
    _restaurant_leave_request_unique = models.Constraint(
        'UNIQUE(restaurant_leave_request_id)',
        'A restaurant leave request can link to only one Time Off request.',
    )

    @api.constrains(
        'restaurant_attendance_entry_id', 'restaurant_leave_request_id', 'employee_id',
    )
    def _check_restaurant_source_employee(self):
        for leave in self:
            if leave.restaurant_attendance_entry_id and leave.restaurant_leave_request_id:
                raise ValidationError(self.env._(
                    'A Time Off request can have only one restaurant source.'
                ))
            if (
                leave.restaurant_attendance_entry_id
                and leave.employee_id != leave.restaurant_attendance_entry_id.employee_id
            ):
                raise ValidationError(self.env._(
                    'Time Off and restaurant source must use the same employee.'
                ))
            if (
                leave.restaurant_leave_request_id
                and leave.employee_id != leave.restaurant_leave_request_id.employee_id
            ):
                raise ValidationError(self.env._(
                    'Time Off and restaurant leave request must use the same employee.'
                ))

    @api.model_create_multi
    def create(self, vals_list):
        if (
            any(
                values.get('restaurant_attendance_entry_id')
                or values.get('restaurant_leave_request_id')
                for values in vals_list
            )
            and not (self.env.su and self.env.context.get('restaurant_hr_sync'))
        ):
            raise AccessError(self.env._('Restaurant source links are managed by the HR sync.'))
        return super().create(vals_list)

    def write(self, vals):
        linked_requests = self.filtered('restaurant_leave_request_id')
        protected = {
            'employee_id', 'work_entry_type_id', 'request_date_from',
            'request_date_to', 'state',
        }
        if (
            linked_requests
            and set(vals) & protected
            and not (self.env.su and self.env.context.get('restaurant_hr_sync'))
        ):
            raise AccessError(self.env._(
                'Approved restaurant leave cannot be changed or cancelled silently. '
                'Use the audited restaurant attendance correction workflow.'
            ))
        if (
            (
                'restaurant_attendance_entry_id' in vals
                or 'restaurant_leave_request_id' in vals
            )
            and not (self.env.su and self.env.context.get('restaurant_hr_sync'))
        ):
            raise AccessError(self.env._('Restaurant source links are managed by the HR sync.'))
        return super().write(vals)

    def unlink(self):
        if self.filtered('restaurant_leave_request_id'):
            raise AccessError(self.env._(
                'Approved restaurant leave cannot be deleted. Use an audited correction.'
            ))
        return super().unlink()


class RestaurantOvertimeLedger(models.Model):
    _name = 'restaurant.overtime.ledger'
    _description = 'Approved Restaurant Overtime Ledger'
    _inherit = ['mail.thread']
    _rec_name = 'employee_id'
    _order = 'attendance_date desc, id desc'

    source_entry_id = fields.Many2one(
        'restaurant.attendance.entry', required=True, readonly=True, copy=False,
        index=True, ondelete='restrict', tracking=True,
    )
    employee_id = fields.Many2one(
        'hr.employee', required=True, readonly=True, ondelete='restrict',
        index=True, tracking=True,
    )
    company_id = fields.Many2one(
        'res.company', required=True, readonly=True, ondelete='restrict',
        index=True, tracking=True,
    )
    branch_id = fields.Many2one(
        'restaurant.branch', required=True, readonly=True, ondelete='restrict',
        index=True, tracking=True,
    )
    attendance_date = fields.Date(required=True, readonly=True, index=True, tracking=True)
    measurement_type = fields.Selection([
        ('legacy_hours', 'Historical Hours'),
        ('approved_day', 'Approved Day Off Worked'),
    ], required=True, default='legacy_hours', readonly=True, copy=False, index=True,
        tracking=True)
    hours = fields.Float(required=True, readonly=True, tracking=True)
    days = fields.Integer(default=0, required=True, readonly=True, tracking=True)
    currency_id = fields.Many2one(
        related='company_id.currency_id', readonly=True,
    )
    amount = fields.Monetary(
        default=0, required=True, readonly=True, currency_field='currency_id',
        tracking=True,
    )
    approved_by = fields.Many2one(
        'res.users', required=True, readonly=True, ondelete='restrict', tracking=True,
    )
    approved_at = fields.Datetime(required=True, readonly=True, tracking=True)
    active = fields.Boolean(
        default=True, required=True, readonly=True, tracking=True,
        help='Cleared only when an audited attendance correction removes overtime.',
    )

    _source_entry_unique = models.Constraint(
        'UNIQUE(source_entry_id)',
        'An attendance entry can have only one overtime ledger row.',
    )
    _measurement_active_consistency = models.Constraint(
        "CHECK((active AND ((measurement_type = 'legacy_hours' AND hours > 0 "
        "AND days = 0) OR (measurement_type = 'approved_day' AND hours = 0 "
        "AND days = 1 AND amount = 100))) OR (NOT active AND hours = 0 "
        "AND days = 0 AND amount = 0))",
        'Active overtime must be either preserved historical hours or one approved day at 100 AED.',
    )

    def init(self):
        self.env.cr.execute(
            """
            CREATE UNIQUE INDEX IF NOT EXISTS
                restaurant_overtime_employee_date_active_uniq
            ON restaurant_overtime_ledger (employee_id, attendance_date)
            WHERE active
            """
        )

    @api.constrains('employee_id', 'company_id', 'branch_id', 'source_entry_id')
    def _check_source_consistency(self):
        for ledger in self:
            source = ledger.source_entry_id
            if (
                ledger.employee_id != source.employee_id
                or ledger.company_id != source.company_id
                or ledger.branch_id != source.branch_id
            ):
                raise ValidationError(self.env._(
                    'The overtime ledger must match its attendance source.'
                ))

    @api.model_create_multi
    def create(self, vals_list):
        if not (self.env.su and self.env.context.get('restaurant_hr_sync')):
            raise AccessError(self.env._('Overtime ledger rows are created by HR approval only.'))
        return super().create(vals_list)

    def write(self, vals):
        if not (self.env.su and self.env.context.get('restaurant_hr_sync')):
            raise AccessError(self.env._('Overtime ledger rows are maintained by HR approval only.'))
        return super().write(vals)

    @api.ondelete(at_uninstall=False)
    def _unlink_except_module_uninstall(self):
        raise AccessError(self.env._('Approved overtime ledger rows cannot be deleted.'))

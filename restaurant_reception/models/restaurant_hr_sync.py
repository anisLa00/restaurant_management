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
        if (
            (
                'restaurant_attendance_entry_id' in vals
                or 'restaurant_leave_request_id' in vals
            )
            and not (self.env.su and self.env.context.get('restaurant_hr_sync'))
        ):
            raise AccessError(self.env._('Restaurant source links are managed by the HR sync.'))
        return super().write(vals)


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
    hours = fields.Float(required=True, readonly=True, tracking=True)
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
    _hours_active_consistency = models.Constraint(
        'CHECK((active AND hours > 0) OR (NOT active AND hours = 0))',
        'Active overtime must be positive; corrected inactive overtime must be zero.',
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

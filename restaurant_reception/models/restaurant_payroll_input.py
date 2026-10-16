from odoo import api, fields, models
from odoo.exceptions import AccessError, UserError, ValidationError

from .reception_security import HR_GROUP


ACCOUNTANT_GROUP = 'restaurant_core.group_restaurant_accountant'


class RestaurantPayrollInputBatch(models.Model):
    _name = 'restaurant.payroll.input.batch'
    _description = 'Restaurant Payroll Input Batch'
    _inherit = ['mail.thread', 'mail.activity.mixin']
    _order = 'month_start desc, branch_id, id desc'
    _mail_post_access = 'read'

    reference = fields.Char(required=True, default='New', readonly=True, copy=False, index=True)
    attendance_month_id = fields.Many2one(
        'restaurant.attendance.month', required=True, ondelete='restrict', index=True,
        domain="[('state', '=', 'closed')]", tracking=True,
    )
    company_id = fields.Many2one(related='attendance_month_id.company_id', store=True, index=True)
    branch_id = fields.Many2one(related='attendance_month_id.branch_id', store=True, index=True)
    month_start = fields.Date(related='attendance_month_id.month_start', store=True, index=True)
    month_end = fields.Date(related='attendance_month_id.month_end', store=True)
    currency_id = fields.Many2one(related='company_id.currency_id', readonly=True)
    line_ids = fields.One2many('restaurant.payroll.input.line', 'batch_id', copy=False)
    employee_count = fields.Integer(compute='_compute_totals')
    total_input_amount = fields.Monetary(compute='_compute_totals', currency_field='currency_id')
    state = fields.Selection([
        ('draft', 'Draft Inputs'),
        ('ready', 'Ready for Payroll'),
        ('processed', 'Processed by Payroll'),
    ], required=True, default='draft', readonly=True, copy=False, tracking=True, index=True)
    hr_note = fields.Text()
    payroll_note = fields.Text()
    prepared_by = fields.Many2one('res.users', readonly=True, copy=False)
    prepared_at = fields.Datetime(readonly=True, copy=False)
    processed_by = fields.Many2one('res.users', readonly=True, copy=False)
    processed_at = fields.Datetime(readonly=True, copy=False)

    _attendance_month_unique = models.Constraint(
        'UNIQUE(attendance_month_id)',
        'A payroll input batch already exists for this monthly attendance summary.',
    )

    def _is_hr(self):
        return self.env.su or self.env.user.has_group(HR_GROUP) or self.env.user.has_group('base.group_system')

    def _is_accountant(self):
        return self.env.su or self.env.user.has_group(ACCOUNTANT_GROUP) or self.env.user.has_group('base.group_system')

    def _require_hr(self):
        if not self._is_hr():
            raise AccessError(self.env._('Only HR can prepare payroll inputs.'))

    def _require_accountant(self):
        if not self._is_accountant():
            raise AccessError(self.env._('Only Payroll/Accounting can mark payroll inputs as processed.'))

    @api.depends('line_ids.input_total')
    def _compute_totals(self):
        for batch in self:
            batch.employee_count = len(batch.line_ids)
            batch.total_input_amount = sum(batch.line_ids.mapped('input_total'))

    @api.model_create_multi
    def create(self, vals_list):
        self._require_hr()
        sequence = self.env['ir.sequence'].sudo().search([
            ('code', '=', 'restaurant.payroll.input.batch'), ('company_id', '=', False),
        ], limit=1)
        if not sequence:
            raise UserError(self.env._('The payroll input sequence is not configured.'))
        prepared = []
        for vals in vals_list:
            values = dict(vals)
            if values.get('state', 'draft') != 'draft':
                raise AccessError(self.env._('Payroll inputs must start in Draft.'))
            if not values.get('reference') or values.get('reference') in ('New', '/'):
                values['reference'] = sequence.next_by_id()
            values['state'] = 'draft'
            prepared.append(values)
        records = super().create(prepared)
        records._generate_lines()
        return records

    def write(self, vals):
        if self.env.context.get('restaurant_payroll_internal'):
            return super().write(vals)
        if 'state' in vals:
            raise AccessError(self.env._('Use payroll input workflow buttons to change status.'))
        if self._is_hr():
            if any(batch.state != 'draft' for batch in self) and set(vals) - {'hr_note'}:
                raise AccessError(self.env._('Ready or processed payroll inputs are locked.'))
            return super().write(vals)
        if self._is_accountant() and set(vals) <= {'payroll_note'}:
            return super().write(vals)
        raise AccessError(self.env._('You cannot edit this payroll input batch.'))

    def unlink(self):
        self._require_hr()
        if any(batch.state != 'draft' for batch in self):
            raise AccessError(self.env._('Only draft payroll input batches can be deleted.'))
        return super().unlink()

    def _generate_lines(self):
        Line = self.env['restaurant.payroll.input.line'].sudo().with_context(
            restaurant_payroll_internal=True,
        )
        for batch in self:
            if batch.attendance_month_id.state != 'closed':
                raise ValidationError(self.env._(
                    'Close the monthly attendance summary before generating payroll inputs.'
                ))
            if batch.line_ids:
                continue
            if not batch.attendance_month_id.line_ids:
                raise ValidationError(self.env._('The closed attendance month has no employee lines.'))
            values_list = []
            for attendance_line in batch.attendance_month_id.line_ids:
                source_entries = self.env['restaurant.attendance.entry'].sudo().search([
                    ('employee_id', '=', attendance_line.employee_id.id),
                    ('branch_id', '=', batch.branch_id.id),
                    ('attendance_date', '>=', batch.month_start),
                    ('attendance_date', '<=', batch.month_end),
                    ('state', '=', 'approved'),
                ], order='attendance_date, id')
                unexcused_absence_sources = source_entries.filtered(lambda entry: (
                    entry.status == 'absent' and not entry.absence_excused
                ))
                unpaid_leave_sources = source_entries.filtered(lambda entry: (
                    entry.status in (
                        'annual_leave', 'sick_leave', 'emergency_leave'
                    ) and entry.leave_payroll_treatment == 'unpaid'
                ))
                overtime_sources = source_entries.filtered(lambda entry: (
                    entry.status == 'overtime_day'
                    and entry.overtime_measurement == 'approved_day'
                    and entry.scheduled_day_off
                ))
                eligible_overtime_sources = overtime_sources[:4]
                offset_count = min(
                    len(unexcused_absence_sources), len(eligible_overtime_sources)
                )
                offset_absence_sources = unexcused_absence_sources[:offset_count]
                offset_overtime_sources = eligible_overtime_sources[:offset_count]
                counted_absence_sources = unexcused_absence_sources[offset_count:]
                payable_overtime_sources = eligible_overtime_sources[offset_count:]
                deduction_sources = counted_absence_sources | unpaid_leave_sources
                base_salary = attendance_line.employee_id.sudo().wage or 0.0
                daily_rate = base_salary / 30 if base_salary else 0.0
                unpaid_days = len(deduction_sources)
                values_list.append({
                    'batch_id': batch.id,
                    'employee_id': attendance_line.employee_id.id,
                    'calculation_version': 'automatic_days',
                    'base_salary': base_salary,
                    'daily_rate': daily_rate,
                    'present_days': attendance_line.present_days,
                    'late_count': attendance_line.late_count,
                    'late_minutes': attendance_line.late_minutes,
                    'absence_days': attendance_line.absence_days,
                    'excused_absence_days': attendance_line.excused_absence_days,
                    'gross_unexcused_absence_days': len(unexcused_absence_sources),
                    'absence_overtime_offset_days': offset_count,
                    'unpaid_absence_days': unpaid_days,
                    'paid_leave_days': attendance_line.paid_leave_days,
                    'unpaid_leave_days': attendance_line.unpaid_leave_days,
                    'automatic_absence_deduction': batch.currency_id.round(
                        daily_rate * unpaid_days
                    ),
                    'annual_leave_days': attendance_line.annual_leave_days,
                    'sick_leave_days': attendance_line.sick_leave_days,
                    'emergency_leave_days': attendance_line.emergency_leave_days,
                    'day_off_days': attendance_line.day_off_days,
                    'overtime_hours': attendance_line.overtime_hours,
                    'overtime_days': len(overtime_sources),
                    'eligible_overtime_days': len(eligible_overtime_sources),
                    'payable_overtime_days': len(payable_overtime_sources),
                    'excess_overtime_days': max(len(overtime_sources) - 4, 0),
                    'automatic_overtime_amount': batch.currency_id.round(
                        len(payable_overtime_sources) * 100
                    ),
                    'source_attendance_ids': [(6, 0, source_entries.ids)],
                    'unpaid_absence_source_ids': [(6, 0, deduction_sources.ids)],
                    'offset_absence_source_ids': [(6, 0, offset_absence_sources.ids)],
                    'overtime_source_ids': [(6, 0, overtime_sources.ids)],
                    'offset_overtime_source_ids': [(6, 0, offset_overtime_sources.ids)],
                    'payable_overtime_source_ids': [(6, 0, payable_overtime_sources.ids)],
                })
            Line.create(values_list)

    def action_ready(self):
        self.ensure_one()
        self._require_hr()
        if self.state != 'draft':
            raise UserError(self.env._('Only draft inputs can be sent to Payroll.'))
        if not self.line_ids:
            raise ValidationError(self.env._('Generate payroll input lines first.'))
        excess = self.line_ids.filtered('excess_overtime_days')
        if excess:
            raise ValidationError(self.env._(
                'Payroll inputs cannot be sent while approved Overtime Days exceed '
                'the monthly maximum of four:\n%s',
                '\n'.join(
                    self.env._(
                        '%(employee)s: %(days)s approved Overtime Days.',
                        employee=line.employee_id.display_name,
                        days=line.overtime_days,
                    )
                    for line in excess
                ),
            ))
        self.with_context(restaurant_payroll_internal=True).write({
            'state': 'ready',
            'prepared_by': self.env.uid,
            'prepared_at': fields.Datetime.now(),
        })
        self.message_post(body=self.env._('Payroll inputs prepared by %s.', self.env.user.name))
        return True

    def action_process(self):
        self.ensure_one()
        self._require_accountant()
        if self.state != 'ready':
            raise UserError(self.env._('Only Ready payroll inputs can be processed.'))
        invalid = []
        for line in self.line_ids:
            if not line.base_salary or line.base_salary <= 0:
                invalid.append(self.env._(
                    '%(employee)s: base salary is missing or invalid.',
                    employee=line.employee_id.display_name,
                ))
            if line.input_total < 0:
                invalid.append(self.env._(
                    '%(employee)s: calculated net payroll input is negative (%(amount)s).',
                    employee=line.employee_id.display_name,
                    amount=line.input_total,
                ))
        if invalid:
            raise ValidationError(self.env._(
                'Payroll inputs cannot be marked Processed:\n%s',
                '\n'.join(invalid),
            ))
        self.with_context(restaurant_payroll_internal=True).write({
            'state': 'processed',
            'processed_by': self.env.uid,
            'processed_at': fields.Datetime.now(),
        })
        self.message_post(body=self.env._('Payroll inputs marked Processed by %s.', self.env.user.name))
        return True

    def action_open_attendance_month(self):
        self.ensure_one()
        return {
            'type': 'ir.actions.act_window',
            'name': self.env._('Monthly Attendance'),
            'res_model': 'restaurant.attendance.month',
            'res_id': self.attendance_month_id.id,
            'view_mode': 'form',
            'target': 'current',
        }


class RestaurantPayrollInputLine(models.Model):
    _name = 'restaurant.payroll.input.line'
    _description = 'Restaurant Payroll Input Employee Line'
    _order = 'employee_id, id'

    batch_id = fields.Many2one(
        'restaurant.payroll.input.batch', required=True, ondelete='cascade', index=True,
    )
    company_id = fields.Many2one(related='batch_id.company_id', store=True, index=True)
    branch_id = fields.Many2one(related='batch_id.branch_id', store=True, index=True)
    currency_id = fields.Many2one(related='batch_id.currency_id', readonly=True)
    employee_id = fields.Many2one('hr.employee', required=True, readonly=True, ondelete='restrict', index=True)
    employee_number = fields.Char(related='employee_id.restaurant_employee_number', readonly=True)
    job_id = fields.Many2one(related='employee_id.job_id', readonly=True)
    base_salary = fields.Monetary(currency_field='currency_id', readonly=True)
    calculation_version = fields.Selection([
        ('legacy', 'Historical Hours / Manual'),
        ('automatic_days', 'Automatic Days'),
    ], required=True, default='legacy', readonly=True, copy=False, index=True)
    daily_rate = fields.Monetary(currency_field='currency_id', readonly=True)
    present_days = fields.Integer(readonly=True)
    late_count = fields.Integer(readonly=True)
    late_minutes = fields.Integer(readonly=True)
    absence_days = fields.Integer(readonly=True)
    excused_absence_days = fields.Integer(readonly=True)
    gross_unexcused_absence_days = fields.Integer(readonly=True)
    absence_overtime_offset_days = fields.Integer(
        string='Absence Days Offset by Overtime', readonly=True,
    )
    unpaid_absence_days = fields.Float(readonly=True)
    paid_leave_days = fields.Integer(readonly=True)
    unpaid_leave_days = fields.Integer(readonly=True)
    automatic_absence_deduction = fields.Monetary(
        currency_field='currency_id', readonly=True,
    )
    annual_leave_days = fields.Integer(readonly=True)
    sick_leave_days = fields.Integer(readonly=True)
    emergency_leave_days = fields.Integer(readonly=True)
    day_off_days = fields.Integer(readonly=True)
    overtime_hours = fields.Float(readonly=True)
    overtime_days = fields.Integer(readonly=True)
    eligible_overtime_days = fields.Integer(
        string='Eligible Overtime Days (Max 4)', readonly=True,
    )
    payable_overtime_days = fields.Integer(readonly=True)
    excess_overtime_days = fields.Integer(readonly=True)
    automatic_overtime_amount = fields.Monetary(
        currency_field='currency_id', readonly=True,
    )
    overtime_amount = fields.Monetary(
        currency_field='currency_id', string='Historical Overtime Amount',
        help='Preserved for historical hour-based payroll inputs only.',
    )
    allowance_amount = fields.Monetary(currency_field='currency_id')
    deduction_amount = fields.Monetary(
        currency_field='currency_id', string='Manual Deduction',
    )
    source_attendance_ids = fields.Many2many(
        'restaurant.attendance.entry',
        'restaurant_payroll_line_attendance_rel',
        'payroll_line_id', 'attendance_entry_id',
        string='Source Attendance', readonly=True, copy=False,
    )
    unpaid_absence_source_ids = fields.Many2many(
        'restaurant.attendance.entry',
        'restaurant_payroll_line_unpaid_source_rel',
        'payroll_line_id', 'attendance_entry_id',
        string='Unpaid Absence Sources', readonly=True, copy=False,
    )
    offset_absence_source_ids = fields.Many2many(
        'restaurant.attendance.entry',
        'restaurant_payroll_line_offset_absence_rel',
        'payroll_line_id', 'attendance_entry_id',
        string='Offset Absence Date Sources', readonly=True, copy=False,
    )
    overtime_source_ids = fields.Many2many(
        'restaurant.attendance.entry',
        'restaurant_payroll_line_overtime_source_rel',
        'payroll_line_id', 'attendance_entry_id',
        string='Overtime Day Sources', readonly=True, copy=False,
    )
    offset_overtime_source_ids = fields.Many2many(
        'restaurant.attendance.entry',
        'restaurant_payroll_line_offset_overtime_rel',
        'payroll_line_id', 'attendance_entry_id',
        string='Overtime Days Used for Absence Offset', readonly=True, copy=False,
    )
    payable_overtime_source_ids = fields.Many2many(
        'restaurant.attendance.entry',
        'restaurant_payroll_line_payable_overtime_rel',
        'payroll_line_id', 'attendance_entry_id',
        string='Payable Overtime Day Sources', readonly=True, copy=False,
    )
    input_total = fields.Monetary(
        compute='_compute_input_total', store=True, currency_field='currency_id',
        string='Payroll Input Total',
    )
    adjustment_note = fields.Char()

    _batch_employee_unique = models.Constraint(
        'UNIQUE(batch_id, employee_id)',
        'An employee can appear only once in a payroll input batch.',
    )

    @api.depends(
        'base_salary', 'overtime_amount', 'automatic_overtime_amount',
        'allowance_amount', 'deduction_amount', 'automatic_absence_deduction',
    )
    def _compute_input_total(self):
        for line in self:
            line.input_total = (
                line.base_salary + line.overtime_amount
                + line.automatic_overtime_amount + line.allowance_amount
                - line.automatic_absence_deduction - line.deduction_amount
            )

    @api.model_create_multi
    def create(self, vals_list):
        if not (self.env.su and self.env.context.get('restaurant_payroll_internal')):
            raise AccessError(self.env._('Payroll input lines are generated automatically.'))
        return super().create(vals_list)

    def write(self, vals):
        if self.env.context.get('restaurant_payroll_internal'):
            return super().write(vals)
        if not (
            self.env.user.has_group(HR_GROUP)
            or self.env.user.has_group('base.group_system')
        ):
            raise AccessError(self.env._('Only HR can edit payroll input adjustments.'))
        if any(line.batch_id.state != 'draft' for line in self):
            raise AccessError(self.env._('Payroll input lines are locked after submission.'))
        editable = {
            'overtime_amount', 'allowance_amount', 'deduction_amount',
            'adjustment_note',
        }
        if set(vals) - editable:
            raise AccessError(self.env._('Source attendance and salary snapshots cannot be edited.'))
        if 'overtime_amount' in vals and any(
            line.calculation_version == 'automatic_days' for line in self
        ):
            raise AccessError(self.env._(
                'Automatic day-based payroll cannot use a manual overtime amount.'
            ))
        adjustment_fields = {'allowance_amount', 'deduction_amount', 'overtime_amount'}
        if set(vals) & adjustment_fields:
            final_note = vals.get('adjustment_note')
            if final_note is None:
                final_note = self[:1].adjustment_note
            if not (final_note or '').strip():
                raise ValidationError(self.env._(
                    'Enter an adjustment note for every manual payroll adjustment.'
                ))
        before = {
            line.id: (
                line.overtime_amount, line.allowance_amount, line.deduction_amount
            )
            for line in self
        }
        result = super().write(vals)
        if set(vals) & adjustment_fields:
            for line in self:
                old = before[line.id]
                new = (
                    line.overtime_amount, line.allowance_amount, line.deduction_amount
                )
                line.batch_id.message_post(body=self.env._(
                    'Manual payroll adjustment changed for %(employee)s by %(user)s. '
                    'Historical overtime / allowance / deduction: %(old)s → %(new)s. '
                    'Reason: %(reason)s',
                    employee=line.employee_id.display_name,
                    user=self.env.user.name,
                    old=old,
                    new=new,
                    reason=line.adjustment_note,
                ))
        return result

    def unlink(self):
        if not (self.env.su and self.env.context.get('restaurant_payroll_internal')):
            raise AccessError(self.env._('Payroll input lines cannot be deleted manually.'))
        return super().unlink()

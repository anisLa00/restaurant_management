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
            Line.create([{
                'batch_id': batch.id,
                'employee_id': attendance_line.employee_id.id,
                'base_salary': attendance_line.employee_id.sudo().wage or 0.0,
                'present_days': attendance_line.present_days,
                'late_count': attendance_line.late_count,
                'late_minutes': attendance_line.late_minutes,
                'absence_days': attendance_line.absence_days,
                'unpaid_absence_days': attendance_line.absence_days,
                'annual_leave_days': attendance_line.annual_leave_days,
                'sick_leave_days': attendance_line.sick_leave_days,
                'emergency_leave_days': attendance_line.emergency_leave_days,
                'day_off_days': attendance_line.day_off_days,
                'overtime_hours': attendance_line.overtime_hours,
            } for attendance_line in batch.attendance_month_id.line_ids])

    def action_ready(self):
        self.ensure_one()
        self._require_hr()
        if self.state != 'draft':
            raise UserError(self.env._('Only draft inputs can be sent to Payroll.'))
        if not self.line_ids:
            raise ValidationError(self.env._('Generate payroll input lines first.'))
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
    present_days = fields.Integer(readonly=True)
    late_count = fields.Integer(readonly=True)
    late_minutes = fields.Integer(readonly=True)
    absence_days = fields.Integer(readonly=True)
    unpaid_absence_days = fields.Float()
    annual_leave_days = fields.Integer(readonly=True)
    sick_leave_days = fields.Integer(readonly=True)
    emergency_leave_days = fields.Integer(readonly=True)
    day_off_days = fields.Integer(readonly=True)
    overtime_hours = fields.Float(readonly=True)
    overtime_amount = fields.Monetary(currency_field='currency_id')
    allowance_amount = fields.Monetary(currency_field='currency_id')
    deduction_amount = fields.Monetary(currency_field='currency_id')
    input_total = fields.Monetary(
        compute='_compute_input_total', store=True, currency_field='currency_id',
        string='Payroll Input Total',
    )
    adjustment_note = fields.Char()

    _batch_employee_unique = models.Constraint(
        'UNIQUE(batch_id, employee_id)',
        'An employee can appear only once in a payroll input batch.',
    )

    @api.depends('base_salary', 'overtime_amount', 'allowance_amount', 'deduction_amount')
    def _compute_input_total(self):
        for line in self:
            line.input_total = (
                line.base_salary + line.overtime_amount
                + line.allowance_amount - line.deduction_amount
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
            'unpaid_absence_days', 'overtime_amount', 'allowance_amount',
            'deduction_amount', 'adjustment_note',
        }
        if set(vals) - editable:
            raise AccessError(self.env._('Source attendance and salary snapshots cannot be edited.'))
        return super().write(vals)

    def unlink(self):
        if not (self.env.su and self.env.context.get('restaurant_payroll_internal')):
            raise AccessError(self.env._('Payroll input lines cannot be deleted manually.'))
        return super().unlink()

from odoo import api, fields, models
from odoo.exceptions import AccessError, UserError

from .reception_security import (
    MANAGER_GROUP, RECEPTION_GROUP, lock_records, require_assigned_branches,
    require_draft, require_role,
)


class RestaurantDailyClosing(models.Model):
    _name = 'restaurant.daily.closing'
    _description = 'Restaurant Daily Closing'
    _inherit = ['mail.thread', 'mail.activity.mixin']
    _order = 'closing_date desc, id desc'
    _mail_post_access = 'read'

    name = fields.Char(string='Reference', required=True, readonly=True, copy=False, default='New', index=True)
    branch_id = fields.Many2one('restaurant.branch', required=True, ondelete='restrict', index=True, tracking=True)
    closing_date = fields.Date(required=True, default=fields.Date.context_today, index=True, tracking=True)
    shift = fields.Selection([
        ('morning', 'Morning'), ('evening', 'Evening'), ('full_day', 'Full Day'),
    ], required=True, default='full_day', tracking=True)
    reception_user_id = fields.Many2one('res.users', required=True, readonly=True, default=lambda self: self.env.user)
    branch_manager_id = fields.Many2one('res.users', string='Reviewed By', readonly=True, copy=False, tracking=True)
    manager_reviewed_at = fields.Datetime(readonly=True, copy=False, tracking=True)
    state = fields.Selection([
        ('draft', 'Draft'), ('manager_review', 'Manager Review'),
        ('confirmed', 'Confirmed'), ('cancelled', 'Cancelled'),
    ], required=True, default='draft', readonly=True, copy=False, tracking=True)
    waiter_line_ids = fields.One2many('restaurant.waiter.daily.line', 'closing_id', string='Waiter Sales & Tips', copy=True)
    total_waiter_sales = fields.Monetary(compute='_compute_totals', store=True, tracking=True)
    total_tips = fields.Monetary(compute='_compute_totals', store=True, tracking=True)
    total_orders = fields.Integer(compute='_compute_totals', store=True, tracking=True)
    total_tables = fields.Integer(compute='_compute_totals', store=True, tracking=True)
    notes = fields.Text(tracking=True)
    company_id = fields.Many2one(related='branch_id.company_id', store=True, index=True)
    currency_id = fields.Many2one(related='company_id.currency_id', store=True)

    _active_closing_unique = models.UniqueIndex(
    "(branch_id, closing_date, shift) WHERE state != 'cancelled'",
    "An active closing already exists for this branch, date and shift.",
)

    @api.depends('waiter_line_ids.sales_amount', 'waiter_line_ids.tips_amount',
                 'waiter_line_ids.order_count', 'waiter_line_ids.table_count')
    def _compute_totals(self):
        for closing in self:
            closing.total_waiter_sales = sum(closing.waiter_line_ids.mapped('sales_amount'))
            closing.total_tips = sum(closing.waiter_line_ids.mapped('tips_amount'))
            closing.total_orders = sum(closing.waiter_line_ids.mapped('order_count'))
            closing.total_tables = sum(closing.waiter_line_ids.mapped('table_count'))

    @api.model_create_multi
    def create(self, vals_list):
        require_role(self.env, RECEPTION_GROUP)
        prepared = []
        for values in vals_list:
            values = dict(values)
            if values.get('state', 'draft') != 'draft':
                raise AccessError(self.env._('Closings must be created in draft.'))
            if set(values) & {'branch_manager_id', 'manager_reviewed_at', 'total_waiter_sales',
                              'total_tips', 'total_orders', 'total_tables', 'company_id', 'currency_id'}:
                raise AccessError(self.env._('Review and calculated fields cannot be supplied manually.'))
            values.update({
                'state': 'draft', 'reception_user_id': self.env.uid,
                'branch_manager_id': False, 'manager_reviewed_at': False,
                'name': self.env['ir.sequence'].next_by_code('restaurant.daily.closing'),
            })
            prepared.append(values)
        records = super().create(prepared)
        require_assigned_branches(records.branch_id)
        return records

    def write(self, vals):
        lock_records(self)
        editable = {'branch_id', 'closing_date', 'shift', 'waiter_line_ids', 'notes'}
        if 'state' in vals:
            if set(vals) != {'state'}:
                raise AccessError(self.env._('Save your changes before changing the workflow state.'))
            target = vals['state']
            for closing in self:
                closing._check_transition(target)
            values = dict(vals)
            if target in ('confirmed', 'draft', 'cancelled'):
                values.update(branch_manager_id=self.env.uid, manager_reviewed_at=fields.Datetime.now())
            result = super().write(values)
            # Release the partial unique index before a replacement is created.
            self.flush_recordset(['state'])
            for closing in self:
                closing.message_post(body=self.env._('Workflow changed to %s by %s.',
                    dict(self._fields['state'].selection)[target], self.env.user.name))
            return result
        if set(vals) - editable:
            raise AccessError(self.env._('System and review fields cannot be modified manually.'))
        require_role(self.env, RECEPTION_GROUP)
        require_draft(self)
        require_assigned_branches(self.branch_id)
        if 'branch_id' in vals:
            require_assigned_branches(self.env['restaurant.branch'].browse(vals['branch_id']))
        result = super().write(vals)
        if 'branch_id' in vals:
            self.waiter_line_ids._check_employee_company()
        return result

    def unlink(self):
        lock_records(self, 'unlink')
        require_role(self.env, RECEPTION_GROUP)
        require_draft(self)
        require_assigned_branches(self.branch_id)
        return super().unlink()

    def _check_transition(self, target):
        self.ensure_one()
        transitions = {
            ('draft', 'manager_review'): RECEPTION_GROUP,
            ('manager_review', 'confirmed'): MANAGER_GROUP,
            ('manager_review', 'draft'): MANAGER_GROUP,
            ('draft', 'cancelled'): RECEPTION_GROUP,
            ('manager_review', 'cancelled'): MANAGER_GROUP,
        }
        group = transitions.get((self.state, target))
        if not group:
            raise UserError(self.env._('This closing workflow transition is not allowed.'))
        require_role(self.env, group)
        require_assigned_branches(self.branch_id)

    def action_submit(self):
        self.ensure_one()
        return self.write({'state': 'manager_review'})

    def action_confirm(self):
        self.ensure_one()
        return self.write({'state': 'confirmed'})

    def action_return_to_draft(self):
        self.ensure_one()
        return self.write({'state': 'draft'})

    def action_cancel(self):
        self.ensure_one()
        return self.write({'state': 'cancelled'})

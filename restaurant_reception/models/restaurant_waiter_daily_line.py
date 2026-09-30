from odoo import api, fields, models
from odoo.exceptions import AccessError

from .reception_security import (
    RECEPTION_GROUP, check_employee_company, lock_records,
    require_assigned_branches, require_draft, require_role,
)


class RestaurantWaiterDailyLine(models.Model):
    _name = 'restaurant.waiter.daily.line'
    _description = 'Waiter Daily Sales and Tips'
    _rec_name = 'employee_id'
    _order = 'closing_date desc, id desc'

    closing_id = fields.Many2one('restaurant.daily.closing', required=True, ondelete='cascade', index=True)
    branch_id = fields.Many2one(related='closing_id.branch_id', store=True, index=True)
    closing_date = fields.Date(related='closing_id.closing_date', store=True, index=True)
    state = fields.Selection(related='closing_id.state', store=True)
    employee_id = fields.Many2one('hr.employee', required=True, ondelete='restrict', index=True)
    sales_amount = fields.Monetary(required=True, default=0)
    tips_amount = fields.Monetary(required=True, default=0)
    order_count = fields.Integer(required=True, default=0)
    table_count = fields.Integer(required=True, default=0)
    note = fields.Text()
    company_id = fields.Many2one(related='closing_id.company_id', store=True, index=True)
    currency_id = fields.Many2one(related='closing_id.currency_id', store=True)
    tip_percentage = fields.Float(compute='_compute_tip_percentage', store=True, aggregator=False)

    _nonnegative_values = models.Constraint(
        'CHECK (sales_amount >= 0 AND tips_amount >= 0 AND order_count >= 0 AND table_count >= 0)',
        'Sales, tips, order counts and table counts must not be negative.',
    )

    @api.depends('sales_amount', 'tips_amount')
    def _compute_tip_percentage(self):
        for line in self:
            line.tip_percentage = 100 * line.tips_amount / line.sales_amount if line.sales_amount else 0

    @api.constrains('employee_id', 'closing_id')
    def _check_employee_company(self):
        check_employee_company(self)

    @api.model_create_multi
    def create(self, vals_list):
        require_role(self.env, RECEPTION_GROUP)
        defaults = self.default_get(['closing_id'])
        closings = self.env['restaurant.daily.closing'].browse([
            values.get('closing_id', defaults.get('closing_id')) for values in vals_list
            if values.get('closing_id', defaults.get('closing_id'))
        ])
        lock_records(closings)
        require_draft(closings)
        require_assigned_branches(closings.branch_id)
        editable = {'closing_id', 'employee_id', 'sales_amount', 'tips_amount', 'order_count', 'table_count', 'note'}
        if any(set(values) - editable for values in vals_list):
            raise AccessError(self.env._('Calculated waiter fields cannot be supplied manually.'))
        records = super().create(vals_list)
        for closing in records.closing_id:
            closing.message_post(body=self.env._('Waiter sales lines added.'))
        return records

    def write(self, vals):
        self.check_access('write')
        require_role(self.env, RECEPTION_GROUP)
        editable = {'employee_id', 'sales_amount', 'tips_amount', 'order_count', 'table_count', 'note'}
        if set(vals) - editable:
            raise AccessError(self.env._('Waiter lines cannot be moved or their calculated fields overwritten.'))
        lock_records(self.closing_id)
        require_draft(self.closing_id)
        require_assigned_branches(self.branch_id)
        result = super().write(vals)
        for closing in self.closing_id:
            closing.message_post(body=self.env._('Waiter sales lines updated.'))
        return result

    def unlink(self):
        self.check_access('unlink')
        require_role(self.env, RECEPTION_GROUP)
        closings = self.closing_id
        lock_records(closings)
        require_draft(closings)
        require_assigned_branches(closings.branch_id)
        result = super().unlink()
        for closing in closings:
            closing.message_post(body=self.env._('Waiter sales lines removed.'))
        return result

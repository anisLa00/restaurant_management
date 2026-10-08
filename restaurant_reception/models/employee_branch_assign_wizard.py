from odoo import Command, api, fields, models
from odoo.exceptions import ValidationError

from .reception_security import HR_GROUP, require_role


class RestaurantEmployeeBranchAssignWizard(models.TransientModel):
    _name = 'restaurant.employee.branch.assign.wizard'
    _description = 'Assign Restaurant Branch to Employees'

    employee_ids = fields.Many2many('hr.employee', required=True)
    branch_id = fields.Many2one(
        'restaurant.branch',
        string='Restaurant Branch',
        required=True,
        ondelete='cascade',
        domain="[('company_id', 'in', company_ids)]",
    )
    company_ids = fields.Many2many(
        'res.company',
        compute='_compute_company_ids',
    )

    @api.model
    def default_get(self, field_names):
        values = super().default_get(field_names)
        if 'employee_ids' in field_names and self.env.context.get('active_model') == 'hr.employee':
            employee_ids = self.env.context.get('active_ids', [])
            if employee_ids:
                values['employee_ids'] = [Command.set(employee_ids)]
        return values

    @api.depends('employee_ids.company_id')
    def _compute_company_ids(self):
        for wizard in self:
            wizard.company_ids = wizard.employee_ids.company_id

    def action_assign_branch(self):
        self.ensure_one()
        require_role(self.env, HR_GROUP)
        if not self.employee_ids:
            raise ValidationError(self.env._('Select at least one employee.'))
        wrong_company = self.employee_ids.filtered(
            lambda employee: employee.company_id != self.branch_id.company_id
        )
        if wrong_company:
            raise ValidationError(self.env._(
                'Every selected employee must belong to the branch company.'
            ))
        self.employee_ids.write({'restaurant_branch_id': self.branch_id.id})
        return {
            'type': 'ir.actions.client',
            'tag': 'display_notification',
            'params': {
                'title': self.env._('Restaurant Branch Assigned'),
                'message': self.env._(
                    '%(count)s employee(s) assigned to %(branch)s.',
                    count=len(self.employee_ids),
                    branch=self.branch_id.display_name,
                ),
                'type': 'success',
                'sticky': False,
            },
        }

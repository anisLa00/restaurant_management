from odoo import Command, api, fields, models
from odoo.exceptions import ValidationError

from .reception_security import HR_GROUP, require_role


class RestaurantEmployeeStaffCategoryAssignWizard(models.TransientModel):
    _name = 'restaurant.employee.staff.category.assign.wizard'
    _description = 'Assign Staff Category to Employees'

    employee_ids = fields.Many2many(
        'hr.employee',
        relation='restaurant_staff_category_wizard_employee_rel',
        required=True,
    )
    staff_category_id = fields.Many2one(
        'restaurant.staff.category',
        string='Staff Category',
        required=True,
        ondelete='cascade',
    )

    @api.model
    def default_get(self, field_names):
        values = super().default_get(field_names)
        if 'employee_ids' in field_names and self.env.context.get('active_model') == 'hr.employee':
            employee_ids = self.env.context.get('active_ids', [])
            if employee_ids:
                values['employee_ids'] = [Command.set(employee_ids)]
        return values

    def action_assign_category(self):
        self.ensure_one()
        require_role(self.env, HR_GROUP)
        if not self.employee_ids:
            raise ValidationError(self.env._('Select at least one employee.'))
        self.employee_ids.write({'restaurant_staff_category_id': self.staff_category_id.id})
        return {
            'type': 'ir.actions.client',
            'tag': 'display_notification',
            'params': {
                'title': self.env._('Staff Category Assigned'),
                'message': self.env._(
                    '%(count)s employee(s) assigned to %(category)s.',
                    count=len(self.employee_ids),
                    category=self.staff_category_id.display_name,
                ),
                'type': 'success',
                'sticky': False,
                'next': {'type': 'ir.actions.act_window_close'},
            },
        }

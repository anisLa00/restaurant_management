from odoo import api, fields, models
from odoo.exceptions import ValidationError


class HrEmployee(models.Model):
    _inherit = 'hr.employee'

    restaurant_branch_id = fields.Many2one(
        'restaurant.branch',
        string='Restaurant Branch',
        ondelete='restrict',
        index=True,
        tracking=True,
        domain="[('company_id', '=', company_id)]",
        help=(
            'The branch whose daily attendance roster includes this employee. '
            'Changing it moves the employee only for future rosters; historical '
            'attendance remains with its original branch.'
        ),
    )
    restaurant_staff_category_id = fields.Many2one(
        'restaurant.staff.category',
        string='Staff Category',
        ondelete='restrict',
        index=True,
        tracking=True,
        help=(
            'Groups staff in the daily attendance sheet while keeping the precise '
            'Job Position on the employee card.'
        ),
    )

    @api.constrains('restaurant_branch_id', 'company_id')
    def _check_restaurant_branch_company(self):
        for employee in self:
            if (
                employee.restaurant_branch_id
                and employee.restaurant_branch_id.company_id != employee.company_id
            ):
                raise ValidationError(self.env._(
                    'The employee restaurant branch must belong to the employee company.'
                ))


class HrEmployeePublic(models.Model):
    _inherit = 'hr.employee.public'

    restaurant_branch_id = fields.Many2one(
        'restaurant.branch',
        string='Restaurant Branch',
        readonly=True,
    )
    restaurant_staff_category_id = fields.Many2one(
        'restaurant.staff.category',
        string='Staff Category',
        readonly=True,
    )

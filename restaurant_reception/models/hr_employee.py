from odoo import api, fields, models
from odoo.exceptions import ValidationError

from .reception_security import MANAGER_GROUP, require_assigned_branches, require_role


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
    restaurant_shift = fields.Selection(
        [
            ('morning', 'Morning'),
            ('evening', 'Evening'),
            ('one_shift', 'One Shift'),
        ],
        string='Current Restaurant Shift',
        index=True,
        tracking=True,
        help=(
            'Current operational shift selected by the branch manager. New daily '
            'attendance sheets copy it while historical sheets keep their original shift.'
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

    def write(self, vals):
        if 'restaurant_shift' in vals and not self.env.su:
            require_role(self.env, MANAGER_GROUP)
            require_assigned_branches(self.restaurant_branch_id)
        result = super().write(vals)
        if 'restaurant_staff_category_id' in vals:
            draft_entries = self.env['restaurant.attendance.entry'].sudo().search([
                ('employee_id', 'in', self.ids),
                ('state', '=', 'draft'),
            ])
            draft_entries._refresh_staff_details_from_employee()
        return result


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
    restaurant_shift = fields.Selection(
        [
            ('morning', 'Morning'),
            ('evening', 'Evening'),
            ('one_shift', 'One Shift'),
        ],
        string='Current Restaurant Shift',
        readonly=True,
    )

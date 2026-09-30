from odoo import api, fields, models
from odoo.exceptions import AccessError, ValidationError


class RestaurantBranch(models.Model):
    _name = 'restaurant.branch'
    _description = 'Restaurant Branch'
    _order = 'name, id'

    name = fields.Char(required=True)
    code = fields.Char(required=True, index=True)
    active = fields.Boolean(default=True)
    company_id = fields.Many2one(
        'res.company', required=True, default=lambda self: self.env.company,
        ondelete='restrict', index=True,
    )
    user_ids = fields.Many2many(
        'res.users', 'restaurant_branch_res_users_rel', 'branch_id', 'user_id',
        string='Allowed Users', groups='base.group_system',
        help='Users explicitly assigned to this branch. Manage assignments on the user form.',
    )

    _code_company_unique = models.Constraint(
        'UNIQUE(company_id, code)', 'The branch code must be unique within a company.',
    )
    _name_not_blank = models.Constraint(
        "CHECK (char_length(trim(name)) > 0)", 'The branch name cannot be blank.',
    )
    _code_not_blank = models.Constraint(
        "CHECK (char_length(trim(code)) > 0)", 'The branch code cannot be blank.',
    )

    @api.constrains('company_id', 'user_ids')
    def _check_user_companies(self):
        # Read only the linked users under sudo to validate all assignments,
        # including archived users invisible to the branch manager.
        for branch in self.sudo().with_context(active_test=False):
            if any(branch.company_id not in user.company_ids for user in branch.user_ids):
                raise ValidationError(self.env._(
                    'Every assigned user must have access to the branch company.',
                ))

    def write(self, vals):
        # Record restrictions check the old record; also protect its destination.
        if (
            'company_id' in vals
            and not self.env.su
            and vals['company_id'] not in self.env.companies.ids
        ):
            raise AccessError(self.env._(
                'You can only move a branch to one of your selected companies.',
            ))
        return super().write(vals)

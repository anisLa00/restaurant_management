from odoo import api, fields, models
from odoo.exceptions import ValidationError


class ResUsers(models.Model):
    _inherit = "res.users"

    restaurant_branch_ids = fields.Many2many(
        "restaurant.branch",
        "restaurant_branch_res_users_rel",
        "user_id",
        "branch_id",
        string="Allowed Restaurant Branches",
        groups="base.group_system",
        help="Restaurant branches assigned to this user.",
    )

    default_restaurant_branch_id = fields.Many2one(
        "restaurant.branch",
        string="Default Restaurant Branch",
        groups="base.group_system",
        help="Default branch used for restaurant operations.",
    )

    @api.constrains(
        "restaurant_branch_ids",
        "default_restaurant_branch_id",
        "company_ids",
    )
    def _check_restaurant_branch_configuration(self):
        for user in self.sudo().with_context(active_test=False):
            # Assigned branches must belong to one of the user's allowed companies.
            if user.restaurant_branch_ids.company_id - user.company_ids:
                raise ValidationError(
                    self.env._(
                        "Allowed restaurant branches must belong to the user's allowed companies."
                    )
                )

            # Default branch must belong to the user's assigned branches.
            if (
                user.default_restaurant_branch_id
                and user.default_restaurant_branch_id
                not in user.restaurant_branch_ids
            ):
                raise ValidationError(
                    self.env._(
                        "The default restaurant branch must be one of the user's allowed restaurant branches."
                    )
                )
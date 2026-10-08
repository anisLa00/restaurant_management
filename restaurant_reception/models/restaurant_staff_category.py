from odoo import fields, models


class RestaurantStaffCategory(models.Model):
    _name = 'restaurant.staff.category'
    _description = 'Restaurant Staff Category'
    _order = 'sequence, name, id'

    name = fields.Char(required=True)
    sequence = fields.Integer(default=10)
    active = fields.Boolean(default=True)

    _name_unique = models.Constraint(
        'UNIQUE(name)',
        'A staff category with this name already exists.',
    )

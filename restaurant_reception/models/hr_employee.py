from odoo import api, fields, models
from odoo.exceptions import UserError, ValidationError

from .reception_security import MANAGER_GROUP, require_assigned_branches, require_role


class HrEmployee(models.Model):
    _inherit = 'hr.employee'

    restaurant_employee_number = fields.Char(
        string='Employee Number',
        default='New',
        readonly=True,
        copy=False,
        index=True,
        tracking=True,
        help='Permanent internal employee number generated automatically by Odoo.',
    )
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
    restaurant_document_ids = fields.One2many(
        'restaurant.employee.document',
        'employee_id',
        string='HR Documents',
        groups='hr.group_hr_user,base.group_system',
    )
    restaurant_document_count = fields.Integer(
        string='Document Count',
        compute='_compute_restaurant_document_count',
        groups='hr.group_hr_user,base.group_system',
    )
    restaurant_missing_document_count = fields.Integer(
        string='Missing Required Documents',
        compute='_compute_restaurant_document_compliance',
        groups='hr.group_hr_user,base.group_system',
    )
    restaurant_missing_document_type_ids = fields.Many2many(
        'restaurant.employee.document.type',
        string='Missing Document Requirements',
        compute='_compute_restaurant_document_compliance',
        groups='hr.group_hr_user,base.group_system',
    )
    restaurant_document_compliance_state = fields.Selection(
        [
            ('complete', 'Complete'),
            ('in_progress', 'Documents In Progress'),
            ('missing', 'Missing Documents'),
        ],
        string='Employee File Status',
        compute='_compute_restaurant_document_compliance',
        groups='hr.group_hr_user,base.group_system',
    )

    _restaurant_employee_number_unique = models.Constraint(
        'UNIQUE(restaurant_employee_number)',
        'The employee number must be unique.',
    )

    @api.model_create_multi
    def create(self, vals_list):
        sequence = self.env['ir.sequence'].sudo().search([
            ('code', '=', 'restaurant.employee.number'),
            ('company_id', '=', False),
        ], limit=1)
        if not sequence:
            raise UserError(self.env._(
                'The Restaurant Employee Number sequence is not configured.'
            ))
        prepared = []
        for vals in vals_list:
            values = dict(vals)
            if not values.get('restaurant_employee_number') or values.get(
                'restaurant_employee_number'
            ) in ('New', '/'):
                values['restaurant_employee_number'] = sequence.next_by_id()
            prepared.append(values)
        return super().create(prepared)

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

    def _compute_restaurant_document_count(self):
        counts = self.env['restaurant.employee.document']._read_group(
            [('employee_id', 'in', self.ids)],
            ['employee_id'],
            ['__count'],
        )
        count_by_employee = {employee.id: count for employee, count in counts}
        for employee in self:
            employee.restaurant_document_count = count_by_employee.get(employee.id, 0)

    @api.depends(
        'restaurant_document_ids.document_type_id',
        'restaurant_document_ids.active',
        'restaurant_document_ids.document_type_id.required_for_employee',
    )
    def _compute_restaurant_document_compliance(self):
        required_types = self.env['restaurant.employee.document.type'].search([
            ('active', '=', True),
            ('required_for_employee', '=', True),
        ])
        required_type_ids = set(required_types.ids)
        for employee in self:
            present_type_ids = set(employee.restaurant_document_ids.filtered(
                'active'
            ).document_type_id.ids)
            missing_count = len(required_type_ids - present_type_ids)
            employee.restaurant_missing_document_type_ids = required_types.filtered(
                lambda document_type: document_type.id not in present_type_ids
            )
            employee.restaurant_missing_document_count = missing_count
            if not missing_count:
                compliance_state = 'complete'
            elif present_type_ids:
                compliance_state = 'in_progress'
            else:
                compliance_state = 'missing'
            employee.restaurant_document_compliance_state = compliance_state

    def action_open_restaurant_documents(self):
        self.ensure_one()
        return {
            'type': 'ir.actions.act_window',
            'name': self.env._('Employee Documents'),
            'res_model': 'restaurant.employee.document',
            'view_mode': 'list,form',
            'domain': [('employee_id', '=', self.id)],
            'context': {
                'default_employee_id': self.id,
                'default_responsible_user_id': self.hr_responsible_id.id
                or self.env.user.id,
            },
        }


class HrEmployeePublic(models.Model):
    _inherit = 'hr.employee.public'

    restaurant_employee_number = fields.Char(
        string='Employee Number',
        readonly=True,
    )
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

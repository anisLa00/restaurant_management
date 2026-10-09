import operator as py_operator

from odoo import api, fields, models
from odoo.exceptions import UserError, ValidationError

from .reception_security import MANAGER_GROUP, require_assigned_branches, require_role


class HrEmployee(models.Model):
    _inherit = 'hr.employee'

    _RESTAURANT_DOCUMENT_REQUIREMENTS = {
        'new_joiner': {
            'passport',
            'signed_job_offer',
            'current_visa_entry_permit',
        },
        'transfer_pending': {
            'passport',
            'signed_job_offer',
            'current_visa_entry_permit',
            'previous_emirates_id',
            'residence_cancellation',
        },
        'visa_in_process': {
            'passport',
            'employment_contract',
            'work_permit',
            'work_entry_permit',
            'medical_fitness',
            'visa_processing_receipt',
        },
        'company_sponsored': {
            'passport',
            'emirates_id',
            'residence_visa',
            'work_permit',
            'employment_contract',
            'medical_fitness',
        },
        'other_sponsor': {
            'passport',
            'emirates_id',
            'residence_visa',
            'work_permit',
            'employment_contract',
        },
    }

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
    restaurant_immigration_status = fields.Selection(
        [
            ('new_joiner', 'Pre-Employment - Visit Visa / No Company Residence'),
            ('transfer_pending', 'Transfer Pending - Previous Employer Residence'),
            ('visa_in_process', 'Company Visa In Process'),
            ('company_sponsored', 'Residence Sponsored by This Company'),
            ('other_sponsor', 'Valid Residence under Family / Self / Other Sponsor'),
        ],
        string='Visa / Sponsorship Status',
        groups='hr.group_hr_user,base.group_system',
        tracking=True,
        help=(
            'Controls which employee documents are required. Existing employees '
            'remain unset until HR confirms their current sponsorship status.'
        ),
    )
    restaurant_current_sponsor_name = fields.Char(
        string='Current Sponsor',
        groups='hr.group_hr_user,base.group_system',
        tracking=True,
        help='Required when the employee residence is sponsored by another sponsor.',
    )
    restaurant_work_authorized = fields.Boolean(
        string='Employment Documents Verified',
        default=False,
        groups='hr.group_hr_user,base.group_system',
        tracking=True,
        help=(
            'HR confirms that the work-authorisation documents were verified. '
            'A visit visa alone is not a work authorisation.'
        ),
    )
    restaurant_work_authorized_by_id = fields.Many2one(
        'res.users',
        string='Employment Verification Confirmed By',
        readonly=True,
        copy=False,
        groups='hr.group_hr_user,base.group_system',
    )
    restaurant_work_authorized_on = fields.Datetime(
        string='Employment Verification Confirmed On',
        readonly=True,
        copy=False,
        groups='hr.group_hr_user,base.group_system',
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
        search='_search_restaurant_document_compliance_state',
        groups='hr.group_hr_user,base.group_system',
    )
    restaurant_expiring_document_count = fields.Integer(
        string='Expiring Documents',
        compute='_compute_restaurant_document_expiry_summary',
        search='_search_restaurant_expiring_document_count',
        groups='hr.group_hr_user,base.group_system',
    )
    restaurant_expired_document_count = fields.Integer(
        string='Expired Documents',
        compute='_compute_restaurant_document_expiry_summary',
        search='_search_restaurant_expired_document_count',
        groups='hr.group_hr_user,base.group_system',
    )
    restaurant_next_document_expiry = fields.Date(
        string='Next Document Expiry',
        compute='_compute_restaurant_document_expiry_summary',
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
            values.setdefault('restaurant_immigration_status', 'new_joiner')
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

    @api.constrains(
        'restaurant_immigration_status',
        'restaurant_current_sponsor_name',
    )
    def _check_restaurant_sponsor_name(self):
        for employee in self:
            if (
                employee.restaurant_immigration_status == 'other_sponsor'
                and not employee.restaurant_current_sponsor_name
            ):
                raise ValidationError(self.env._(
                    'Enter the current sponsor for an employee sponsored by another sponsor.'
                ))

    @api.constrains(
        'restaurant_work_authorized',
        'restaurant_immigration_status',
    )
    def _check_restaurant_work_authorization(self):
        today = fields.Date.context_today(self)
        for employee in self.filtered('restaurant_work_authorized'):
            if not employee.restaurant_immigration_status:
                # Legacy employees are backfilled during the upgrade. HR can
                # classify them later without interrupting current rosters.
                continue
            if employee.restaurant_immigration_status in (
                'new_joiner', 'transfer_pending',
            ):
                raise ValidationError(self.env._(
                    'An employee on a visit visa or pending a previous-employer '
                    'transfer cannot be cleared to work.'
                ))
            valid_codes = set(employee.restaurant_document_ids.filtered(
                lambda document: document.active
                and document.document_type_id.code in ('work_permit', 'work_entry_permit')
                and (not document.expiry_date or document.expiry_date >= today)
            ).document_type_id.mapped('code'))
            if 'work_permit' not in valid_codes:
                raise ValidationError(self.env._(
                    'Upload a valid Work Permit / Labour Card before clearing '
                    'this employee to work.'
                ))
            if (
                employee.restaurant_immigration_status == 'visa_in_process'
                and 'work_entry_permit' not in valid_codes
            ):
                raise ValidationError(self.env._(
                    'Upload the Company Work Entry Permit / Change of Status '
                    'before clearing this in-process employee to work.'
                ))

    def write(self, vals):
        values = dict(vals)
        if values.get('restaurant_immigration_status') in (
            'new_joiner', 'transfer_pending',
        ):
            values.update({
                'restaurant_work_authorized': False,
                'restaurant_work_authorized_by_id': False,
                'restaurant_work_authorized_on': False,
            })
        elif 'restaurant_work_authorized' in values:
            if values['restaurant_work_authorized']:
                values.update({
                    'restaurant_work_authorized_by_id': self.env.user.id,
                    'restaurant_work_authorized_on': fields.Datetime.now(),
                })
            else:
                values.update({
                    'restaurant_work_authorized_by_id': False,
                    'restaurant_work_authorized_on': False,
                })
        if 'restaurant_shift' in values and not self.env.su:
            require_role(self.env, MANAGER_GROUP)
            require_assigned_branches(self.restaurant_branch_id)
        result = super().write(values)
        if 'restaurant_staff_category_id' in values:
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
        'restaurant_immigration_status',
    )
    def _compute_restaurant_document_compliance(self):
        for employee in self:
            required_types = employee._get_required_restaurant_document_types()
            required_type_ids = set(required_types.ids)
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

    @api.depends(
        'restaurant_document_ids.active',
        'restaurant_document_ids.status',
        'restaurant_document_ids.expiry_date',
    )
    def _compute_restaurant_document_expiry_summary(self):
        today = fields.Date.context_today(self)
        for employee in self:
            documents = employee.restaurant_document_ids.filtered('active')
            employee.restaurant_expiring_document_count = len(
                documents.filtered(lambda document: document.status == 'expiring')
            )
            employee.restaurant_expired_document_count = len(
                documents.filtered(lambda document: document.status == 'expired')
            )
            future_expiries = [
                document.expiry_date
                for document in documents
                if document.expiry_date and document.expiry_date >= today
            ]
            employee.restaurant_next_document_expiry = (
                min(future_expiries) if future_expiries else False
            )

    def _search_restaurant_computed_value(self, field_name, operator, value):
        comparators = {
            '=': py_operator.eq,
            '!=': py_operator.ne,
            '>': py_operator.gt,
            '>=': py_operator.ge,
            '<': py_operator.lt,
            '<=': py_operator.le,
            'in': lambda left, right: left in right,
            'not in': lambda left, right: left not in right,
        }
        comparator = comparators.get(operator)
        if not comparator:
            return NotImplemented
        employees = self.sudo().with_context(active_test=False).search([])
        matching_ids = [
            employee.id
            for employee in employees
            if comparator(getattr(employee, field_name), value)
        ]
        return [('id', 'in', matching_ids)]

    def _search_restaurant_document_compliance_state(self, operator, value):
        return self._search_restaurant_computed_value(
            'restaurant_document_compliance_state', operator, value,
        )

    def _search_restaurant_expiring_document_count(self, operator, value):
        return self._search_restaurant_computed_value(
            'restaurant_expiring_document_count', operator, value,
        )

    def _search_restaurant_expired_document_count(self, operator, value):
        return self._search_restaurant_computed_value(
            'restaurant_expired_document_count', operator, value,
        )

    def _get_required_restaurant_document_types(self):
        self.ensure_one()
        document_type_model = self.env['restaurant.employee.document.type']
        requirement_codes = self._RESTAURANT_DOCUMENT_REQUIREMENTS.get(
            self.restaurant_immigration_status
        )
        if requirement_codes is None:
            return document_type_model.search([
                ('active', '=', True),
                ('required_for_employee', '=', True),
            ])
        return document_type_model.search([
            ('active', '=', True),
            ('code', 'in', list(requirement_codes)),
        ])

    def _update_restaurant_immigration_status_from_documents(self):
        """Complete the company-sponsored stage once every final document exists.

        Only an in-progress company application is promoted automatically.  Older
        Emirates IDs or residence copies therefore cannot promote a pre-employment
        or transfer-pending employee by accident.
        """
        final_codes = self._RESTAURANT_DOCUMENT_REQUIREMENTS['company_sponsored']
        for employee in self.filtered(
            lambda record: record.restaurant_immigration_status == 'visa_in_process'
        ):
            present_codes = set(employee.restaurant_document_ids.filtered(
                'active'
            ).document_type_id.mapped('code'))
            if final_codes.issubset(present_codes):
                employee.restaurant_immigration_status = 'company_sponsored'

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

    def action_open_document_upload_wizard(self):
        self.ensure_one()
        existing_type_ids = set(self.restaurant_document_ids.filtered(
            'active'
        ).document_type_id.ids)
        document_types = self.env['restaurant.employee.document.type'].search([
            ('active', '=', True),
            ('id', 'not in', list(existing_type_ids)),
        ])
        wizard = self.env['restaurant.employee.document.upload.wizard'].create({
            'employee_id': self.id,
            'line_ids': [
                (0, 0, {'document_type_id': document_type.id})
                for document_type in document_types
            ],
        })
        return {
            'type': 'ir.actions.act_window',
            'name': self.env._('Upload Employee Documents'),
            'res_model': 'restaurant.employee.document.upload.wizard',
            'res_id': wizard.id,
            'view_mode': 'form',
            'target': 'new',
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

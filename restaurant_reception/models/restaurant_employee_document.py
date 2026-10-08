from datetime import timedelta

from odoo import api, fields, models
from odoo.exceptions import ValidationError


class RestaurantEmployeeDocumentType(models.Model):
    _name = 'restaurant.employee.document.type'
    _description = 'Employee Document Type'
    _order = 'sequence, name'

    name = fields.Char(required=True, translate=True)
    code = fields.Char(required=True, index=True)
    sequence = fields.Integer(default=10)
    active = fields.Boolean(default=True)
    required_for_employee = fields.Boolean(
        string='Required for Employee File',
        default=True,
        help='Include this type in the employee file completeness check.',
    )
    expiry_required = fields.Boolean(
        string='Expiry Date Required',
        default=True,
        help='Require an expiry date whenever this document type is recorded.',
    )
    alert_days = fields.Integer(
        string='Alert Before Expiry',
        default=60,
        required=True,
        help='Create an HR reminder this many days before expiry.',
    )

    _document_type_code_unique = models.Constraint(
        'UNIQUE(code)',
        'The employee document type code must be unique.',
    )
    _document_type_alert_days_positive = models.Constraint(
        'CHECK(alert_days >= 0)',
        'Alert days cannot be negative.',
    )


class RestaurantEmployeeDocument(models.Model):
    _name = 'restaurant.employee.document'
    _description = 'Employee Document'
    _inherit = ['mail.thread', 'mail.activity.mixin']
    _order = 'expiry_date, employee_id, document_type_id'

    name = fields.Char(compute='_compute_name', store=True)
    employee_id = fields.Many2one(
        'hr.employee',
        required=True,
        ondelete='cascade',
        index=True,
        tracking=True,
    )
    employee_number = fields.Char(
        related='employee_id.restaurant_employee_number',
        string='Employee Number',
        store=True,
        readonly=True,
    )
    branch_id = fields.Many2one(
        related='employee_id.restaurant_branch_id',
        string='Restaurant Branch',
        store=True,
        readonly=True,
    )
    company_id = fields.Many2one(
        related='employee_id.company_id',
        store=True,
        readonly=True,
        index=True,
    )
    document_type_id = fields.Many2one(
        'restaurant.employee.document.type',
        string='Document Type',
        required=True,
        ondelete='restrict',
        index=True,
        tracking=True,
    )
    document_number = fields.Char(index=True, tracking=True)
    issue_date = fields.Date(tracking=True)
    expiry_date = fields.Date(index=True, tracking=True)
    status = fields.Selection(
        [
            ('no_expiry', 'No Expiry'),
            ('valid', 'Valid'),
            ('expiring', 'Expiring Soon'),
            ('expired', 'Expired'),
        ],
        compute='_compute_status',
        store=True,
        index=True,
    )
    days_to_expiry = fields.Integer(
        string='Days to Expiry',
        compute='_compute_days_to_expiry',
    )
    responsible_user_id = fields.Many2one(
        'res.users',
        string='HR Responsible',
        required=True,
        default=lambda self: self.env.user,
        domain="[('share', '=', False)]",
        tracking=True,
    )
    document_file = fields.Binary(
        string='Document File',
        attachment=True,
        required=True,
    )
    document_filename = fields.Char()
    notes = fields.Text()
    active = fields.Boolean(default=True, tracking=True)
    alerted_expiry_date = fields.Date(copy=False, readonly=True)
    expiry_activity_id = fields.Many2one(
        'mail.activity',
        copy=False,
        readonly=True,
        ondelete='set null',
    )

    @api.depends('employee_id.name', 'document_type_id.name', 'document_number')
    def _compute_name(self):
        for document in self:
            parts = [document.employee_id.name, document.document_type_id.name]
            if document.document_number:
                parts.append(document.document_number)
            document.name = ' - '.join(filter(None, parts)) or self.env._('Employee Document')

    @api.depends('expiry_date', 'document_type_id.alert_days')
    def _compute_status(self):
        today = fields.Date.context_today(self)
        for document in self:
            if not document.expiry_date:
                document.status = 'no_expiry'
            elif document.expiry_date < today:
                document.status = 'expired'
            elif document.expiry_date <= today + timedelta(
                days=document.document_type_id.alert_days
            ):
                document.status = 'expiring'
            else:
                document.status = 'valid'

    @api.depends('expiry_date')
    def _compute_days_to_expiry(self):
        today = fields.Date.context_today(self)
        for document in self:
            document.days_to_expiry = (
                (document.expiry_date - today).days
                if document.expiry_date else 0
            )

    @api.onchange('employee_id')
    def _onchange_employee_id(self):
        if self.employee_id and not self.responsible_user_id:
            self.responsible_user_id = (
                self.employee_id.hr_responsible_id or self.env.user
            )

    @api.model_create_multi
    def create(self, vals_list):
        prepared = []
        for vals in vals_list:
            values = dict(vals)
            if values.get('employee_id') and not values.get('responsible_user_id'):
                employee = self.env['hr.employee'].browse(values['employee_id'])
                values['responsible_user_id'] = (
                    employee.hr_responsible_id.id or self.env.user.id
                )
            prepared.append(values)
        return super().create(prepared)

    def write(self, vals):
        reminder_fields = {'expiry_date', 'document_type_id', 'responsible_user_id'}
        if reminder_fields.intersection(vals):
            activities = self.sudo().mapped('expiry_activity_id').exists()
            if activities:
                activities.unlink()
            vals = dict(vals, alerted_expiry_date=False, expiry_activity_id=False)
        elif vals.get('active') is False:
            activities = self.sudo().mapped('expiry_activity_id').exists()
            if activities:
                activities.unlink()
            vals = dict(vals, expiry_activity_id=False)
        return super().write(vals)

    @api.constrains('issue_date', 'expiry_date')
    def _check_document_dates(self):
        for document in self:
            if (
                document.issue_date
                and document.expiry_date
                and document.expiry_date < document.issue_date
            ):
                raise ValidationError(self.env._(
                    'The document expiry date cannot be before its issue date.'
                ))

    @api.constrains('document_type_id', 'expiry_date')
    def _check_required_expiry_date(self):
        for document in self:
            if document.document_type_id.expiry_required and not document.expiry_date:
                raise ValidationError(self.env._(
                    'An expiry date is required for %(document_type)s.',
                    document_type=document.document_type_id.name,
                ))

    @api.model
    def _cron_check_document_expiry(self):
        documents = self.sudo().search([('active', '=', True)])
        documents._compute_status()
        documents.flush_recordset(['status'])
        due_documents = documents.filtered(
            lambda document: document.status in ('expiring', 'expired')
            and document.expiry_date
            and document.alerted_expiry_date != document.expiry_date
        )
        for document in due_documents:
            activity = document.activity_schedule(
                'mail.mail_activity_data_todo',
                date_deadline=document.expiry_date,
                user_id=document.responsible_user_id.id,
                summary=self.env._(
                    '%(document_type)s for %(employee)s requires renewal',
                    document_type=document.document_type_id.name,
                    employee=document.employee_id.name,
                ),
                note=self.env._(
                    'Review and renew this employee document before its expiry date.'
                ),
            )
            document.write({
                'alerted_expiry_date': document.expiry_date,
                'expiry_activity_id': activity.id,
            })
        return True

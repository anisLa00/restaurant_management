from odoo import api, fields, models
from odoo.exceptions import UserError, ValidationError


class EmployeeDocumentUploadWizard(models.TransientModel):
    _name = 'restaurant.employee.document.upload.wizard'
    _description = 'Upload Employee Documents'

    employee_id = fields.Many2one(
        'hr.employee',
        required=True,
        readonly=True,
        ondelete='cascade',
    )
    line_ids = fields.One2many(
        'restaurant.employee.document.upload.wizard.line',
        'wizard_id',
        string='Document Checklist',
    )

    def action_save_documents(self):
        self.ensure_one()
        uploaded_lines = self.line_ids.filtered('document_file')
        if not uploaded_lines:
            raise UserError(self.env._('Upload at least one document before saving.'))

        for line in uploaded_lines:
            if line.document_type_id.expiry_required and not line.expiry_date:
                raise ValidationError(self.env._(
                    'Enter the expiry date for %(document_type)s.',
                    document_type=line.document_type_id.name,
                ))
            self.env['restaurant.employee.document'].create({
                'employee_id': self.employee_id.id,
                'document_type_id': line.document_type_id.id,
                'document_number': line.document_number,
                'issue_date': line.issue_date,
                'expiry_date': line.expiry_date,
                'responsible_user_id': line.responsible_user_id.id,
                'document_file': line.document_file,
                'document_filename': line.document_filename,
                'notes': line.notes,
            })

        return {
            'type': 'ir.actions.act_window',
            'name': self.employee_id.display_name,
            'res_model': 'hr.employee',
            'res_id': self.employee_id.id,
            'view_mode': 'form',
            'target': 'current',
        }


class EmployeeDocumentUploadWizardLine(models.TransientModel):
    _name = 'restaurant.employee.document.upload.wizard.line'
    _description = 'Employee Document Upload Line'
    _order = 'sequence, document_type_id'

    wizard_id = fields.Many2one(
        'restaurant.employee.document.upload.wizard',
        required=True,
        ondelete='cascade',
    )
    document_type_id = fields.Many2one(
        'restaurant.employee.document.type',
        string='Requirement',
        required=True,
        readonly=True,
    )
    sequence = fields.Integer(related='document_type_id.sequence')
    required_for_employee = fields.Boolean(
        compute='_compute_required_for_employee',
        string='Required',
    )
    document_number = fields.Char(string='Document Number')
    issue_date = fields.Date()
    expiry_date = fields.Date()
    responsible_user_id = fields.Many2one(
        'res.users',
        string='HR Responsible',
        required=True,
        default=lambda self: self.env.user,
        domain="[('share', '=', False)]",
    )
    document_file = fields.Binary(string='Upload File', attachment=True)
    document_filename = fields.Char()
    notes = fields.Char()

    @api.depends(
        'wizard_id.employee_id.restaurant_immigration_status',
        'document_type_id',
    )
    def _compute_required_for_employee(self):
        for line in self:
            required_types = (
                line.wizard_id.employee_id._get_required_restaurant_document_types()
                if line.wizard_id.employee_id
                else self.env['restaurant.employee.document.type']
            )
            line.required_for_employee = line.document_type_id in required_types

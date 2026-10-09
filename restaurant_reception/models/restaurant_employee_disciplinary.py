import base64
from datetime import timedelta
from io import BytesIO
from pathlib import Path
from xml.sax.saxutils import escape

from odoo import api, fields, models
from odoo.exceptions import AccessError, UserError, ValidationError
from odoo.tools.misc import format_date
from reportlab.lib import colors
from reportlab.lib.enums import TA_CENTER
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
from reportlab.lib.units import mm
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont
from reportlab.platypus import (
    HRFlowable,
    Paragraph,
    SimpleDocTemplate,
    Spacer,
    Table,
    TableStyle,
)

from .reception_security import HR_GROUP, MANAGER_GROUP, require_assigned_branches


MANAGER_EDITABLE_FIELDS = {
    'employee_id', 'branch_id', 'incident_date', 'discovered_date',
    'incident_category', 'severity', 'allegation_summary', 'incident_details',
    'witnesses', 'evidence_file', 'evidence_filename',
}
HR_EDITABLE_FIELDS = MANAGER_EDITABLE_FIELDS | {
    'policy_rule_reference', 'hr_charge_statement',
    'charge_notice_date', 'charge_notice_file', 'charge_notice_filename',
    'employee_response_status', 'employee_statement', 'employee_statement_file',
    'employee_statement_filename', 'investigation_summary',
    'investigation_completed_date', 'investigation_report_file',
    'investigation_report_filename', 'violation_established', 'decision_type',
    'sanction_days', 'sanction_months', 'decision_reason', 'repeat_consequence',
    'decision_notice_date', 'decision_notice_file', 'decision_notice_filename',
    'acknowledgement_status', 'acknowledgement_date',
    'acknowledgement_file', 'acknowledgement_filename', 'grievance_date',
    'grievance_text', 'grievance_file', 'grievance_filename',
    'grievance_outcome', 'grievance_outcome_note', 'grievance_outcome_date',
    'grievance_outcome_file', 'grievance_outcome_filename',
}


class RestaurantEmployeeDisciplinaryCase(models.Model):
    _name = 'restaurant.employee.disciplinary.case'
    _description = 'Restaurant Employee Disciplinary Case'
    _inherit = ['mail.thread', 'mail.activity.mixin']
    _rec_name = 'reference'
    _order = 'incident_date desc, id desc'
    _mail_post_access = 'read'

    reference = fields.Char(
        required=True, default='New', readonly=True, copy=False, index=True,
    )
    employee_id = fields.Many2one(
        'hr.employee', required=True, ondelete='restrict', index=True, tracking=True,
    )
    employee_number = fields.Char(
        related='employee_id.restaurant_employee_number', readonly=True,
    )
    branch_id = fields.Many2one(
        'restaurant.branch', required=True, ondelete='restrict', index=True,
        tracking=True, string='Restaurant Branch',
    )
    company_id = fields.Many2one(
        related='branch_id.company_id', store=True, index=True,
    )
    job_id = fields.Many2one(
        'hr.job', string='Position at Incident', readonly=True, copy=False,
    )
    incident_date = fields.Date(
        required=True, default=fields.Date.context_today, index=True, tracking=True,
    )
    discovered_date = fields.Date(
        required=True, default=fields.Date.context_today, index=True, tracking=True,
    )
    allegation_deadline = fields.Date(
        compute='_compute_legal_deadlines', store=True,
        help='Last day to accuse the worker, calculated as 30 days from discovery.',
    )
    decision_deadline = fields.Date(
        compute='_compute_legal_deadlines', store=True,
        help='Last day to impose a sanction, calculated as 60 days from investigation completion.',
    )
    incident_category = fields.Selection([
        ('attendance', 'Attendance / Timekeeping'),
        ('conduct', 'Conduct / Behaviour'),
        ('safety', 'Health & Safety'),
        ('cash_property', 'Cash / Company Property'),
        ('service', 'Service / Operational Procedure'),
        ('confidentiality', 'Confidentiality / Data'),
        ('harassment', 'Harassment / Discrimination'),
        ('policy', 'Policy Violation'),
        ('other', 'Other'),
    ], required=True, default='conduct', index=True, tracking=True)
    severity = fields.Selection([
        ('low', 'Low'),
        ('medium', 'Medium'),
        ('high', 'High'),
        ('critical', 'Critical'),
    ], required=True, default='medium', index=True, tracking=True)
    allegation_summary = fields.Char(required=True, tracking=True)
    incident_details = fields.Text(required=True, tracking=True)
    witnesses = fields.Text()
    evidence_file = fields.Binary(attachment=True, copy=False)
    evidence_filename = fields.Char(copy=False)
    reported_by = fields.Many2one(
        'res.users', required=True, readonly=True, copy=False,
        default=lambda self: self.env.user,
    )
    reported_at = fields.Datetime(readonly=True, copy=False)

    state = fields.Selection([
        ('draft', 'Manager Draft'),
        ('submitted', 'Submitted to HR'),
        ('investigation', 'Under Investigation'),
        ('decision_pending', 'HR Decision'),
        ('issued', 'Decision Issued'),
        ('grievance', 'Grievance Review'),
        ('closed', 'Closed'),
        ('dismissed', 'Not Established / Dismissed'),
    ], required=True, default='draft', readonly=True, copy=False, index=True,
        tracking=True)

    charge_notice_date = fields.Date(copy=False, tracking=True)
    policy_rule_reference = fields.Text(
        string='Company Policy / Rule Cited', copy=False, tracking=True,
        help='Quote or reference the company rule allegedly breached. This is not a final finding.',
    )
    hr_charge_statement = fields.Text(
        string='HR Written Allegation', copy=False, tracking=True,
        help='The formal allegation written by HR for the employee charge notice.',
    )
    charge_notice_file = fields.Binary(
        string='Written Charge Notice', attachment=True, copy=False,
    )
    charge_notice_filename = fields.Char(copy=False)
    employee_response_status = fields.Selection([
        ('pending', 'Pending'),
        ('provided', 'Statement Provided'),
        ('declined', 'Employee Declined'),
        ('unavailable', 'Employee Unavailable'),
    ], required=True, default='pending', copy=False, tracking=True)
    employee_statement = fields.Text(copy=False, tracking=True)
    employee_statement_file = fields.Binary(attachment=True, copy=False)
    employee_statement_filename = fields.Char(copy=False)
    investigation_summary = fields.Text(copy=False, tracking=True)
    investigation_completed_date = fields.Date(copy=False, tracking=True)
    investigation_report_file = fields.Binary(
        string='Investigation Report', attachment=True, copy=False,
    )
    investigation_report_filename = fields.Char(copy=False)
    violation_established = fields.Selection([
        ('pending', 'Pending'),
        ('yes', 'Established'),
        ('no', 'Not Established'),
    ], required=True, default='pending', copy=False, tracking=True)
    investigated_by = fields.Many2one('res.users', readonly=True, copy=False)
    investigation_started_at = fields.Datetime(readonly=True, copy=False)
    investigation_completed_at = fields.Datetime(readonly=True, copy=False)

    decision_type = fields.Selection([
        ('written_notice', 'Written Notice'),
        ('written_warning', 'Written Warning'),
        ('wage_deduction', 'Wage Deduction'),
        ('unpaid_suspension', 'Unpaid Suspension'),
        ('bonus_deprivation', 'Deprivation of Periodic Bonus'),
        ('promotion_deprivation', 'Deprivation of Promotion'),
        ('termination', 'Termination with End-of-Service Rights'),
    ], copy=False, tracking=True)
    sanction_days = fields.Integer(copy=False, tracking=True)
    sanction_months = fields.Integer(copy=False, tracking=True)
    decision_reason = fields.Text(copy=False, tracking=True)
    repeat_consequence = fields.Text(copy=False)
    decision_notice_date = fields.Date(copy=False, tracking=True)
    decision_notice_file = fields.Binary(
        string='Written Decision Notice', attachment=True, copy=False,
    )
    decision_notice_filename = fields.Char(copy=False)
    decided_by = fields.Many2one('res.users', readonly=True, copy=False)
    decided_at = fields.Datetime(readonly=True, copy=False)

    acknowledgement_status = fields.Selection([
        ('pending', 'Pending'),
        ('acknowledged', 'Acknowledged'),
        ('refused', 'Signature Refused'),
        ('unavailable', 'Employee Unavailable'),
    ], required=True, default='pending', copy=False, tracking=True)
    acknowledgement_date = fields.Date(copy=False, tracking=True)
    acknowledgement_file = fields.Binary(
        string='Acknowledgement / Delivery Proof', attachment=True, copy=False,
    )
    acknowledgement_filename = fields.Char(copy=False)
    grievance_date = fields.Date(copy=False, tracking=True)
    grievance_text = fields.Text(copy=False, tracking=True)
    grievance_file = fields.Binary(
        string='Employee Grievance', attachment=True, copy=False,
    )
    grievance_filename = fields.Char(copy=False)
    grievance_outcome = fields.Selection([
        ('upheld', 'Decision Upheld'),
        ('reduced', 'Sanction Reduced'),
        ('revoked', 'Decision Revoked'),
    ], copy=False, tracking=True)
    grievance_outcome_note = fields.Text(copy=False, tracking=True)
    grievance_outcome_date = fields.Date(copy=False, tracking=True)
    grievance_outcome_file = fields.Binary(
        string='Written Grievance Outcome', attachment=True, copy=False,
    )
    grievance_outcome_filename = fields.Char(copy=False)
    grievance_resolved_by = fields.Many2one('res.users', readonly=True, copy=False)
    closed_by = fields.Many2one('res.users', readonly=True, copy=False)
    closed_at = fields.Datetime(readonly=True, copy=False)

    _reference_unique = models.Constraint(
        'UNIQUE(reference)', 'The disciplinary case reference must be unique.',
    )

    def _is_hr(self):
        return self.env.su or self.env.user.has_group(HR_GROUP) or self.env.user.has_group(
            'base.group_system'
        )

    def _is_manager(self):
        return self.env.user.has_group(MANAGER_GROUP)

    def _require_hr(self):
        if not self._is_hr():
            raise AccessError(self.env._('Only HR can perform this disciplinary action.'))

    def _require_manager_or_hr(self):
        if not self._is_hr() and not self._is_manager():
            raise AccessError(self.env._(
                'Only an assigned Branch Manager or HR can create a disciplinary case.'
            ))

    @api.depends('discovered_date', 'investigation_completed_date')
    def _compute_legal_deadlines(self):
        for case in self:
            case.allegation_deadline = (
                case.discovered_date + timedelta(days=30)
                if case.discovered_date else False
            )
            case.decision_deadline = (
                case.investigation_completed_date + timedelta(days=60)
                if case.investigation_completed_date else False
            )

    @api.constrains('incident_date', 'discovered_date')
    def _check_incident_dates(self):
        for case in self:
            if case.incident_date and case.discovered_date < case.incident_date:
                raise ValidationError(self.env._(
                    'The discovery date cannot be before the incident date.'
                ))

    @api.constrains('employee_id', 'branch_id')
    def _check_employee_branch_company(self):
        for case in self:
            employee = case.employee_id.sudo()
            if employee.company_id != case.branch_id.company_id:
                raise ValidationError(self.env._(
                    'The employee and branch must belong to the same company.'
                ))
            if (
                employee.restaurant_branch_id
                and employee.restaurant_branch_id != case.branch_id
            ):
                raise ValidationError(self.env._(
                    'The disciplinary case must use the employee current restaurant branch.'
                ))

    @api.model_create_multi
    def create(self, vals_list):
        self._require_manager_or_hr()
        sequence = self.env['ir.sequence'].sudo().search([
            ('code', '=', 'restaurant.employee.disciplinary.case'),
            ('company_id', '=', False),
        ], limit=1)
        if not sequence:
            raise UserError(self.env._('The disciplinary case sequence is not configured.'))
        prepared = []
        for vals in vals_list:
            values = dict(vals)
            if values.get('state', 'draft') != 'draft':
                raise AccessError(self.env._('A disciplinary case must start in Draft.'))
            if not values.get('reference') or values.get('reference') in ('New', '/'):
                values['reference'] = sequence.next_by_id()
            employee = self.env['hr.employee'].browse(values.get('employee_id')).exists()
            if not employee:
                raise ValidationError(self.env._('Choose an employee.'))
            branch = self.env['restaurant.branch'].browse(values.get('branch_id')).exists()
            if not branch:
                raise ValidationError(self.env._('Choose the incident branch.'))
            if not self._is_hr():
                require_assigned_branches(branch)
            values.update({
                'state': 'draft',
                'job_id': employee.sudo().job_id.id,
                'reported_by': self.env.uid,
                'reported_at': False,
            })
            prepared.append(values)
        return super().create(prepared)

    def write(self, vals):
        if self.env.context.get('restaurant_disciplinary_internal'):
            return super().write(vals)
        if 'state' in vals or set(vals) - HR_EDITABLE_FIELDS:
            raise AccessError(self.env._(
                'Audit and workflow fields cannot be edited manually.'
            ))
        if self._is_hr():
            closed_grievance_fields = {
                'grievance_date', 'grievance_text',
                'grievance_file', 'grievance_filename',
            }
            closed_grievance_entry = (
                all(case.state == 'closed' for case in self)
                and set(vals) <= closed_grievance_fields
            )
            if (
                any(case.state in ('closed', 'dismissed') for case in self)
                and not closed_grievance_entry
            ):
                raise AccessError(self.env._('Closed disciplinary cases are locked.'))
            if any(case.state != 'draft' for case in self) and set(vals) & MANAGER_EDITABLE_FIELDS:
                raise AccessError(self.env._(
                    'Incident details cannot change after submission to HR.'
                ))
            return super().write(vals)
        if not self._is_manager() or any(case.state != 'draft' for case in self):
            raise AccessError(self.env._(
                'Managers can edit only their own branch draft cases.'
            ))
        if set(vals) - MANAGER_EDITABLE_FIELDS:
            raise AccessError(self.env._('Managers cannot edit HR investigation fields.'))
        require_assigned_branches(self.branch_id)
        if vals.get('branch_id'):
            require_assigned_branches(
                self.env['restaurant.branch'].browse(vals['branch_id'])
            )
        return super().write(vals)

    def unlink(self):
        if any(case.state != 'draft' for case in self):
            raise AccessError(self.env._(
                'Submitted disciplinary cases are audit records and cannot be deleted.'
            ))
        self._require_manager_or_hr()
        if not self._is_hr():
            require_assigned_branches(self.branch_id)
        return super().unlink()

    def _workflow_write(self, values):
        return self.with_context(restaurant_disciplinary_internal=True).write(values)

    def action_submit_to_hr(self):
        for case in self:
            if case.state != 'draft':
                raise UserError(self.env._('Only a draft case can be submitted to HR.'))
            case._require_manager_or_hr()
            if not case._is_hr():
                require_assigned_branches(case.branch_id)
            if not case.allegation_summary or not case.incident_details:
                raise ValidationError(self.env._(
                    'Enter the allegation summary and full incident details.'
                ))
            case._workflow_write({
                'state': 'submitted',
                'reported_at': fields.Datetime.now(),
            })
            case.message_post(body=self.env._('Disciplinary case submitted to HR.'))
        return True

    def action_start_investigation(self):
        for case in self:
            case._require_hr()
            if case.state != 'submitted':
                raise UserError(self.env._('Only a submitted case can enter investigation.'))
            if not case.policy_rule_reference or not case.hr_charge_statement:
                raise ValidationError(self.env._(
                    'Enter the company policy or rule cited and the formal HR written allegation.'
                ))
            if not case.charge_notice_date or not case.charge_notice_file:
                raise ValidationError(self.env._(
                    'Record the written charge notice date and upload its signed copy.'
                ))
            if case.charge_notice_date < case.discovered_date:
                raise ValidationError(self.env._(
                    'The written charge notice cannot predate discovery of the incident.'
                ))
            if case.charge_notice_date > case.allegation_deadline:
                raise ValidationError(self.env._(
                    'The 30-day limit from discovery to written accusation has passed.'
                ))
            case._workflow_write({
                'state': 'investigation',
                'investigated_by': self.env.uid,
                'investigation_started_at': fields.Datetime.now(),
            })
            case.message_post(body=self.env._('HR opened the documented investigation.'))
        return True

    def action_print_charge_notice(self):
        self.ensure_one()
        self._require_hr()
        if self.state not in ('submitted', 'investigation'):
            raise UserError(self.env._(
                'The charge notice can only be printed for a submitted or active investigation.'
            ))
        if not self.charge_notice_date:
            raise ValidationError(self.env._('Enter the written charge notice date.'))
        if not self.policy_rule_reference or not self.hr_charge_statement:
            raise ValidationError(self.env._(
                'Enter the company policy or rule cited and the formal HR written allegation.'
            ))
        pdf_content = self._render_charge_notice_pdf()
        safe_reference = self.reference.replace('/', '-')
        filename = self.env._('Charge Notice - %s.pdf', safe_reference)
        attachment = self.env['ir.attachment'].sudo().create({
            'name': filename,
            'type': 'binary',
            'raw': pdf_content,
            'mimetype': 'application/pdf',
            'res_model': self._name,
            'res_id': self.id,
        })
        self.message_post(body=self.env._(
            'HR generated the written charge notice for printing and delivery.'
        ))
        return {
            'type': 'ir.actions.act_url',
            'url': f'/web/content/{attachment.id}?download=true',
            'target': 'self',
        }

    def _render_charge_notice_pdf(self):
        self.ensure_one()
        font_name = 'Helvetica'
        font_path = Path('/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf')
        if font_path.exists():
            font_name = 'RestaurantDejaVuSans'
            if font_name not in pdfmetrics.getRegisteredFontNames():
                pdfmetrics.registerFont(TTFont(font_name, str(font_path)))

        styles = getSampleStyleSheet()
        normal = ParagraphStyle(
            'RestaurantChargeNormal', parent=styles['BodyText'],
            fontName=font_name, fontSize=9.5, leading=14,
        )
        title = ParagraphStyle(
            'RestaurantChargeTitle', parent=styles['Title'],
            fontName=font_name, fontSize=16, leading=20, alignment=TA_CENTER,
            spaceAfter=8,
        )
        heading = ParagraphStyle(
            'RestaurantChargeHeading', parent=styles['Heading3'],
            fontName=font_name, fontSize=11, leading=14, spaceBefore=8,
            spaceAfter=5,
        )
        small_center = ParagraphStyle(
            'RestaurantChargeSmallCenter', parent=normal,
            alignment=TA_CENTER, fontSize=8.5,
        )

        def paragraph(value, style=normal, markup=False):
            text = str(value or '')
            if not markup:
                text = escape(text)
            text = text.replace('\n', '<br/>')
            return Paragraph(text or '&#160;', style)

        category_options = dict(
            self._fields['incident_category']._description_selection(self.env)
        )
        company = self.company_id
        partner = company.partner_id
        story = []
        story.extend([
            paragraph(company.name, title),
            paragraph(partner.contact_address or '', small_center),
            Spacer(1, 4 * mm),
            Paragraph('WRITTEN DISCIPLINARY CHARGE NOTICE', title),
            paragraph(
                f'Case: {self.reference}  |  '
                f'Notice Date: {format_date(self.env, self.charge_notice_date, date_format="dd MMMM yyyy")}',
                small_center,
            ),
            Spacer(1, 5 * mm),
        ])

        employee_data = [
            [paragraph('<b>Employee</b>', markup=True), paragraph(self.employee_id.name),
             paragraph('<b>Employee No.</b>', markup=True), paragraph(self.employee_number or '-')],
            [paragraph('<b>Branch</b>', markup=True), paragraph(self.branch_id.name),
             paragraph('<b>Position</b>', markup=True), paragraph(self.job_id.name or '-')],
            [paragraph('<b>Incident Date</b>', markup=True),
             paragraph(format_date(
                 self.env, self.incident_date, date_format='dd MMMM yyyy',
             )),
             paragraph('<b>Category</b>', markup=True),
             paragraph(category_options.get(self.incident_category, self.incident_category))],
        ]
        employee_table = Table(
            employee_data, colWidths=[28 * mm, 57 * mm, 29 * mm, 60 * mm],
            repeatRows=0,
        )
        employee_table.setStyle(TableStyle([
            ('GRID', (0, 0), (-1, -1), 0.5, colors.HexColor('#777777')),
            ('BACKGROUND', (0, 0), (0, -1), colors.HexColor('#F2F2F2')),
            ('BACKGROUND', (2, 0), (2, -1), colors.HexColor('#F2F2F2')),
            ('VALIGN', (0, 0), (-1, -1), 'TOP'),
            ('LEFTPADDING', (0, 0), (-1, -1), 5),
            ('RIGHTPADDING', (0, 0), (-1, -1), 5),
            ('TOPPADDING', (0, 0), (-1, -1), 5),
            ('BOTTOMPADDING', (0, 0), (-1, -1), 5),
        ]))
        story.extend([
            employee_table,
            Paragraph('Formal Allegation', heading),
            paragraph(self.hr_charge_statement),
            Paragraph('Incident Reported', heading),
            paragraph(
                f'<b>Summary:</b> {escape(self.allegation_summary or "")}', markup=True,
            ),
            paragraph(self.incident_details),
            Paragraph('Company Policy / Rule Cited', heading),
            paragraph(self.policy_rule_reference),
            Spacer(1, 3 * mm),
        ])

        notice_box = Table([[
            paragraph(
                '<b>This notice records an allegation and is not a final finding.</b> '
                'You have the right to provide your response and supporting evidence before '
                'HR completes the investigation.',
                markup=True,
            )
        ]], colWidths=[174 * mm])
        notice_box.setStyle(TableStyle([
            ('BOX', (0, 0), (-1, -1), 0.7, colors.HexColor('#777777')),
            ('BACKGROUND', (0, 0), (-1, -1), colors.HexColor('#F3F6F8')),
            ('LEFTPADDING', (0, 0), (-1, -1), 8),
            ('RIGHTPADDING', (0, 0), (-1, -1), 8),
            ('TOPPADDING', (0, 0), (-1, -1), 7),
            ('BOTTOMPADDING', (0, 0), (-1, -1), 7),
        ]))
        story.extend([
            notice_box,
            Paragraph('Employee Response', heading),
            Spacer(1, 18 * mm),
            HRFlowable(width='100%', thickness=0.5, color=colors.HexColor('#777777')),
            Spacer(1, 7 * mm),
            paragraph(
                'My signature confirms receipt of this written notice. It does not by itself '
                'mean that I agree with or admit the allegation.'
            ),
            Spacer(1, 18 * mm),
        ])
        signatures = Table([
            [paragraph('Employee Signature / Date', small_center),
             paragraph('HR Representative / Date', small_center)],
        ], colWidths=[82 * mm, 82 * mm], hAlign='CENTER')
        signatures.setStyle(TableStyle([
            ('LINEABOVE', (0, 0), (-1, 0), 0.8, colors.black),
            ('LEFTPADDING', (0, 0), (0, 0), 0),
            ('RIGHTPADDING', (1, 0), (1, 0), 0),
            ('VALIGN', (0, 0), (-1, -1), 'TOP'),
        ]))
        story.append(signatures)

        buffer = BytesIO()
        document = SimpleDocTemplate(
            buffer, pagesize=A4, rightMargin=18 * mm, leftMargin=18 * mm,
            topMargin=14 * mm, bottomMargin=14 * mm,
            title=f'Charge Notice - {self.reference}',
            author=company.name,
        )
        document.build(story)
        return buffer.getvalue()

    def action_print_investigation_report(self):
        self.ensure_one()
        self._require_hr()
        if self.state != 'investigation':
            raise UserError(self.env._(
                'The investigation report can only be generated during an active investigation.'
            ))
        if self.employee_response_status == 'pending':
            raise ValidationError(self.env._(
                'Record whether the employee provided, declined or could not provide a statement.'
            ))
        if self.employee_response_status == 'provided' and not self.employee_statement:
            raise ValidationError(self.env._('Record the employee statement.'))
        if not self.investigation_summary or not self.investigation_completed_date:
            raise ValidationError(self.env._(
                'Enter the investigation summary and completion date.'
            ))
        if self.violation_established == 'pending':
            raise ValidationError(self.env._(
                'Record whether the violation was established.'
            ))

        pdf_content = self._render_investigation_report_pdf()
        safe_reference = self.reference.replace('/', '-')
        filename = self.env._('Investigation Report - %s.pdf', safe_reference)
        self.write({
            'investigation_report_file': base64.b64encode(pdf_content).decode(),
            'investigation_report_filename': filename,
        })
        attachment = self.env['ir.attachment'].sudo().search([
            ('res_model', '=', self._name),
            ('res_id', '=', self.id),
            ('res_field', '=', 'investigation_report_file'),
        ], order='id desc', limit=1)
        self.message_post(body=self.env._(
            'HR generated the consolidated investigation report.'
        ))
        return {
            'type': 'ir.actions.act_url',
            'url': f'/web/content/{attachment.id}?download=true',
            'target': 'self',
        }

    def _render_investigation_report_pdf(self):
        self.ensure_one()
        font_name = 'Helvetica'
        font_path = Path('/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf')
        if font_path.exists():
            font_name = 'RestaurantDejaVuSans'
            if font_name not in pdfmetrics.getRegisteredFontNames():
                pdfmetrics.registerFont(TTFont(font_name, str(font_path)))

        styles = getSampleStyleSheet()
        normal = ParagraphStyle(
            'RestaurantInvestigationNormal', parent=styles['BodyText'],
            fontName=font_name, fontSize=9.2, leading=13.5,
        )
        title = ParagraphStyle(
            'RestaurantInvestigationTitle', parent=styles['Title'],
            fontName=font_name, fontSize=16, leading=20, alignment=TA_CENTER,
            spaceAfter=8,
        )
        heading = ParagraphStyle(
            'RestaurantInvestigationHeading', parent=styles['Heading3'],
            fontName=font_name, fontSize=11, leading=14, spaceBefore=7,
            spaceAfter=4,
        )
        small_center = ParagraphStyle(
            'RestaurantInvestigationSmallCenter', parent=normal,
            alignment=TA_CENTER, fontSize=8.5,
        )

        def paragraph(value, style=normal, markup=False):
            text = str(value or '')
            if not markup:
                text = escape(text)
            text = text.replace('\n', '<br/>')
            return Paragraph(text or '&#160;', style)

        company = self.company_id
        partner = company.partner_id
        category_options = dict(
            self._fields['incident_category']._description_selection(self.env)
        )
        response_options = dict(
            self._fields['employee_response_status']._description_selection(self.env)
        )
        finding_options = dict(
            self._fields['violation_established']._description_selection(self.env)
        )
        prepared_by = self.investigated_by.name or self.env.user.name
        story = [
            paragraph(company.name, title),
            paragraph(partner.contact_address or '', small_center),
            Spacer(1, 4 * mm),
            Paragraph('HR INVESTIGATION REPORT', title),
            paragraph(
                f'Case: {self.reference}  |  '
                f'Completion Date: {format_date(self.env, self.investigation_completed_date, date_format="dd MMMM yyyy")}',
                small_center,
            ),
            Spacer(1, 5 * mm),
        ]

        employee_data = [
            [paragraph('<b>Employee</b>', markup=True), paragraph(self.employee_id.name),
             paragraph('<b>Employee No.</b>', markup=True), paragraph(self.employee_number or '-')],
            [paragraph('<b>Branch</b>', markup=True), paragraph(self.branch_id.name),
             paragraph('<b>Position</b>', markup=True), paragraph(self.job_id.name or '-')],
            [paragraph('<b>Incident Date</b>', markup=True),
             paragraph(format_date(self.env, self.incident_date, date_format='dd MMMM yyyy')),
             paragraph('<b>Category</b>', markup=True),
             paragraph(category_options.get(self.incident_category, self.incident_category))],
        ]
        employee_table = Table(
            employee_data, colWidths=[28 * mm, 57 * mm, 29 * mm, 60 * mm],
        )
        employee_table.setStyle(TableStyle([
            ('GRID', (0, 0), (-1, -1), 0.5, colors.HexColor('#777777')),
            ('BACKGROUND', (0, 0), (0, -1), colors.HexColor('#F2F2F2')),
            ('BACKGROUND', (2, 0), (2, -1), colors.HexColor('#F2F2F2')),
            ('VALIGN', (0, 0), (-1, -1), 'TOP'),
            ('LEFTPADDING', (0, 0), (-1, -1), 5),
            ('RIGHTPADDING', (0, 0), (-1, -1), 5),
            ('TOPPADDING', (0, 0), (-1, -1), 5),
            ('BOTTOMPADDING', (0, 0), (-1, -1), 5),
        ]))
        story.extend([
            employee_table,
            Paragraph('Allegation Reviewed', heading),
            paragraph(self.hr_charge_statement or self.allegation_summary),
            Paragraph('Company Policy / Rule', heading),
            paragraph(self.policy_rule_reference),
            Paragraph('Evidence Reviewed', heading),
            paragraph(
                f'<b>Manager report:</b> {escape(self.incident_details or "-")}<br/>'
                f'<b>Witnesses:</b> {escape(self.witnesses or "None recorded")}<br/>'
                f'<b>Evidence attachment:</b> {"Attached to the case" if self.evidence_file else "None recorded"}',
                markup=True,
            ),
            Paragraph('Employee Response', heading),
            paragraph(
                f'<b>Status:</b> {escape(response_options.get(self.employee_response_status, self.employee_response_status))}<br/>'
                f'<b>Statement:</b> {escape(self.employee_statement or "No statement recorded")}',
                markup=True,
            ),
            Paragraph('Investigation Summary', heading),
            paragraph(self.investigation_summary),
            Spacer(1, 3 * mm),
        ])

        finding_box = Table([[
            paragraph(
                f'<b>Finding: {escape(finding_options.get(self.violation_established, self.violation_established))}</b><br/>'
                'This report records the evidence reviewed, the employee response and the HR '
                'finding. Any disciplinary decision is documented separately.',
                markup=True,
            )
        ]], colWidths=[174 * mm])
        finding_box.setStyle(TableStyle([
            ('BOX', (0, 0), (-1, -1), 0.7, colors.HexColor('#777777')),
            ('BACKGROUND', (0, 0), (-1, -1), colors.HexColor('#F3F6F8')),
            ('LEFTPADDING', (0, 0), (-1, -1), 8),
            ('RIGHTPADDING', (0, 0), (-1, -1), 8),
            ('TOPPADDING', (0, 0), (-1, -1), 7),
            ('BOTTOMPADDING', (0, 0), (-1, -1), 7),
        ]))
        story.extend([
            finding_box,
            Spacer(1, 18 * mm),
        ])
        signatures = Table([
            [paragraph(f'Prepared by: {prepared_by}', small_center),
             paragraph('HR Signature / Date', small_center)],
        ], colWidths=[82 * mm, 82 * mm], hAlign='CENTER')
        signatures.setStyle(TableStyle([
            ('LINEABOVE', (0, 0), (-1, 0), 0.8, colors.black),
            ('LEFTPADDING', (0, 0), (0, 0), 0),
            ('RIGHTPADDING', (1, 0), (1, 0), 0),
            ('VALIGN', (0, 0), (-1, -1), 'TOP'),
        ]))
        story.append(signatures)

        buffer = BytesIO()
        document = SimpleDocTemplate(
            buffer, pagesize=A4, rightMargin=18 * mm, leftMargin=18 * mm,
            topMargin=14 * mm, bottomMargin=14 * mm,
            title=f'Investigation Report - {self.reference}',
            author=company.name,
        )
        document.build(story)
        return buffer.getvalue()

    def action_print_written_warning(self):
        self.ensure_one()
        self._require_hr()
        if self.state != 'decision_pending':
            raise UserError(self.env._(
                'A written warning can only be printed while the case is awaiting an HR decision.'
            ))
        if self.decision_type != 'written_warning':
            raise ValidationError(self.env._('Choose Written Warning as the single sanction.'))
        if not self.decision_notice_date:
            raise ValidationError(self.env._('Enter the written warning date.'))
        if not self.decision_reason or not self.repeat_consequence:
            raise ValidationError(self.env._(
                'Enter the decision reason and the consequence of a repeated violation.'
            ))
        if not self.policy_rule_reference:
            raise ValidationError(self.env._('Enter the company policy or rule cited.'))

        pdf_content = self._render_written_warning_pdf()
        safe_reference = self.reference.replace('/', '-')
        filename = self.env._('Written Warning - %s.pdf', safe_reference)
        self.write({
            'decision_notice_file': base64.b64encode(pdf_content).decode(),
            'decision_notice_filename': filename,
        })
        attachment = self.env['ir.attachment'].sudo().search([
            ('res_model', '=', self._name),
            ('res_id', '=', self.id),
            ('res_field', '=', 'decision_notice_file'),
        ], order='id desc', limit=1)
        self.message_post(body=self.env._(
            'HR generated the written warning for delivery to the employee.'
        ))
        return {
            'type': 'ir.actions.act_url',
            'url': f'/web/content/{attachment.id}?download=true',
            'target': 'self',
        }

    def action_download_decision_notice(self):
        self.ensure_one()
        self._require_hr()
        if not self.decision_notice_file:
            raise UserError(self.env._('No written decision letter is available for download.'))
        attachment = self.env['ir.attachment'].sudo().search([
            ('res_model', '=', self._name),
            ('res_id', '=', self.id),
            ('res_field', '=', 'decision_notice_file'),
        ], order='id desc', limit=1)
        if not attachment:
            raise UserError(self.env._('The written decision attachment could not be found.'))
        return {
            'type': 'ir.actions.act_url',
            'url': f'/web/content/{attachment.id}?download=true',
            'target': 'self',
        }

    def _render_written_warning_pdf(self):
        self.ensure_one()
        font_name = 'Helvetica'
        font_path = Path('/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf')
        if font_path.exists():
            font_name = 'RestaurantDejaVuSans'
            if font_name not in pdfmetrics.getRegisteredFontNames():
                pdfmetrics.registerFont(TTFont(font_name, str(font_path)))

        styles = getSampleStyleSheet()
        normal = ParagraphStyle(
            'RestaurantWarningNormal', parent=styles['BodyText'],
            fontName=font_name, fontSize=9.5, leading=14,
        )
        title = ParagraphStyle(
            'RestaurantWarningTitle', parent=styles['Title'],
            fontName=font_name, fontSize=16, leading=20, alignment=TA_CENTER,
            spaceAfter=8,
        )
        heading = ParagraphStyle(
            'RestaurantWarningHeading', parent=styles['Heading3'],
            fontName=font_name, fontSize=11, leading=14, spaceBefore=8,
            spaceAfter=5,
        )
        small_center = ParagraphStyle(
            'RestaurantWarningSmallCenter', parent=normal,
            alignment=TA_CENTER, fontSize=8.5,
        )

        def paragraph(value, style=normal, markup=False):
            text = str(value or '')
            if not markup:
                text = escape(text)
            text = text.replace('\n', '<br/>')
            return Paragraph(text or '&#160;', style)

        company = self.company_id
        partner = company.partner_id
        story = [
            paragraph(company.name, title),
            paragraph(partner.contact_address or '', small_center),
            Spacer(1, 4 * mm),
            Paragraph('WRITTEN WARNING', title),
            paragraph(
                f'Case: {self.reference}  |  '
                f'Warning Date: {format_date(self.env, self.decision_notice_date, date_format="dd MMMM yyyy")}',
                small_center,
            ),
            Spacer(1, 5 * mm),
        ]

        employee_data = [
            [paragraph('<b>Employee</b>', markup=True), paragraph(self.employee_id.name),
             paragraph('<b>Employee No.</b>', markup=True), paragraph(self.employee_number or '-')],
            [paragraph('<b>Branch</b>', markup=True), paragraph(self.branch_id.name),
             paragraph('<b>Position</b>', markup=True), paragraph(self.job_id.name or '-')],
            [paragraph('<b>Incident Date</b>', markup=True),
             paragraph(format_date(
                 self.env, self.incident_date, date_format='dd MMMM yyyy',
             )),
             paragraph('<b>Decision</b>', markup=True), paragraph('Written Warning')],
        ]
        employee_table = Table(
            employee_data, colWidths=[28 * mm, 57 * mm, 29 * mm, 60 * mm],
        )
        employee_table.setStyle(TableStyle([
            ('GRID', (0, 0), (-1, -1), 0.5, colors.HexColor('#777777')),
            ('BACKGROUND', (0, 0), (0, -1), colors.HexColor('#F2F2F2')),
            ('BACKGROUND', (2, 0), (2, -1), colors.HexColor('#F2F2F2')),
            ('VALIGN', (0, 0), (-1, -1), 'TOP'),
            ('LEFTPADDING', (0, 0), (-1, -1), 5),
            ('RIGHTPADDING', (0, 0), (-1, -1), 5),
            ('TOPPADDING', (0, 0), (-1, -1), 5),
            ('BOTTOMPADDING', (0, 0), (-1, -1), 5),
        ]))
        story.extend([
            employee_table,
            Paragraph('Finding After Investigation', heading),
            paragraph(self.decision_reason),
            Paragraph('Company Policy / Rule', heading),
            paragraph(self.policy_rule_reference),
            Paragraph('Required Standard', heading),
            paragraph(
                'The employee is required to comply with the cited company policy and the '
                'reasonable instructions applicable to the role and workplace.'
            ),
            Paragraph('Consequence of Repetition', heading),
            paragraph(self.repeat_consequence),
            Spacer(1, 3 * mm),
        ])

        warning_box = Table([[
            paragraph(
                '<b>This document is the written disciplinary decision issued after HR '
                'completed the investigation.</b> The employee may submit a grievance through '
                'the company process. Signing below confirms receipt only; it does not by '
                'itself mean agreement with the finding or sanction.',
                markup=True,
            )
        ]], colWidths=[174 * mm])
        warning_box.setStyle(TableStyle([
            ('BOX', (0, 0), (-1, -1), 0.7, colors.HexColor('#777777')),
            ('BACKGROUND', (0, 0), (-1, -1), colors.HexColor('#F3F6F8')),
            ('LEFTPADDING', (0, 0), (-1, -1), 8),
            ('RIGHTPADDING', (0, 0), (-1, -1), 8),
            ('TOPPADDING', (0, 0), (-1, -1), 7),
            ('BOTTOMPADDING', (0, 0), (-1, -1), 7),
        ]))
        story.extend([
            warning_box,
            Spacer(1, 19 * mm),
        ])
        signatures = Table([
            [paragraph('Employee Signature / Date', small_center),
             paragraph('HR Representative / Date', small_center)],
        ], colWidths=[82 * mm, 82 * mm], hAlign='CENTER')
        signatures.setStyle(TableStyle([
            ('LINEABOVE', (0, 0), (-1, 0), 0.8, colors.black),
            ('LEFTPADDING', (0, 0), (0, 0), 0),
            ('RIGHTPADDING', (1, 0), (1, 0), 0),
            ('VALIGN', (0, 0), (-1, -1), 'TOP'),
        ]))
        story.append(signatures)

        buffer = BytesIO()
        document = SimpleDocTemplate(
            buffer, pagesize=A4, rightMargin=18 * mm, leftMargin=18 * mm,
            topMargin=14 * mm, bottomMargin=14 * mm,
            title=f'Written Warning - {self.reference}',
            author=company.name,
        )
        document.build(story)
        return buffer.getvalue()

    def action_complete_investigation(self):
        for case in self:
            case._require_hr()
            if case.state != 'investigation':
                raise UserError(self.env._('Only an active investigation can be completed.'))
            if case.employee_response_status == 'pending':
                raise ValidationError(self.env._(
                    'Record whether the employee provided or declined a statement.'
                ))
            if case.employee_response_status == 'provided' and not case.employee_statement:
                raise ValidationError(self.env._('Record the employee statement.'))
            if not case.investigation_summary or not case.investigation_report_file:
                raise ValidationError(self.env._(
                    'Enter the investigation summary and upload the investigation report.'
                ))
            if not case.investigation_completed_date:
                raise ValidationError(self.env._('Enter the investigation completion date.'))
            if case.investigation_completed_date < case.charge_notice_date:
                raise ValidationError(self.env._(
                    'Investigation completion cannot predate the written charge notice.'
                ))
            if case.violation_established == 'pending':
                raise ValidationError(self.env._(
                    'Record whether the violation was established.'
                ))
            values = {
                'state': (
                    'decision_pending'
                    if case.violation_established == 'yes' else 'dismissed'
                ),
                'investigation_completed_at': fields.Datetime.now(),
            }
            if case.violation_established == 'no':
                values.update({
                    'closed_by': self.env.uid,
                    'closed_at': fields.Datetime.now(),
                })
            case._workflow_write(values)
            case.message_post(body=self.env._(
                'Investigation completed: %s.',
                'violation established'
                if case.violation_established == 'yes' else 'case dismissed',
            ))
        return True

    def _validate_sanction(self):
        self.ensure_one()
        if self.decision_type == 'wage_deduction' and not 1 <= self.sanction_days <= 5:
            raise ValidationError(self.env._(
                'Enter wage deduction days from 1 to 5. No payroll deduction is automated.'
            ))
        if self.decision_type == 'unpaid_suspension' and not 1 <= self.sanction_days <= 14:
            raise ValidationError(self.env._(
                'Unpaid suspension must be from 1 to 14 days.'
            ))
        if self.decision_type == 'bonus_deprivation' and not 1 <= self.sanction_months <= 12:
            raise ValidationError(self.env._(
                'Periodic bonus deprivation must be from 1 to 12 months.'
            ))
        if self.decision_type == 'promotion_deprivation' and not 1 <= self.sanction_months <= 24:
            raise ValidationError(self.env._(
                'Promotion deprivation must be from 1 to 24 months.'
            ))

    def action_issue_decision(self):
        for case in self:
            case._require_hr()
            if case.state != 'decision_pending':
                raise UserError(self.env._('This case is not awaiting an HR decision.'))
            if not case.decision_type or not case.decision_reason:
                raise ValidationError(self.env._('Choose one sanction and enter its reason.'))
            if not case.repeat_consequence:
                raise ValidationError(self.env._(
                    'Record the consequence if the violation is repeated.'
                ))
            if not case.decision_notice_date or not case.decision_notice_file:
                raise ValidationError(self.env._(
                    'Record the written decision notice date and upload its copy.'
                ))
            if case.decision_notice_date < case.investigation_completed_date:
                raise ValidationError(self.env._(
                    'The decision notice cannot predate investigation completion.'
                ))
            if case.decision_notice_date > case.decision_deadline:
                raise ValidationError(self.env._(
                    'The 60-day limit after investigation completion has passed.'
                ))
            case._validate_sanction()
            case._workflow_write({
                'state': 'issued',
                'decided_by': self.env.uid,
                'decided_at': fields.Datetime.now(),
            })
            case.message_post(body=self.env._('HR issued the documented disciplinary decision.'))
        return True

    def action_close_after_notification(self):
        for case in self:
            case._require_hr()
            if case.state != 'issued':
                raise UserError(self.env._('Only an issued decision can be closed.'))
            if case.acknowledgement_status == 'pending' or not case.acknowledgement_date:
                raise ValidationError(self.env._(
                    'Record the employee acknowledgement or delivery result and date.'
                ))
            if not case.acknowledgement_file:
                raise ValidationError(self.env._(
                    'Upload the acknowledgement or proof of written delivery.'
                ))
            case._workflow_write({
                'state': 'closed',
                'closed_by': self.env.uid,
                'closed_at': fields.Datetime.now(),
            })
        return True

    def action_register_grievance(self):
        for case in self:
            case._require_hr()
            if case.state not in ('issued', 'closed'):
                raise UserError(self.env._(
                    'A grievance can be registered after a disciplinary decision is issued.'
                ))
            if (
                not case.grievance_date
                or not case.grievance_text
                or not case.grievance_file
            ):
                raise ValidationError(self.env._(
                    'Enter the grievance date and upload the employee grievance.'
                ))
            case._workflow_write({
                'state': 'grievance',
                'closed_by': False,
                'closed_at': False,
            })
        return True

    def action_resolve_grievance(self):
        for case in self:
            case._require_hr()
            if case.state != 'grievance':
                raise UserError(self.env._('This case is not under grievance review.'))
            if (
                not case.grievance_outcome
                or not case.grievance_outcome_note
                or not case.grievance_outcome_date
                or not case.grievance_outcome_file
            ):
                raise ValidationError(self.env._(
                    'Record and upload the written grievance outcome, explanation and date.'
                ))
            case._workflow_write({
                'state': 'closed',
                'grievance_resolved_by': self.env.uid,
                'closed_by': self.env.uid,
                'closed_at': fields.Datetime.now(),
            })
        return True

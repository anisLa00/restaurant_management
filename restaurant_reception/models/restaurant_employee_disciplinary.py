from datetime import timedelta

from odoo import api, fields, models
from odoo.exceptions import AccessError, UserError, ValidationError

from .reception_security import HR_GROUP, MANAGER_GROUP, require_assigned_branches


MANAGER_EDITABLE_FIELDS = {
    'employee_id', 'branch_id', 'incident_date', 'discovered_date',
    'incident_category', 'severity', 'allegation_summary', 'incident_details',
    'witnesses', 'evidence_file', 'evidence_filename',
}
HR_EDITABLE_FIELDS = MANAGER_EDITABLE_FIELDS | {
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

import logging

from odoo import api, fields, models
from odoo.exceptions import AccessError, UserError, ValidationError

from .reception_security import (
    HR_GROUP, MANAGER_GROUP, check_employee_company, lock_records,
    require_assigned_branches, require_draft, require_role,
)


_logger = logging.getLogger(__name__)


class RestaurantLeaveRequest(models.Model):
    _name = 'restaurant.leave.request'
    _description = 'Restaurant Employee Leave Request'
    _inherit = ['mail.thread', 'mail.activity.mixin']
    _rec_name = 'reference'
    _order = 'request_date_from desc, id desc'
    _mail_post_access = 'read'

    reference = fields.Char(
        required=True, readonly=True, copy=False, default='New', index=True,
    )
    employee_id = fields.Many2one(
        'hr.employee', required=True, ondelete='restrict', index=True, tracking=True,
    )
    branch_id = fields.Many2one(
        'restaurant.branch', required=True, ondelete='restrict', index=True, tracking=True,
    )
    company_id = fields.Many2one(
        related='branch_id.company_id', store=True, index=True,
    )
    leave_kind = fields.Selection([
        ('annual_leave', 'Annual Leave'),
        ('sick_leave', 'Sick Leave'),
        ('emergency_leave', 'Emergency Leave'),
    ], required=True, default='annual_leave', index=True, tracking=True)
    request_date_from = fields.Date(required=True, index=True, tracking=True)
    request_date_to = fields.Date(required=True, index=True, tracking=True)
    reason = fields.Text(required=True, tracking=True)
    request_document = fields.Binary(
        string='Signed Leave Form', attachment=True,
        help='Scanned or photographed leave form signed by the employee.',
    )
    request_document_name = fields.Char(string='Document Name')
    manager_note = fields.Text(tracking=True)
    hr_note = fields.Text(tracking=True)
    decision_reason = fields.Text(
        tracking=True,
        help='Required when HR returns or rejects the request.',
    )
    last_hr_return_reason = fields.Text(readonly=True, copy=False, tracking=True)
    official_leave_type_id = fields.Many2one(
        'hr.work.entry.type', string='Official Leave Type', copy=False, tracking=True,
        ondelete='restrict', domain="[('time_off_selectable', '=', True)]",
        help='Optional HR override. If empty, the company mapping is used.',
    )
    entered_by = fields.Many2one(
        'res.users', required=True, readonly=True, default=lambda self: self.env.user,
    )
    manager_approved_by = fields.Many2one(
        'res.users', readonly=True, copy=False, tracking=True,
    )
    manager_approved_at = fields.Datetime(readonly=True, copy=False, tracking=True)
    hr_reviewed_by = fields.Many2one(
        'res.users', readonly=True, copy=False, tracking=True,
    )
    hr_reviewed_at = fields.Datetime(readonly=True, copy=False, tracking=True)
    hr_returned_by = fields.Many2one(
        'res.users', readonly=True, copy=False, tracking=True,
    )
    hr_returned_at = fields.Datetime(readonly=True, copy=False, tracking=True)
    hr_return_count = fields.Integer(readonly=True, copy=False, tracking=True)
    state = fields.Selection([
        ('draft', 'Manager Draft'),
        ('submitted_to_hr', 'Submitted to HR'),
        ('approved', 'Approved'),
        ('rejected', 'Rejected'),
    ], required=True, default='draft', readonly=True, copy=False, index=True, tracking=True)
    sync_status = fields.Selection([
        ('pending', 'Pending'),
        ('synced', 'Synced'),
        ('error', 'Sync Error'),
    ], required=True, default='pending', readonly=True, copy=False, index=True, tracking=True)
    sync_error = fields.Text(readonly=True, copy=False, tracking=True)
    sync_attempt_count = fields.Integer(readonly=True, copy=False, tracking=True)
    last_sync_attempt_at = fields.Datetime(readonly=True, copy=False, tracking=True)
    last_sync_attempt_by = fields.Many2one(
        'res.users', readonly=True, copy=False, tracking=True,
    )
    official_leave_id = fields.Many2one(
        'hr.leave', readonly=True, copy=False, ondelete='restrict', tracking=True,
    )
    can_review_hr = fields.Boolean(compute='_compute_can_review_hr')

    _reference_unique = models.Constraint(
        'UNIQUE(reference)', 'The leave request reference must be unique.',
    )

    @api.depends('manager_approved_by')
    @api.depends_context('uid')
    def _compute_can_review_hr(self):
        is_hr = self.env.user.has_group(HR_GROUP)
        for request in self:
            request.can_review_hr = (
                is_hr and request.manager_approved_by != self.env.user
            )

    @api.constrains('employee_id', 'branch_id')
    def _check_employee_company(self):
        check_employee_company(self)

    @api.constrains('request_date_from', 'request_date_to')
    def _check_request_dates(self):
        for request in self:
            if (
                request.request_date_from
                and request.request_date_to
                and request.request_date_to < request.request_date_from
            ):
                raise ValidationError(self.env._(
                    'The leave end date cannot be before its start date.'
                ))

    @api.constrains('official_leave_type_id', 'company_id')
    def _check_official_leave_type(self):
        for request in self:
            leave_type = request.official_leave_type_id
            if not leave_type:
                continue
            if not leave_type.time_off_selectable:
                raise ValidationError(self.env._(
                    'The official leave type must be selectable in Time Off.'
                ))
            if leave_type.country_id and leave_type.country_id != request.company_id.country_id:
                raise ValidationError(self.env._(
                    'The official leave type must match the request company country.'
                ))

    @api.model_create_multi
    def create(self, vals_list):
        require_role(self.env, MANAGER_GROUP)
        editable = {
            'reference', 'employee_id', 'branch_id', 'leave_kind',
            'request_date_from', 'request_date_to', 'reason', 'request_document',
            'request_document_name', 'manager_note', 'state',
        }
        prepared = []
        for values in vals_list:
            values = dict(values)
            if values.get('state', 'draft') != 'draft' or set(values) - editable:
                raise AccessError(self.env._(
                    'A leave request must start as a manager draft without HR fields.'
                ))
            if values.get('reference', 'New') == 'New':
                values['reference'] = self.env['ir.sequence'].next_by_code(
                    'restaurant.leave.request'
                ) or 'New'
            values.update({
                'state': 'draft',
                'sync_status': 'pending',
                'entered_by': self.env.uid,
                'manager_approved_by': False,
                'manager_approved_at': False,
                'hr_reviewed_by': False,
                'hr_reviewed_at': False,
                'hr_note': False,
                'decision_reason': False,
                'official_leave_type_id': False,
            })
            prepared.append(values)
        records = super().create(prepared)
        require_assigned_branches(records.branch_id)
        return records

    def write(self, vals):
        lock_records(self)
        if 'state' in vals:
            raise AccessError(self.env._(
                'Use the leave workflow actions to change state.'
            ))

        fields_to_write = set(vals)
        manager_editable = {
            'employee_id', 'branch_id', 'leave_kind', 'request_date_from',
            'request_date_to', 'reason', 'request_document',
            'request_document_name', 'manager_note',
        }
        hr_editable = {'hr_note', 'decision_reason', 'official_leave_type_id'}

        if fields_to_write and fields_to_write <= manager_editable:
            require_role(self.env, MANAGER_GROUP)
            require_draft(self)
            require_assigned_branches(self.branch_id)
            if 'branch_id' in vals:
                require_assigned_branches(
                    self.env['restaurant.branch'].browse(vals['branch_id'])
                )
            return super().write(vals)

        if fields_to_write and fields_to_write <= hr_editable:
            self._require_hr_reviewer()
            if any(request.state != 'submitted_to_hr' for request in self):
                raise AccessError(self.env._(
                    'HR fields can only be edited while the request awaits HR.'
                ))
            return super().write(vals)

        raise AccessError(self.env._(
            'Approval, audit, link, and sync fields cannot be modified manually.'
        ))

    def unlink(self):
        lock_records(self, 'unlink')
        require_role(self.env, MANAGER_GROUP)
        require_draft(self)
        require_assigned_branches(self.branch_id)
        return super().unlink()

    def _require_hr_reviewer(self):
        require_role(self.env, HR_GROUP)
        if not self.env.su and any(
            request.manager_approved_by == self.env.user for request in self
        ):
            raise AccessError(self.env._(
                'You cannot make the HR decision on a leave request you approved as manager.'
            ))

    def _transition(self, target, extra_values=None):
        self.ensure_one()
        source = self.state
        transitions = {
            ('draft', 'submitted_to_hr'): MANAGER_GROUP,
            ('submitted_to_hr', 'draft'): HR_GROUP,
            ('submitted_to_hr', 'approved'): HR_GROUP,
            ('submitted_to_hr', 'rejected'): HR_GROUP,
        }
        group = transitions.get((source, target))
        if not group:
            raise UserError(self.env._('This leave workflow transition is not allowed.'))
        if group == HR_GROUP:
            self._require_hr_reviewer()
        else:
            require_role(self.env, group)
            require_assigned_branches(self.branch_id)

        now = fields.Datetime.now()
        values = {'state': target, **(extra_values or {})}
        if target == 'submitted_to_hr':
            values.update({
                'manager_approved_by': self.env.uid,
                'manager_approved_at': now,
                'decision_reason': False,
                'sync_status': 'pending',
                'sync_error': False,
            })
        elif target in ('approved', 'rejected'):
            values.update({
                'hr_reviewed_by': self.env.uid,
                'hr_reviewed_at': now,
            })
        elif source == 'submitted_to_hr' and target == 'draft':
            values.update({
                'hr_returned_by': self.env.uid,
                'hr_returned_at': now,
                'hr_return_count': self.hr_return_count + 1,
                'last_hr_return_reason': self.decision_reason,
                'sync_status': 'pending',
                'sync_error': False,
            })

        result = super(RestaurantLeaveRequest, self).write(values)
        label = dict(self._fields['state']._description_selection(self.env))[target]
        self.sudo().message_post(
            body=self.env._(
                'Leave workflow changed to %(state)s by %(user)s.',
                state=label,
                user=self.env.user.name,
            ),
            author_id=self.env.user.partner_id.id,
        )
        return result

    def action_submit_to_hr(self):
        self.ensure_one()
        lock_records(self)
        if not self.request_document:
            raise ValidationError(self.env._(
                'Attach the employee\'s signed leave form before sending the request to HR.'
            ))
        return self._transition('submitted_to_hr')

    def action_return_to_manager(self):
        self.ensure_one()
        lock_records(self)
        self._require_hr_reviewer()
        if self.state != 'submitted_to_hr':
            raise UserError(self.env._('Only a request awaiting HR can be returned.'))
        if not (self.decision_reason or '').strip():
            raise ValidationError(self.env._(
                'Enter a decision reason before returning the request to the manager.'
            ))
        return self._transition('draft')

    def action_reject(self):
        self.ensure_one()
        lock_records(self)
        self._require_hr_reviewer()
        if self.state != 'submitted_to_hr':
            raise UserError(self.env._('Only a request awaiting HR can be rejected.'))
        if not (self.decision_reason or '').strip():
            raise ValidationError(self.env._(
                'Enter a decision reason before rejecting the leave request.'
            ))
        return self._transition('rejected', {
            'sync_status': 'pending',
            'sync_error': False,
        })

    def action_approve(self):
        self.ensure_one()
        return self._attempt_approval_sync(retry=False)

    def action_retry_sync(self):
        self.ensure_one()
        if self.sync_status != 'error':
            raise UserError(self.env._('Only a failed leave sync can be retried.'))
        return self._attempt_approval_sync(retry=True)

    def _attempt_approval_sync(self, retry=False):
        self.ensure_one()
        lock_records(self)
        self._require_hr_reviewer()
        if self.state != 'submitted_to_hr':
            raise UserError(self.env._('Only a request awaiting HR can be approved.'))
        if retry and self.sync_status != 'error':
            raise UserError(self.env._('Only a failed leave sync can be retried.'))

        super(RestaurantLeaveRequest, self).write({
            'sync_attempt_count': self.sync_attempt_count + 1,
            'last_sync_attempt_at': fields.Datetime.now(),
            'last_sync_attempt_by': self.env.uid,
            'sync_status': 'pending',
            'sync_error': False,
        })

        try:
            with self.env.cr.savepoint():
                self._sync_official_leave()
                self._transition('approved', {
                    'sync_status': 'synced',
                    'sync_error': False,
                })
        except Exception as error:  # noqa: BLE001 - failure must remain retryable
            _logger.exception('Restaurant leave sync failed for request %s', self.id)
            self.invalidate_recordset()
            error_message = str(error)[:2000] or error.__class__.__name__
            super(RestaurantLeaveRequest, self).write({
                'sync_status': 'error',
                'sync_error': error_message,
            })
            self.sudo().message_post(
                body=self.env._('Official Time Off sync failed: %s', error_message),
                author_id=self.env.user.partner_id.id,
            )
            return {
                'type': 'ir.actions.client',
                'tag': 'display_notification',
                'params': {
                    'title': self.env._('Leave Sync Failed'),
                    'message': error_message,
                    'type': 'danger',
                    'sticky': True,
                },
            }

        return {
            'type': 'ir.actions.client',
            'tag': 'display_notification',
            'params': {
                'title': self.env._('Leave Approved'),
                'message': self.env._(
                    'The official Time Off request was created and approved.'
                ),
                'type': 'success',
                'sticky': False,
            },
        }

    def _mapped_leave_type(self):
        self.ensure_one()
        mapping = {
            'annual_leave': self.company_id.restaurant_annual_leave_type_id,
            'sick_leave': self.company_id.restaurant_sick_leave_type_id,
            'emergency_leave': self.company_id.restaurant_emergency_leave_type_id,
        }
        return (self.official_leave_type_id or mapping.get(self.leave_kind)).sudo()

    def _sync_official_leave(self):
        self.ensure_one()
        if not self.company_id.restaurant_hr_official_sync_enabled:
            raise ValidationError(self.env._(
                'Official Restaurant HR sync is disabled for %(company)s.',
                company=self.company_id.display_name,
            ))
        leave_type = self._mapped_leave_type()
        if not leave_type:
            raise ValidationError(self.env._(
                'Configure the %(kind)s Time Off type for %(company)s or select '
                'an official leave type on this request.',
                kind=dict(
                    self._fields['leave_kind']._description_selection(self.env)
                )[self.leave_kind],
                company=self.company_id.display_name,
            ))
        if not leave_type.time_off_selectable:
            raise ValidationError(self.env._(
                'The configured leave type is not selectable in Time Off.'
            ))
        if leave_type.country_id and leave_type.country_id != self.company_id.country_id:
            raise ValidationError(self.env._(
                'The configured leave type does not match the request company country.'
            ))

        leave_model = self.env['hr.leave'].sudo().with_company(
            self.company_id
        ).with_context(
            restaurant_hr_sync=True,
            leave_fast_create=True,
        )
        leave = self.official_leave_id.sudo().exists()
        if not leave:
            leave = leave_model.search([
                ('restaurant_leave_request_id', '=', self.id),
            ], limit=1)
        values = {
            'employee_id': self.employee_id.id,
            'work_entry_type_id': leave_type.id,
            'request_date_from': self.request_date_from,
            'request_date_to': self.request_date_to,
            'notes': self.hr_note or self.reason,
            'restaurant_leave_request_id': self.id,
        }
        if leave:
            if leave.employee_id != self.employee_id:
                raise ValidationError(self.env._(
                    'The linked Time Off request belongs to another employee.'
                ))
        else:
            leave = leave_model.create(values)
            if not leave:
                raise ValidationError(self.env._(
                    'Odoo could not create a valid Time Off request. Check allocations '
                    'and the employee work schedule.'
                ))
        if leave.state in ('confirm', 'validate1'):
            leave.sudo().with_context(
                restaurant_hr_sync=True,
                leave_fast_create=True,
            )._action_validate(check_state=False)
        if leave.state != 'validate':
            raise ValidationError(self.env._(
                'The official Time Off request could not be approved.'
            ))
        super(RestaurantLeaveRequest, self).write({
            'official_leave_type_id': leave_type.id,
            'official_leave_id': leave.id,
        })

    def action_open_official_leave(self):
        self.ensure_one()
        if not self.official_leave_id:
            raise UserError(self.env._('No official Time Off request is linked.'))
        return {
            'type': 'ir.actions.act_window',
            'name': self.env._('Time Off Request'),
            'res_model': 'hr.leave',
            'res_id': self.official_leave_id.id,
            'view_mode': 'form',
        }

from datetime import timedelta

from dateutil.relativedelta import relativedelta

from odoo import api, fields, models
from odoo.exceptions import AccessError, UserError, ValidationError

from .reception_security import MANAGER_GROUP, lock_records, require_assigned_branches, require_role


SHIFT_SELECTION = [
    ('morning', 'Morning'),
    ('evening', 'Evening'),
    ('one_shift', 'One Shift'),
]


class RestaurantShiftRoster(models.Model):
    _name = 'restaurant.shift.roster'
    _description = 'Restaurant Monthly Shift Roster'
    _inherit = ['mail.thread', 'mail.activity.mixin']
    _order = 'month_start desc, branch_id, id desc'
    _mail_post_access = 'read'

    name = fields.Char(compute='_compute_period', store=True)
    branch_id = fields.Many2one(
        'restaurant.branch', required=True, ondelete='restrict', index=True,
        tracking=True,
    )
    company_id = fields.Many2one(
        related='branch_id.company_id', store=True, index=True,
    )
    month_start = fields.Date(
        required=True, default=lambda self: self._default_month_start(),
        index=True, tracking=True,
    )
    month_end = fields.Date(compute='_compute_period', store=True, index=True)
    line_ids = fields.One2many(
        'restaurant.shift.roster.line', 'roster_id', string='Staff Shifts', copy=False,
    )
    state = fields.Selection([
        ('draft', 'Draft'),
        ('published', 'Published'),
    ], required=True, default='draft', readonly=True, copy=False, index=True,
        tracking=True)
    published_by = fields.Many2one('res.users', readonly=True, copy=False)
    published_at = fields.Datetime(readonly=True, copy=False)

    _branch_month_unique = models.Constraint(
        'UNIQUE(branch_id, month_start)',
        'Only one shift roster is allowed per branch and month.',
    )

    @api.model
    def _default_month_start(self):
        today = fields.Date.context_today(self)
        return today.replace(day=1)

    @api.depends('branch_id', 'month_start')
    def _compute_period(self):
        for roster in self:
            roster.month_end = (
                roster.month_start + relativedelta(months=1) - timedelta(days=1)
                if roster.month_start else False
            )
            roster.name = (
                f'{roster.branch_id.display_name} - {roster.month_start:%B %Y}'
                if roster.branch_id and roster.month_start
                else self.env._('New Shift Roster')
            )

    @api.constrains('month_start')
    def _check_month_start(self):
        for roster in self:
            if roster.month_start and roster.month_start.day != 1:
                raise ValidationError(self.env._('The roster month must start on day one.'))

    @api.model_create_multi
    def create(self, vals_list):
        require_role(self.env, MANAGER_GROUP)
        prepared = []
        for vals in vals_list:
            values = dict(vals)
            if values.get('state', 'draft') != 'draft':
                raise AccessError(self.env._('Shift rosters must start in Draft.'))
            branch = self.env['restaurant.branch'].browse(values.get('branch_id'))
            require_assigned_branches(branch)
            values['state'] = 'draft'
            prepared.append(values)
        records = super().create(prepared)
        records._populate_staff_rows()
        return records

    def write(self, vals):
        lock_records(self)
        if self.env.context.get('restaurant_roster_internal'):
            return super().write(vals)
        require_role(self.env, MANAGER_GROUP)
        require_assigned_branches(self.branch_id)
        if 'state' in vals:
            raise AccessError(self.env._('Use roster workflow buttons to change state.'))
        if any(roster.state != 'draft' for roster in self):
            raise AccessError(self.env._('Reopen the roster before changing it.'))
        if set(vals) - {'branch_id', 'month_start', 'line_ids'}:
            raise AccessError(self.env._('Roster audit fields cannot be edited manually.'))
        if 'branch_id' in vals:
            branch = self.env['restaurant.branch'].browse(vals['branch_id'])
            require_assigned_branches(branch)
        if any(roster.line_ids for roster in self) and set(vals) & {'branch_id', 'month_start'}:
            raise AccessError(self.env._(
                'Branch and month cannot change after staff rows are populated.'
            ))
        return super().write(vals)

    def unlink(self):
        lock_records(self, 'unlink')
        require_role(self.env, MANAGER_GROUP)
        require_assigned_branches(self.branch_id)
        if any(roster.state != 'draft' for roster in self):
            raise AccessError(self.env._('Only draft shift rosters can be deleted.'))
        return super().unlink()

    def _populate_staff_rows(self):
        Line = self.env['restaurant.shift.roster.line']
        totals = {}
        for roster in self:
            employees = self.env['hr.employee'].sudo().search([
                ('active', '=', True),
                ('company_id', '=', roster.company_id.id),
                ('restaurant_branch_id', '=', roster.branch_id.id),
            ], order='name, id')
            existing = {line.employee_id.id: line for line in roster.line_ids}
            for employee in employees:
                if employee.id not in existing:
                    Line.create({
                        'roster_id': roster.id,
                        'employee_id': employee.id,
                    })
            stale = roster.line_ids.filtered(lambda line: line.employee_id not in employees)
            if stale:
                stale.unlink()
            totals[roster.id] = len(employees)
        return totals

    def action_populate_staff(self):
        self.ensure_one()
        lock_records(self)
        require_role(self.env, MANAGER_GROUP)
        require_assigned_branches(self.branch_id)
        if self.state != 'draft':
            raise AccessError(self.env._('Only a draft roster can be refreshed.'))

        total = self._populate_staff_rows()[self.id]
        return {
            'type': 'ir.actions.client',
            'tag': 'display_notification',
            'params': {
                'title': self.env._('Roster Refreshed'),
                'message': self.env._('%s employee(s) are ready for scheduling.', total),
                'type': 'success',
                'sticky': False,
                'next': {'type': 'ir.actions.client', 'tag': 'reload'},
            },
        }

    def action_publish(self):
        self.ensure_one()
        lock_records(self)
        require_role(self.env, MANAGER_GROUP)
        require_assigned_branches(self.branch_id)
        if self.state != 'draft':
            raise UserError(self.env._('Only a draft roster can be published.'))
        if not self.line_ids:
            raise ValidationError(self.env._('Populate the staff roster before publishing.'))
        missing = self.line_ids.filtered(lambda line: not line.staff_shift)
        if missing:
            raise ValidationError(self.env._(
                'Choose Morning, Evening, or One Shift for every employee: %s',
                ', '.join(missing[:8].mapped('employee_id.name')),
            ))
        super(RestaurantShiftRoster, self.with_context(
            restaurant_roster_internal=True,
        )).write({
            'state': 'published',
            'published_by': self.env.uid,
            'published_at': fields.Datetime.now(),
        })
        draft_sheets = self.env['restaurant.attendance.sheet'].sudo().search([
            ('branch_id', '=', self.branch_id.id),
            ('attendance_date', '>=', self.month_start),
            ('attendance_date', '<=', self.month_end),
            ('state', '=', 'draft'),
        ])
        for sheet in draft_sheets:
            sheet._sync_from_shift_roster(self)
        self.message_post(body=self.env._(
            'Monthly shift roster published by %s.', self.env.user.name,
        ))
        return True

    def action_reopen(self):
        self.ensure_one()
        lock_records(self)
        require_role(self.env, MANAGER_GROUP)
        require_assigned_branches(self.branch_id)
        if self.state != 'published':
            raise UserError(self.env._('Only a published roster can be reopened.'))
        super(RestaurantShiftRoster, self.with_context(
            restaurant_roster_internal=True,
        )).write({
            'state': 'draft',
            'published_by': False,
            'published_at': False,
        })
        self.message_post(body=self.env._(
            'Monthly shift roster reopened by %s.', self.env.user.name,
        ))
        return True


class RestaurantShiftRosterLine(models.Model):
    _name = 'restaurant.shift.roster.line'
    _description = 'Restaurant Monthly Shift Roster Line'
    _order = 'staff_category_id, employee_id, id'

    roster_id = fields.Many2one(
        'restaurant.shift.roster', required=True, ondelete='cascade', index=True,
    )
    employee_id = fields.Many2one(
        'hr.employee', required=True, ondelete='restrict', index=True,
    )
    branch_id = fields.Many2one(
        related='roster_id.branch_id', store=True, index=True,
    )
    company_id = fields.Many2one(
        related='roster_id.company_id', store=True, index=True,
    )
    staff_category_id = fields.Many2one(
        related='employee_id.restaurant_staff_category_id',
        string='Staff Category', store=True, readonly=True,
    )
    job_id = fields.Many2one(
        related='employee_id.job_id', string='Job / Role', readonly=True,
    )
    staff_shift = fields.Selection(
        SHIFT_SELECTION, string='Shift', index=True,
    )

    _roster_employee_unique = models.Constraint(
        'UNIQUE(roster_id, employee_id)',
        'The employee already exists in this shift roster.',
    )

    @api.constrains('employee_id', 'roster_id')
    def _check_employee_branch(self):
        for line in self:
            if line.employee_id.restaurant_branch_id != line.roster_id.branch_id:
                raise ValidationError(self.env._(
                    '%s is not assigned to this roster branch.',
                    line.employee_id.display_name,
                ))

    @api.model_create_multi
    def create(self, vals_list):
        require_role(self.env, MANAGER_GROUP)
        rosters = self.env['restaurant.shift.roster'].browse([
            vals.get('roster_id') for vals in vals_list if vals.get('roster_id')
        ])
        require_assigned_branches(rosters.branch_id)
        if any(roster.state != 'draft' for roster in rosters):
            raise AccessError(self.env._('Staff can only be added to a draft roster.'))
        return super().create(vals_list)

    def write(self, vals):
        self.check_access('write')
        self.lock_for_update()
        self.invalidate_recordset(['roster_id'])
        require_role(self.env, MANAGER_GROUP)
        require_assigned_branches(self.branch_id)
        if any(line.roster_id.state != 'draft' for line in self):
            raise AccessError(self.env._('Reopen the roster before changing shifts.'))
        if set(vals) - {'staff_shift'}:
            raise AccessError(self.env._('Only the scheduled shift can be edited here.'))
        return super().write(vals)

    def unlink(self):
        self.check_access('unlink')
        self.lock_for_update()
        self.invalidate_recordset(['roster_id'])
        require_role(self.env, MANAGER_GROUP)
        require_assigned_branches(self.branch_id)
        if any(line.roster_id.state != 'draft' for line in self):
            raise AccessError(self.env._('Staff can only be removed from a draft roster.'))
        return super().unlink()

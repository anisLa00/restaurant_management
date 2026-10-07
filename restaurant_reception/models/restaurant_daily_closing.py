from odoo import api, fields, models
from odoo.exceptions import AccessError, UserError, ValidationError

from .reception_security import (
    MANAGER_GROUP, RECEPTION_GROUP, lock_records, require_assigned_branches,
    require_draft, require_role,
)


class RestaurantDailyClosing(models.Model):
    _name = 'restaurant.daily.closing'
    _description = 'Restaurant Daily Closing'
    _inherit = ['mail.thread', 'mail.activity.mixin']
    _order = 'closing_date desc, id desc'
    _mail_post_access = 'read'

    name = fields.Char(string='Reference', required=True, readonly=True, copy=False, default='New', index=True)
    branch_id = fields.Many2one('restaurant.branch', required=True, ondelete='restrict', index=True, tracking=True)
    closing_date = fields.Date(required=True, default=fields.Date.context_today, index=True, tracking=True)
    shift = fields.Selection([
        ('morning', 'Morning'), ('evening', 'Evening'), ('full_day', 'Full Day'),
    ], required=True, default='full_day', tracking=True)
    reception_user_id = fields.Many2one('res.users', required=True, readonly=True, default=lambda self: self.env.user)
    branch_manager_id = fields.Many2one('res.users', string='Reviewed By', readonly=True, copy=False, tracking=True)
    manager_reviewed_at = fields.Datetime(readonly=True, copy=False, tracking=True)
    state = fields.Selection([
        ('draft', 'Draft'), ('manager_review', 'Submitted'),
        ('confirmed', 'Reviewed'), ('cancelled', 'Cancelled'),
    ], required=True, default='draft', readonly=True, copy=False, tracking=True)
    service_tracking_mode = fields.Selection([
        ('legacy', 'Legacy Manual Totals'),
        ('service_entries', 'Bill Service Entries'),
        ('standalone', 'Standalone Waiter Closing'),
    ], required=True, default='standalone', readonly=True, copy=False,
        help=(
            'Historical closings keep their original manual waiter and tip '
            'totals or embedded bill entries. New closings use the separate '
            'Waiter Sales & Tips workflow.'
        ))
    waiter_sales_closing_id = fields.Many2one(
        'restaurant.waiter.sales.closing',
        string='Waiter Sales & Tips Closing',
        readonly=True,
        copy=False,
        ondelete='set null',
        index=True,
    )
    waiter_sales_closing_state = fields.Selection(
        related='waiter_sales_closing_id.state',
        string='Waiter Closing Status',
        readonly=True,
    )
    service_entry_ids = fields.One2many(
        'restaurant.waiter.service.entry', 'closing_id',
        string='Service Entries', copy=True,
    )
    waiter_line_ids = fields.One2many('restaurant.waiter.daily.line', 'closing_id', string='Waiter Sales & Tips', copy=True)
    total_waiter_sales = fields.Monetary(compute='_compute_totals', store=True, tracking=True)
    total_tips = fields.Monetary(
        string='Waiter Tip Contribution',
        compute='_compute_totals', store=True, tracking=True,
    )
    total_orders = fields.Integer(compute='_compute_totals', store=True, tracking=True)
    total_tables = fields.Integer(compute='_compute_totals', store=True, tracking=True)
    reported_total_sales = fields.Monetary(
        string='Total Sales', required=True, default=0, tracking=True,
        help=(
            'Final daily POS sales total including 5% VAT. This remains the '
            'amount reconciled against the payment breakdown.'
        ),
    )
    calculated_vat_amount = fields.Monetary(
        string='Calculated VAT (5%)',
        compute='_compute_vat_breakdown',
        readonly=True,
        help=(
            'VAT extracted from the VAT-inclusive Total Sales using '
            'Total Sales × 5 / 105 and rounded in the company currency.'
        ),
    )
    net_sales_excluding_vat = fields.Monetary(
        string='Net Sales (Excl. VAT)',
        compute='_compute_vat_breakdown',
        readonly=True,
        help='Total Sales less the rounded Calculated VAT.',
    )
    reported_vat_amount = fields.Monetary(
        string='POS Reported VAT',
        default=0,
        tracking=True,
        help=(
            'VAT copied from the POS report for the same business date. '
            'It is informational and never changes Total Sales or payments.'
        ),
    )
    vat_difference = fields.Monetary(
        string='VAT Difference',
        compute='_compute_vat_breakdown',
        readonly=True,
        help='POS Reported VAT minus Calculated VAT.',
    )
    guest_count = fields.Integer(required=True, default=0, tracking=True)
    tracked_dine_in_sales = fields.Monetary(
        compute='_compute_service_entry_totals', store=True, readonly=True,
    )
    tracked_pickup_sales = fields.Monetary(
        compute='_compute_service_entry_totals', store=True, readonly=True,
    )
    tracked_delivery_sales = fields.Monetary(
        compute='_compute_service_entry_totals', store=True, readonly=True,
    )
    tracked_service_sales = fields.Monetary(
        string='Tracked Bill Sales',
        compute='_compute_service_entry_totals', store=True, readonly=True,
    )
    service_sales_difference = fields.Monetary(
        string='Sales Difference',
        compute='_compute_service_entry_totals', store=True, readonly=True,
        help='Official Total Sales minus the sum of bill service entries.',
    )
    tracked_guest_count = fields.Integer(
        string='Tracked Dine-in Guests',
        compute='_compute_service_entry_totals', store=True, readonly=True,
    )
    guest_count_difference = fields.Integer(
        string='Guest Difference',
        compute='_compute_service_entry_totals', store=True, readonly=True,
        help='Official Guests minus guests recorded on dine-in bill entries.',
    )
    tracked_cash_tips = fields.Monetary(
        string='Cash Tips',
        compute='_compute_service_entry_totals', store=True, readonly=True,
    )
    tracked_card_tips = fields.Monetary(
        string='Card Tips',
        compute='_compute_service_entry_totals', store=True, readonly=True,
    )
    tracked_total_tips = fields.Monetary(
        string='Pooled Tips',
        compute='_compute_service_entry_totals', store=True, readonly=True,
    )
    service_bill_count = fields.Integer(
        string='Tracked Bills',
        compute='_compute_service_entry_totals', store=True, readonly=True,
    )
    dine_in_bill_count = fields.Integer(
        string='Dine-in Bills',
        compute='_compute_service_entry_totals', store=True, readonly=True,
    )
    pickup_bill_count = fields.Integer(
        string='Pickup Bills',
        compute='_compute_service_entry_totals', store=True, readonly=True,
    )
    delivery_bill_count = fields.Integer(
        string='Delivery Bills',
        compute='_compute_service_entry_totals', store=True, readonly=True,
    )
    total_reviews = fields.Integer(
        string='Reviews',
        compute='_compute_service_entry_totals', store=True, readonly=True,
    )
    payment_line_ids = fields.One2many(
        'restaurant.reception.payment.line', 'closing_id',
        string='Payment Breakdown', copy=True,
    )
    cash_payment_total = fields.Monetary(
        compute='_compute_payment_totals', store=True, readonly=True,
    )
    card_payment_total = fields.Monetary(
        compute='_compute_payment_totals', store=True, readonly=True,
    )
    local_payment_total = fields.Monetary(
        compute='_compute_payment_totals', store=True, readonly=True,
    )
    delivery_payment_total = fields.Monetary(
        compute='_compute_payment_totals', store=True, readonly=True,
    )
    other_payment_total = fields.Monetary(
        compute='_compute_payment_totals', store=True, readonly=True,
    )
    payment_total = fields.Monetary(
        compute='_compute_payment_totals', store=True, readonly=True,
        tracking=True,
    )
    payment_difference = fields.Monetary(
        compute='_compute_payment_totals', store=True, readonly=True,
        help='Total Sales minus the payment breakdown total.',
    )
    tip_line_ids = fields.One2many(
        'restaurant.reception.tip.line', 'closing_id',
        string='Tip Breakdown by Source', copy=True,
    )
    reported_total_tips = fields.Monetary(
        string='Total Tips', compute='_compute_tip_source_totals',
        store=True, readonly=True, tracking=True,
    )
    tip_allocation_difference = fields.Monetary(
        string='Unattributed Pooled Tips',
        compute='_compute_tip_source_totals', store=True, readonly=True,
        help=(
            'Pooled tips minus dine-in waiter contributions. In Service Entries '
            'mode this is normally pickup/delivery tip contribution.'
        ),
    )
    discount_entry_ids = fields.One2many(
        'restaurant.reception.discount', 'closing_id',
        string='Discount Entries', copy=True,
    )
    total_discounts = fields.Monetary(
        compute='_compute_discount_total', store=True, readonly=True,
        tracking=True,
    )
    opening_cash_float = fields.Monetary(default=0, tracking=True)
    closing_cash_float = fields.Monetary(default=0, tracking=True)
    supporting_attachment_ids = fields.Many2many(
        'ir.attachment', 'restaurant_daily_closing_attachment_rel',
        'closing_id', 'attachment_id', string='Supporting Documents',
        copy=False,
    )
    entry_source = fields.Selection([
        ('manual', 'Manual'), ('foodics', 'Foodics'), ('import', 'Imported'),
    ], required=True, default='manual', readonly=True, index=True)
    external_reference = fields.Char(
        readonly=True, copy=False, index=True,
        help='Reserved for a future POS/Foodics daily report identifier.',
    )
    submitted_by_id = fields.Many2one(
        'res.users', string='Submitted By', readonly=True, copy=False,
        tracking=True,
    )
    submitted_at = fields.Datetime(readonly=True, copy=False, tracking=True)
    submission_count = fields.Integer(readonly=True, copy=False)
    review_note = fields.Text(tracking=True)
    correction_reason = fields.Text(
        copy=False,
        help='Required before management returns a submitted closing to Draft.',
    )
    last_reopened_by_id = fields.Many2one(
        'res.users', string='Last Reopened By', readonly=True, copy=False,
    )
    last_reopened_at = fields.Datetime(readonly=True, copy=False)
    reopen_count = fields.Integer(readonly=True, copy=False)
    cancelled_by_id = fields.Many2one(
        'res.users', string='Cancelled By', readonly=True, copy=False,
    )
    cancelled_at = fields.Datetime(readonly=True, copy=False)
    whatsapp_summary = fields.Text(
        string='WhatsApp-ready Summary', compute='_compute_whatsapp_summary',
        readonly=True,
    )
    notes = fields.Text(tracking=True)
    company_id = fields.Many2one(related='branch_id.company_id', store=True, index=True)
    currency_id = fields.Many2one(related='company_id.currency_id', store=True)

    _active_closing_unique = models.UniqueIndex(
        "(branch_id, closing_date) WHERE state != 'cancelled'",
        "An active closing already exists for this branch and business date.",
    )
    _external_reference_unique = models.UniqueIndex(
        "(company_id, entry_source, external_reference) "
        "WHERE external_reference IS NOT NULL",
        "This external daily closing reference has already been imported.",
    )

    @api.depends(
        'service_tracking_mode',
        'waiter_line_ids.sales_amount', 'waiter_line_ids.tips_amount',
        'waiter_line_ids.order_count', 'waiter_line_ids.table_count',
        'waiter_sales_closing_id.waiter_sales',
        'waiter_sales_closing_id.waiter_tip_contribution',
        'waiter_sales_closing_id.dine_in_bill_count',
    )
    def _compute_totals(self):
        for closing in self:
            if closing.service_tracking_mode == 'standalone':
                waiter_closing = closing.waiter_sales_closing_id
                closing.total_waiter_sales = waiter_closing.waiter_sales
                closing.total_tips = waiter_closing.waiter_tip_contribution
                closing.total_orders = waiter_closing.dine_in_bill_count
                closing.total_tables = waiter_closing.dine_in_bill_count
            else:
                closing.total_waiter_sales = sum(
                    closing.waiter_line_ids.mapped('sales_amount')
                )
                closing.total_tips = sum(
                    closing.waiter_line_ids.mapped('tips_amount')
                )
                closing.total_orders = sum(
                    closing.waiter_line_ids.mapped('order_count')
                )
                closing.total_tables = sum(
                    closing.waiter_line_ids.mapped('table_count')
                )

    @api.depends(
        'service_tracking_mode',
        'reported_total_sales',
        'guest_count',
        'service_entry_ids.service_type',
        'service_entry_ids.bill_amount',
        'service_entry_ids.guest_count',
        'service_entry_ids.cash_tip_amount',
        'service_entry_ids.card_tip_amount',
        'service_entry_ids.review_count',
        'waiter_sales_closing_id.dine_in_sales',
        'waiter_sales_closing_id.pickup_sales',
        'waiter_sales_closing_id.delivery_sales',
        'waiter_sales_closing_id.total_sales',
        'waiter_sales_closing_id.guest_count',
        'waiter_sales_closing_id.cash_tips',
        'waiter_sales_closing_id.card_tips',
        'waiter_sales_closing_id.total_tips',
        'waiter_sales_closing_id.bill_count',
        'waiter_sales_closing_id.dine_in_bill_count',
        'waiter_sales_closing_id.pickup_bill_count',
        'waiter_sales_closing_id.delivery_bill_count',
        'waiter_sales_closing_id.review_count',
    )
    def _compute_service_entry_totals(self):
        for closing in self:
            if closing.service_tracking_mode == 'legacy':
                closing.tracked_dine_in_sales = 0
                closing.tracked_pickup_sales = 0
                closing.tracked_delivery_sales = 0
                closing.tracked_service_sales = 0
                closing.service_sales_difference = 0
                closing.tracked_guest_count = 0
                closing.guest_count_difference = 0
                closing.tracked_cash_tips = 0
                closing.tracked_card_tips = 0
                closing.tracked_total_tips = 0
                closing.service_bill_count = 0
                closing.dine_in_bill_count = 0
                closing.pickup_bill_count = 0
                closing.delivery_bill_count = 0
                closing.total_reviews = 0
                continue

            if closing.service_tracking_mode == 'standalone':
                waiter_closing = closing.waiter_sales_closing_id
                closing.tracked_dine_in_sales = waiter_closing.dine_in_sales
                closing.tracked_pickup_sales = waiter_closing.pickup_sales
                closing.tracked_delivery_sales = waiter_closing.delivery_sales
                closing.tracked_service_sales = waiter_closing.total_sales
                closing.service_sales_difference = (
                    closing.reported_total_sales - waiter_closing.total_sales
                )
                closing.tracked_guest_count = waiter_closing.guest_count
                closing.guest_count_difference = (
                    closing.guest_count - waiter_closing.guest_count
                )
                closing.tracked_cash_tips = waiter_closing.cash_tips
                closing.tracked_card_tips = waiter_closing.card_tips
                closing.tracked_total_tips = waiter_closing.total_tips
                closing.service_bill_count = waiter_closing.bill_count
                closing.dine_in_bill_count = waiter_closing.dine_in_bill_count
                closing.pickup_bill_count = waiter_closing.pickup_bill_count
                closing.delivery_bill_count = waiter_closing.delivery_bill_count
                closing.total_reviews = waiter_closing.review_count
                continue

            entries = closing.service_entry_ids
            dine_in = entries.filtered(lambda entry: entry.service_type == 'dine_in')
            pickup = entries.filtered(lambda entry: entry.service_type == 'pickup')
            delivery = entries.filtered(lambda entry: entry.service_type == 'delivery')
            closing.tracked_dine_in_sales = sum(dine_in.mapped('bill_amount'))
            closing.tracked_pickup_sales = sum(pickup.mapped('bill_amount'))
            closing.tracked_delivery_sales = sum(delivery.mapped('bill_amount'))
            closing.tracked_service_sales = sum(entries.mapped('bill_amount'))
            closing.service_sales_difference = (
                closing.reported_total_sales - closing.tracked_service_sales
            )
            closing.tracked_guest_count = sum(dine_in.mapped('guest_count'))
            closing.guest_count_difference = (
                closing.guest_count - closing.tracked_guest_count
            )
            closing.tracked_cash_tips = sum(entries.mapped('cash_tip_amount'))
            closing.tracked_card_tips = sum(entries.mapped('card_tip_amount'))
            closing.tracked_total_tips = (
                closing.tracked_cash_tips + closing.tracked_card_tips
            )
            closing.service_bill_count = len(entries)
            closing.dine_in_bill_count = len(dine_in)
            closing.pickup_bill_count = len(pickup)
            closing.delivery_bill_count = len(delivery)
            closing.total_reviews = sum(entries.mapped('review_count'))

    @api.depends('reported_total_sales', 'reported_vat_amount', 'currency_id')
    def _compute_vat_breakdown(self):
        for closing in self:
            currency = closing.currency_id or self.env.company.currency_id
            calculated_vat = currency.round(
                closing.reported_total_sales * 5.0 / 105.0
            )
            closing.calculated_vat_amount = calculated_vat
            closing.net_sales_excluding_vat = (
                closing.reported_total_sales - calculated_vat
            )
            closing.vat_difference = (
                closing.reported_vat_amount - calculated_vat
            )

    @api.depends(
        'reported_total_sales',
        'payment_line_ids.payment_type',
        'payment_line_ids.amount',
    )
    def _compute_payment_totals(self):
        for closing in self:
            amounts = {
                payment_type: sum(
                    closing.payment_line_ids.filtered(
                        lambda line: line.payment_type == payment_type
                    ).mapped('amount')
                )
                for payment_type in ('cash', 'card', 'local', 'delivery', 'other')
            }
            closing.cash_payment_total = amounts['cash']
            closing.card_payment_total = amounts['card']
            closing.local_payment_total = amounts['local']
            closing.delivery_payment_total = amounts['delivery']
            closing.other_payment_total = amounts['other']
            closing.payment_total = sum(amounts.values())
            closing.payment_difference = (
                closing.reported_total_sales - closing.payment_total
            )

    @api.depends(
        'service_tracking_mode', 'tip_line_ids.amount', 'total_tips',
        'tracked_total_tips',
    )
    def _compute_tip_source_totals(self):
        for closing in self:
            if closing.service_tracking_mode != 'legacy':
                closing.reported_total_tips = closing.tracked_total_tips
            else:
                closing.reported_total_tips = sum(
                    closing.tip_line_ids.mapped('amount')
                )
            closing.tip_allocation_difference = (
                closing.reported_total_tips - closing.total_tips
            )

    @api.depends('discount_entry_ids.amount')
    def _compute_discount_total(self):
        for closing in self:
            closing.total_discounts = sum(
                closing.discount_entry_ids.mapped('amount')
            )

    @api.constrains(
        'reported_total_sales', 'reported_vat_amount', 'guest_count',
        'opening_cash_float', 'closing_cash_float',
    )
    def _check_nonnegative_daily_values(self):
        for closing in self:
            if any(value < 0 for value in (
                closing.reported_total_sales,
                closing.reported_vat_amount,
                closing.guest_count,
                closing.opening_cash_float,
                closing.closing_cash_float,
            )):
                raise ValidationError(
                    self.env._(
                        'Sales, reported VAT, guests, and cash float values '
                        'cannot be negative.'
                    )
                )

    def _format_summary_amount(self, amount):
        self.ensure_one()
        symbol = self.currency_id.symbol or self.currency_id.name or ''
        return '%s %0.2f' % (symbol, amount)

    def _get_whatsapp_summary_extra_lines(self):
        self.ensure_one()
        return []

    def _build_whatsapp_summary(self):
        self.ensure_one()
        state_label = dict(self._fields['state'].selection).get(
            self.state, self.state
        )
        lines = [
            self.env._('Reception Daily Summary'),
            self.env._('Branch: %s', self.branch_id.display_name),
            self.env._('Business Date: %s', self.closing_date),
            self.env._('Status: %s', state_label),
            self.env._(
                'Total Sales: %s',
                self._format_summary_amount(self.reported_total_sales),
            ),
            self.env._(
                'Net Sales (Excl. VAT): %s',
                self._format_summary_amount(self.net_sales_excluding_vat),
            ),
            self.env._(
                'VAT (Calculated 5%%): %s',
                self._format_summary_amount(self.calculated_vat_amount),
            ),
            self.env._(
                'Payments: Cash %s | Card %s | Local %s | Delivery %s | Other %s',
                self._format_summary_amount(self.cash_payment_total),
                self._format_summary_amount(self.card_payment_total),
                self._format_summary_amount(self.local_payment_total),
                self._format_summary_amount(self.delivery_payment_total),
                self._format_summary_amount(self.other_payment_total),
            ),
            self.env._('Guests: %s', self.guest_count),
            self.env._(
                'Tips: %s',
                self._format_summary_amount(self.reported_total_tips),
            ),
            self.env._(
                'Discounts: %s',
                self._format_summary_amount(self.total_discounts),
            ),
        ]
        if self.service_tracking_mode != 'legacy':
            lines.extend([
                self.env._(
                    'Tracked Bills: %s (Dine-in %s | Pickup %s | Delivery %s)',
                    self.service_bill_count,
                    self.dine_in_bill_count,
                    self.pickup_bill_count,
                    self.delivery_bill_count,
                ),
                self.env._(
                    'Tracked Sales: %s | Difference %s',
                    self._format_summary_amount(self.tracked_service_sales),
                    self._format_summary_amount(self.service_sales_difference),
                ),
                self.env._(
                    'Tracked Guests: %s | Difference %s',
                    self.tracked_guest_count,
                    self.guest_count_difference,
                ),
                self.env._(
                    'Pooled Tips: Cash %s | Card %s | Total %s',
                    self._format_summary_amount(self.tracked_cash_tips),
                    self._format_summary_amount(self.tracked_card_tips),
                    self._format_summary_amount(self.tracked_total_tips),
                ),
                self.env._('Reviews: %s', self.total_reviews),
            ])
        if self.reported_vat_amount:
            lines.append(self.env._(
                'VAT Comparison: POS Reported %s | Difference %s',
                self._format_summary_amount(self.reported_vat_amount),
                self._format_summary_amount(self.vat_difference),
            ))
        lines.extend(self._get_whatsapp_summary_extra_lines())
        if self.notes:
            lines.append(self.env._('Notes: %s', self.notes.strip()))
        return '\n'.join(lines)

    def _compute_whatsapp_summary(self):
        for closing in self:
            closing.whatsapp_summary = closing._build_whatsapp_summary()

    def get_whatsapp_summary(self):
        self.ensure_one()
        return self._build_whatsapp_summary()

    @api.model_create_multi
    def create(self, vals_list):
        require_role(self.env, RECEPTION_GROUP)
        prepared = []
        for values in vals_list:
            values = dict(values)
            if values.get('state', 'draft') != 'draft':
                raise AccessError(self.env._('Closings must be created in draft.'))
            if set(values) & {
                'branch_manager_id', 'manager_reviewed_at',
                'submitted_by_id', 'submitted_at', 'submission_count',
                'last_reopened_by_id', 'last_reopened_at', 'reopen_count',
                'cancelled_by_id', 'cancelled_at',
                'total_waiter_sales', 'total_tips', 'total_orders',
                'total_tables', 'cash_payment_total', 'card_payment_total',
                'local_payment_total', 'delivery_payment_total',
                'other_payment_total', 'payment_total', 'payment_difference',
                'calculated_vat_amount', 'net_sales_excluding_vat',
                'vat_difference',
                'reported_total_tips', 'tip_allocation_difference',
                'service_tracking_mode', 'waiter_sales_closing_id',
                'waiter_sales_closing_state',
                'tracked_dine_in_sales', 'tracked_pickup_sales',
                'tracked_delivery_sales', 'tracked_service_sales',
                'service_sales_difference', 'tracked_guest_count',
                'guest_count_difference', 'tracked_cash_tips',
                'tracked_card_tips', 'tracked_total_tips',
                'service_bill_count', 'dine_in_bill_count',
                'pickup_bill_count', 'delivery_bill_count', 'total_reviews',
                'total_discounts', 'company_id', 'currency_id',
                'entry_source', 'external_reference',
            }:
                raise AccessError(self.env._('Review and calculated fields cannot be supplied manually.'))
            values.update({
                'state': 'draft', 'reception_user_id': self.env.uid,
                'branch_manager_id': False, 'manager_reviewed_at': False,
                'submitted_by_id': False, 'submitted_at': False,
                'name': self.env['ir.sequence'].next_by_code('restaurant.daily.closing'),
            })
            prepared.append(values)
        records = super().create(prepared)
        require_assigned_branches(records.branch_id)
        records._sync_waiter_sales_closing()
        return records

    def write(self, vals):
        if self.env.su and self.env.context.get('waiter_closing_sync'):
            if set(vals) - {'waiter_sales_closing_id'}:
                raise AccessError(self.env._('Invalid Waiter Closing synchronization.'))
            return super().write(vals)
        if self.env.su and self.env.context.get('service_tracking_migration'):
            result = super().write(vals)
            if 'service_tracking_mode' in vals:
                self._sync_waiter_sales_closing()
            return result
        lock_records(self)
        editable = self._draft_editable_fields()
        if 'state' in vals:
            if set(vals) != {'state'}:
                raise AccessError(self.env._('Save your changes before changing the workflow state.'))
            target = vals['state']
            for closing in self:
                closing._check_transition(target)
            for closing in self:
                values = dict(vals)
                now = fields.Datetime.now()
                correction_reason = closing.correction_reason
                if target == 'manager_review':
                    values.update(
                        submitted_by_id=self.env.uid,
                        submitted_at=now,
                        submission_count=closing.submission_count + 1,
                    )
                elif target == 'confirmed':
                    values.update(
                        branch_manager_id=self.env.uid,
                        manager_reviewed_at=now,
                    )
                elif target == 'draft':
                    values.update(
                        last_reopened_by_id=self.env.uid,
                        last_reopened_at=now,
                        reopen_count=closing.reopen_count + 1,
                        correction_reason=False,
                    )
                elif target == 'cancelled':
                    values.update(
                        cancelled_by_id=self.env.uid,
                        cancelled_at=now,
                    )
                super(RestaurantDailyClosing, closing).write(values)
                if target == 'draft':
                    closing.message_post(body=self.env._(
                        'Closing reopened for correction by %s. Reason: %s',
                        self.env.user.name, correction_reason,
                    ))
                closing.message_post(body=self.env._('Workflow changed to %s by %s.',
                    dict(self._fields['state'].selection)[target], self.env.user.name))
            # Release the partial unique index before a replacement is created.
            self.flush_recordset(['state'])
            if target == 'cancelled':
                self.sudo().with_context(waiter_closing_sync=True).write({
                    'waiter_sales_closing_id': False,
                })
            return True
        if (
            all(closing.state == 'manager_review' for closing in self)
            and self.env.user.has_group(MANAGER_GROUP)
        ):
            if set(vals) - {'review_note', 'correction_reason'}:
                raise AccessError(self.env._(
                    'Management can only edit the review note and correction reason.'
                ))
            require_assigned_branches(self.branch_id)
            return super().write(vals)
        if set(vals) - editable:
            raise AccessError(self.env._('System and review fields cannot be modified manually.'))
        require_role(self.env, RECEPTION_GROUP)
        require_draft(self)
        require_assigned_branches(self.branch_id)
        if 'branch_id' in vals:
            require_assigned_branches(self.env['restaurant.branch'].browse(vals['branch_id']))
        result = super().write(vals)
        if 'branch_id' in vals:
            self.waiter_line_ids._check_employee_company()
            self.service_entry_ids.filtered('employee_id')._check_service_details()
        if set(vals) & {'branch_id', 'closing_date'}:
            self._sync_waiter_sales_closing()
        return result

    @api.model
    def _draft_editable_fields(self):
        return {
            'branch_id', 'closing_date', 'shift', 'waiter_line_ids', 'notes',
            'service_entry_ids',
            'reported_total_sales', 'guest_count', 'payment_line_ids',
            'reported_vat_amount',
            'tip_line_ids', 'discount_entry_ids', 'opening_cash_float',
            'closing_cash_float', 'supporting_attachment_ids',
        }

    def unlink(self):
        lock_records(self, 'unlink')
        require_role(self.env, RECEPTION_GROUP)
        require_draft(self)
        require_assigned_branches(self.branch_id)
        return super().unlink()

    def _check_transition(self, target):
        self.ensure_one()
        transitions = {
            ('draft', 'manager_review'): RECEPTION_GROUP,
            ('manager_review', 'confirmed'): MANAGER_GROUP,
            ('manager_review', 'draft'): MANAGER_GROUP,
            ('draft', 'cancelled'): RECEPTION_GROUP,
            ('manager_review', 'cancelled'): MANAGER_GROUP,
        }
        group = transitions.get((self.state, target))
        if not group:
            raise UserError(self.env._('This closing workflow transition is not allowed.'))
        require_role(self.env, group)
        require_assigned_branches(self.branch_id)
        if self.service_tracking_mode == 'standalone':
            waiter_closing = self.waiter_sales_closing_id
            if target == 'confirmed' and waiter_closing.state != 'closed':
                raise ValidationError(
                    self.env._(
                        'Close the linked Waiter Sales & Tips closing before '
                        'marking the Reception Daily Closing as Reviewed.'
                    )
                )
        if (
            self.state == 'manager_review'
            and target == 'draft'
            and not (self.correction_reason or '').strip()
        ):
            raise ValidationError(
                self.env._(
                    'Enter a correction reason before returning the closing to Draft.'
                )
            )

    def action_submit(self):
        self.ensure_one()
        return self.write({'state': 'manager_review'})

    def action_confirm(self):
        self.ensure_one()
        return self.write({'state': 'confirmed'})

    def action_return_to_draft(self):
        self.ensure_one()
        return self.write({'state': 'draft'})

    def action_cancel(self):
        self.ensure_one()
        return self.write({'state': 'cancelled'})

    def _sync_waiter_sales_closing(self):
        waiter_model = self.env['restaurant.waiter.sales.closing'].sudo()
        for closing in self:
            waiter_closing = waiter_model.browse()
            if (
                closing.service_tracking_mode == 'standalone'
                and closing.state != 'cancelled'
            ):
                waiter_closing = waiter_model.search([
                    ('branch_id', '=', closing.branch_id.id),
                    ('business_date', '=', closing.closing_date),
                    ('state', '!=', 'cancelled'),
                ], limit=1)
            if closing.waiter_sales_closing_id != waiter_closing:
                closing.sudo().with_context(waiter_closing_sync=True).write({
                    'waiter_sales_closing_id': waiter_closing.id or False,
                })

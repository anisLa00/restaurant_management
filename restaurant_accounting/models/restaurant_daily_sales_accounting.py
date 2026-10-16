from odoo import Command, api, fields, models
from odoo.exceptions import AccessError, UserError, ValidationError
from odoo.tools import float_is_zero

from .restaurant_purchase_invoice_handoff import ACCOUNTANT_GROUP, require_group


DAILY_WORKFLOW_CONTEXT = "restaurant_daily_sales_accounting_workflow"
DAILY_SYNC_CONTEXT = "restaurant_daily_sales_accounting_sync"
DAILY_WORKFLOW_SENTINEL = object()


class ResCompany(models.Model):
    _inherit = "res.company"

    restaurant_sales_journal_id = fields.Many2one(
        "account.journal", string="Daily Sales Journal", check_company=True,
        domain="[('type', '=', 'general'), ('company_id', '=', id)]",
    )
    restaurant_sales_income_account_id = fields.Many2one(
        "account.account", string="Restaurant Sales Income", check_company=True,
        domain="[('account_type', 'in', ('income', 'income_other'))]",
    )
    restaurant_vat_output_account_id = fields.Many2one(
        "account.account", string="Output VAT Payable", check_company=True,
        domain="[('account_type', '=', 'liability_current')]",
    )
    restaurant_cash_account_id = fields.Many2one(
        "account.account", string="Cash Collection Account", check_company=True,
        domain="[('account_type', 'in', ('asset_cash', 'asset_current'))]",
    )
    restaurant_card_clearing_account_id = fields.Many2one(
        "account.account", string="Card Clearing Account", check_company=True,
        domain="[('account_type', '=', 'asset_current')]",
    )
    restaurant_delivery_clearing_account_id = fields.Many2one(
        "account.account", string="Delivery Platform Clearing", check_company=True,
        domain="[('account_type', '=', 'asset_current')]",
    )
    restaurant_other_clearing_account_id = fields.Many2one(
        "account.account", string="Other Payment Clearing", check_company=True,
        domain="[('account_type', '=', 'asset_current')]",
    )
    restaurant_tips_payable_account_id = fields.Many2one(
        "account.account", string="Tips Payable", check_company=True,
        domain="[('account_type', '=', 'liability_current')]",
    )


class ResConfigSettings(models.TransientModel):
    _inherit = "res.config.settings"

    restaurant_sales_journal_id = fields.Many2one(
        related="company_id.restaurant_sales_journal_id", readonly=False,
    )
    restaurant_sales_income_account_id = fields.Many2one(
        related="company_id.restaurant_sales_income_account_id", readonly=False,
    )
    restaurant_vat_output_account_id = fields.Many2one(
        related="company_id.restaurant_vat_output_account_id", readonly=False,
    )
    restaurant_cash_account_id = fields.Many2one(
        related="company_id.restaurant_cash_account_id", readonly=False,
    )
    restaurant_card_clearing_account_id = fields.Many2one(
        related="company_id.restaurant_card_clearing_account_id", readonly=False,
    )
    restaurant_delivery_clearing_account_id = fields.Many2one(
        related="company_id.restaurant_delivery_clearing_account_id", readonly=False,
    )
    restaurant_other_clearing_account_id = fields.Many2one(
        related="company_id.restaurant_other_clearing_account_id", readonly=False,
    )
    restaurant_tips_payable_account_id = fields.Many2one(
        related="company_id.restaurant_tips_payable_account_id", readonly=False,
    )


class RestaurantAccountingDailySales(models.Model):
    _name = "restaurant.accounting.daily.sales"
    _description = "Restaurant Daily Sales Accounting"
    _inherit = ["mail.thread", "mail.activity.mixin"]
    _order = "business_date desc, branch_id, id desc"
    _mail_post_access = "read"

    name = fields.Char(required=True, readonly=True, copy=False, default="New", index=True)
    closing_id = fields.Many2one(
        "restaurant.daily.closing", required=True, readonly=True,
        ondelete="restrict", index=True,
    )
    company_id = fields.Many2one(
        related="closing_id.company_id", store=True, readonly=True, index=True,
    )
    branch_id = fields.Many2one(
        related="closing_id.branch_id", store=True, readonly=True, index=True,
    )
    business_date = fields.Date(
        related="closing_id.closing_date", store=True, readonly=True, index=True,
    )
    currency_id = fields.Many2one(
        related="closing_id.currency_id", store=True, readonly=True,
    )
    total_sales = fields.Monetary(readonly=True, currency_field="currency_id")
    net_sales = fields.Monetary(readonly=True, currency_field="currency_id")
    vat_amount = fields.Monetary(readonly=True, currency_field="currency_id")
    pos_reported_vat = fields.Monetary(readonly=True, currency_field="currency_id")
    vat_difference = fields.Monetary(readonly=True, currency_field="currency_id")
    cash_sales = fields.Monetary(readonly=True, currency_field="currency_id")
    card_sales = fields.Monetary(readonly=True, currency_field="currency_id")
    delivery_sales = fields.Monetary(readonly=True, currency_field="currency_id")
    other_sales = fields.Monetary(readonly=True, currency_field="currency_id")
    payment_total = fields.Monetary(readonly=True, currency_field="currency_id")
    payment_difference = fields.Monetary(readonly=True, currency_field="currency_id")
    cash_tips = fields.Monetary(readonly=True, currency_field="currency_id")
    card_tips = fields.Monetary(readonly=True, currency_field="currency_id")
    delivery_tips = fields.Monetary(readonly=True, currency_field="currency_id")
    other_tips = fields.Monetary(readonly=True, currency_field="currency_id")
    total_tips = fields.Monetary(readonly=True, currency_field="currency_id")
    review_note = fields.Text(tracking=True)
    workflow_state = fields.Selection(
        [
            ("pending", "Pending Accounting Review"),
            ("reviewed", "Reviewed"),
            ("draft_entry", "Journal Entry Created / Accounting Managed"),
            ("correction", "Returned for Reception Correction"),
        ],
        required=True, default="pending", readonly=True, copy=False,
        tracking=True, index=True,
    )
    state = fields.Selection(
        [
            ("pending", "Pending Accounting Review"),
            ("reviewed", "Reviewed"),
            ("draft_entry", "Draft Journal Entry"),
            ("posted", "Posted"),
            ("cancelled", "Journal Entry Cancelled"),
            ("correction", "Returned for Reception Correction"),
        ],
        compute="_compute_state", store=True, compute_sudo=True, precompute=True,
        required=True, readonly=True, copy=False, tracking=True, index=True,
    )
    journal_link_issue = fields.Boolean(
        compute="_compute_state", store=True, compute_sudo=True, precompute=True,
        readonly=True, copy=False,
        help=(
            "The linked journal entry is missing or does not belong to this "
            "daily sales record, company, or document type."
        ),
    )
    reviewed_by_id = fields.Many2one("res.users", readonly=True, copy=False)
    reviewed_at = fields.Datetime(readonly=True, copy=False)
    journal_entry_id = fields.Many2one(
        "account.move", readonly=True, copy=False, check_company=True,
        domain="[('move_type', '=', 'entry')]",
    )
    posted_by_id = fields.Many2one("res.users", readonly=True, copy=False)
    posted_at = fields.Datetime(readonly=True, copy=False)
    returned_by_id = fields.Many2one(
        "res.users", string="Last Returned By", readonly=True, copy=False,
    )
    returned_at = fields.Datetime(
        string="Last Returned At", readonly=True, copy=False,
    )
    correction_count = fields.Integer(readonly=True, copy=False)
    last_correction_reason = fields.Text(readonly=True, copy=False)
    last_correction_applied_by_id = fields.Many2one(
        "res.users", string="Last Correction Approved By", readonly=True, copy=False,
    )
    last_correction_applied_at = fields.Datetime(
        string="Last Correction Applied At", readonly=True, copy=False,
    )

    _closing_unique = models.Constraint(
        "UNIQUE(closing_id)",
        "A daily sales accounting record already exists for this closing.",
    )

    @api.depends(
        "workflow_state", "closing_id.company_id", "journal_entry_id",
        "journal_entry_id.state", "journal_entry_id.company_id",
        "journal_entry_id.move_type",
        "journal_entry_id.restaurant_daily_sales_id",
    )
    def _compute_state(self):
        # This is a status projection only. Native move changes remain the
        # source of truth and no actor/timestamp is fabricated here.
        for daily in self:
            state = daily.workflow_state or "pending"
            move = daily.journal_entry_id
            inconsistent = bool(move) and (
                move.company_id != daily.closing_id.company_id
                or move.move_type != "entry"
                or move.restaurant_daily_sales_id != daily
            )
            daily.journal_link_issue = bool(
                inconsistent or (state == "draft_entry" and not move)
            )
            if state == "draft_entry" and move and not inconsistent:
                if move.state == "cancel":
                    state = "cancelled"
                elif move.state == "posted":
                    state = "posted"
            daily.state = state

    @api.model
    def _snapshot_values(self, closing):
        tip_totals = {key: 0.0 for key in ("cash", "card", "delivery", "other")}
        for tip in closing.tip_line_ids:
            key = tip.tip_source if tip.tip_source in tip_totals else "other"
            tip_totals[key] += tip.amount
        return {
            "total_sales": closing.reported_total_sales,
            "net_sales": closing.net_sales_excluding_vat,
            "vat_amount": closing.calculated_vat_amount,
            "pos_reported_vat": closing.reported_vat_amount,
            "vat_difference": closing.vat_difference,
            "cash_sales": closing.cash_payment_total,
            "card_sales": closing.card_payment_total,
            "delivery_sales": closing.delivery_payment_total,
            "other_sales": closing.local_payment_total + closing.other_payment_total,
            "payment_total": closing.payment_total,
            "payment_difference": closing.payment_difference,
            "cash_tips": tip_totals["cash"],
            "card_tips": tip_totals["card"],
            "delivery_tips": tip_totals["delivery"],
            "other_tips": tip_totals["other"],
            "total_tips": sum(tip_totals.values()),
        }

    @api.model_create_multi
    def create(self, vals_list):
        if not self.env.su and not self.env.context.get(DAILY_SYNC_CONTEXT):
            require_group(self.env, ACCOUNTANT_GROUP)
        prepared = []
        for vals in vals_list:
            values = dict(vals)
            if (
                values.get(
                    "state", self.env.context.get("default_state", "pending")
                ) != "pending"
                or values.get(
                    "workflow_state",
                    self.env.context.get("default_workflow_state", "pending"),
                ) != "pending"
            ):
                raise AccessError(self.env._(
                    "Daily sales accounting records must start Pending."
                ))
            protected = {
                "journal_entry_id", "reviewed_by_id", "reviewed_at",
                "posted_by_id", "posted_at", "returned_by_id",
                "returned_at", "correction_count", "last_correction_reason",
                "last_correction_applied_by_id", "last_correction_applied_at",
            }
            if any(
                values.get(key, self.env.context.get("default_" + key))
                for key in protected
            ):
                raise AccessError(self.env._(
                    "Use the workflow buttons to record reviews and entries."
                ))
            closing = self.env["restaurant.daily.closing"].browse(values.get("closing_id"))
            if not closing or closing.state != "confirmed":
                raise ValidationError(self.env._("Only reviewed daily closings can enter Accounting."))
            values.update(self._snapshot_values(closing))
            values.update({
                "name": self.env["ir.sequence"].next_by_code(
                    "restaurant.accounting.daily.sales"
                ) or "New",
                "workflow_state": "pending",
            })
            values.pop("state", None)
            values.pop("journal_link_issue", None)
            values.update({key: False for key in protected})
            prepared.append(values)
        context = dict(self.env.context)
        context.pop("default_state", None)
        context.pop("default_journal_link_issue", None)
        return super(
            RestaurantAccountingDailySales, self.with_context(context)
        ).create(prepared)

    def write(self, vals):
        if self.env.context.get(DAILY_WORKFLOW_CONTEXT) is DAILY_WORKFLOW_SENTINEL:
            return super().write(vals)
        require_group(self.env, ACCOUNTANT_GROUP)
        if set(vals) & {"state", "workflow_state", "journal_link_issue"}:
            raise AccessError(self.env._("Use the accounting workflow buttons."))
        if set(vals) - {"review_note"}:
            raise AccessError(self.env._("Use the accounting workflow buttons."))
        return super().write(vals)

    def unlink(self):
        raise AccessError(self.env._("Daily accounting records are permanent audit records."))

    def _workflow_write(self, vals):
        return self._workflow_recordset().write(vals)

    def _workflow_recordset(self):
        return self.with_context(**{
            DAILY_WORKFLOW_CONTEXT: DAILY_WORKFLOW_SENTINEL,
        })

    @api.model
    def _sync_confirmed_closings(self):
        closings = self.env["restaurant.daily.closing"].search([
            ("state", "=", "confirmed"),
            ("id", "not in", self.search([]).mapped("closing_id").ids),
        ])
        if closings:
            self.with_context(**{DAILY_SYNC_CONTEXT: True}).create([
                {"closing_id": closing.id} for closing in closings
            ])
        return True

    def action_review(self):
        self.ensure_one()
        require_group(self.env, ACCOUNTANT_GROUP)
        self.check_access("write")
        self.lock_for_update()
        if self.state != "pending":
            raise UserError(self.env._("Only pending daily sales can be reviewed."))
        if not float_is_zero(
            self.payment_difference,
            precision_rounding=self.currency_id.rounding,
        ):
            raise ValidationError(self.env._("Resolve the sales-to-payment difference first."))
        if not float_is_zero(
            self.vat_difference,
            precision_rounding=self.currency_id.rounding,
        ) and not (self.review_note or "").strip():
            raise ValidationError(self.env._("Document the POS VAT difference before review."))
        self._workflow_write({
            "workflow_state": "reviewed",
            "reviewed_by_id": self.env.uid,
            "reviewed_at": fields.Datetime.now(),
        })
        return True

    def _configured_accounts(self):
        company = self.company_id
        settings = {
            "journal": company.restaurant_sales_journal_id,
            "sales": company.restaurant_sales_income_account_id,
            "vat": company.restaurant_vat_output_account_id,
            "cash": company.restaurant_cash_account_id,
            "card": company.restaurant_card_clearing_account_id,
            "delivery": company.restaurant_delivery_clearing_account_id,
            "other": company.restaurant_other_clearing_account_id,
            "tips": company.restaurant_tips_payable_account_id,
        }
        missing = [label for label, record in settings.items() if not record]
        if missing:
            raise ValidationError(self.env._(
                "Configure the Daily Sales Accounting accounts first: %s",
                ", ".join(missing),
            ))
        return settings

    def action_create_journal_entry(self):
        self.ensure_one()
        require_group(self.env, ACCOUNTANT_GROUP)
        self.check_access("write")
        self.lock_for_update()
        if self.workflow_state != "reviewed":
            raise UserError(self.env._("Review daily sales before creating the journal entry."))
        if self.journal_entry_id:
            raise UserError(self.env._("A journal entry already exists."))
        accounts = self._configured_accounts()
        lines = []

        def add_line(label, account, debit=0.0, credit=0.0):
            if float_is_zero(debit - credit, precision_rounding=self.currency_id.rounding):
                return
            lines.append(Command.create({
                "name": label,
                "account_id": account.id,
                "debit": debit,
                "credit": credit,
            }))

        add_line(self.env._("Cash sales"), accounts["cash"], debit=self.cash_sales)
        add_line(self.env._("Card sales"), accounts["card"], debit=self.card_sales)
        add_line(self.env._("Delivery platform sales"), accounts["delivery"], debit=self.delivery_sales)
        add_line(self.env._("Other payment sales"), accounts["other"], debit=self.other_sales)
        add_line(self.env._("Net restaurant sales"), accounts["sales"], credit=self.net_sales)
        add_line(self.env._("Output VAT"), accounts["vat"], credit=self.vat_amount)
        add_line(self.env._("Cash tips collected"), accounts["cash"], debit=self.cash_tips)
        add_line(self.env._("Card tips collected"), accounts["card"], debit=self.card_tips)
        add_line(self.env._("Delivery tips collected"), accounts["delivery"], debit=self.delivery_tips)
        add_line(self.env._("Other tips collected"), accounts["other"], debit=self.other_tips)
        add_line(self.env._("Employee tips payable"), accounts["tips"], credit=self.total_tips)
        if not lines:
            raise ValidationError(self.env._("There are no daily sales amounts to post."))
        move = self.env["account.move"].with_company(self.company_id).create({
            "move_type": "entry",
            "journal_id": accounts["journal"].id,
            "date": self.business_date,
            "ref": self.name,
            "line_ids": lines,
            "restaurant_daily_sales_id": self.id,
        })
        self._workflow_write({
            "journal_entry_id": move.id,
            "workflow_state": "draft_entry",
        })
        return {
            "type": "ir.actions.act_window",
            "res_model": "account.move",
            "res_id": move.id,
            "view_mode": "form",
        }

    def action_post_journal_entry(self):
        self.ensure_one()
        require_group(self.env, ACCOUNTANT_GROUP)
        self.check_access("write")
        self.lock_for_update()
        if self.state != "draft_entry" or not self.journal_entry_id:
            raise UserError(self.env._("Create the draft journal entry first."))
        if self.journal_entry_id.state != "draft":
            raise ValidationError(self.env._("The linked journal entry is no longer in Draft."))
        self.journal_entry_id.action_post()
        self._workflow_write({
            "posted_by_id": self.env.uid,
            "posted_at": fields.Datetime.now(),
        })
        return True

    def action_return_for_correction(self):
        self.ensure_one()
        require_group(self.env, ACCOUNTANT_GROUP)
        self.check_access("write")
        self.lock_for_update()
        if self.journal_entry_id:
            raise ValidationError(self.env._(
                "A linked journal entry exists. Resolve it through the normal "
                "Accounting reversal process before correcting the source."
            ))
        if self.workflow_state not in ("pending", "reviewed"):
            raise UserError(self.env._(
                "Only an unposted daily sale can be returned for correction."
            ))
        reason = (self.review_note or "").strip()
        if not reason:
            raise ValidationError(self.env._(
                "Enter the Reception correction reason in the review note first."
            ))
        closing = self.closing_id
        closing._return_from_accounting_for_correction(reason, self.env.user)
        self._workflow_write({
            "workflow_state": "correction",
            "returned_by_id": self.env.uid,
            "returned_at": fields.Datetime.now(),
            "correction_count": self.correction_count + 1,
            "last_correction_reason": reason,
        })
        self.message_post(body=self.env._(
            "Returned to Reception for correction by %s. Reason: %s",
            self.env.user.name,
            reason,
        ))
        return True

    def _resnapshot_after_correction(self):
        for daily in self:
            if daily.workflow_state != "correction":
                continue
            if daily.journal_entry_id:
                raise ValidationError(self.env._(
                    "A correction record cannot be refreshed while a journal "
                    "entry is linked."
                ))
            values = daily._snapshot_values(daily.closing_id)
            values.update({
                "workflow_state": "pending",
                "reviewed_by_id": False,
                "reviewed_at": False,
                "last_correction_applied_by_id": self.env.uid,
                "last_correction_applied_at": fields.Datetime.now(),
            })
            daily._workflow_write(values)
            daily.message_post(body=self.env._(
                "Reception correction was reviewed by Management. The "
                "Accounting snapshot was refreshed and returned to Pending."
            ))
        return True


class RestaurantDailyClosing(models.Model):
    _inherit = "restaurant.daily.closing"

    accounting_daily_sales_id = fields.One2many(
        "restaurant.accounting.daily.sales", "closing_id",
        string="Accounting Record", readonly=True,
    )

    def _return_from_accounting_for_correction(self, reason, accountant):
        self.ensure_one()
        require_group(self.env, ACCOUNTANT_GROUP)
        if self.state != "confirmed":
            raise UserError(self.env._(
                "Only a Management-reviewed closing can be returned by Accounting."
            ))
        if not reason:
            raise ValidationError(self.env._("Enter a correction reason first."))
        now = fields.Datetime.now()
        # Deliberately bypass the Reception transition matrix for this one
        # audited Accountant -> Reception correction transition. No ACL is
        # broadened and the public write API remains locked.
        models.Model.write(self.sudo(), {
            "state": "draft",
            "correction_reason": reason,
            "last_reopened_by_id": accountant.id,
            "last_reopened_at": now,
            "reopen_count": self.reopen_count + 1,
        })
        if self.waiter_sales_closing_id:
            self.waiter_sales_closing_id._open_from_accounting_for_correction(
                reason, accountant,
            )
        self.sudo().message_post(body=self.env._(
            "Closing returned by Accounting user %s for Reception correction. "
            "Reason: %s",
            accountant.name,
            reason,
        ))
        return True

    def write(self, vals):
        result = super().write(vals)
        if vals.get("state") == "confirmed":
            Accounting = self.env["restaurant.accounting.daily.sales"].sudo().with_context(
                **{DAILY_SYNC_CONTEXT: True}
            )
            existing = Accounting.search([("closing_id", "in", self.ids)])
            existing_closing_ids = set(existing.mapped("closing_id").ids)
            missing = self.filtered(
                lambda closing: closing.id not in existing_closing_ids
            )
            if missing:
                Accounting.create([{"closing_id": closing.id} for closing in missing])
            correction_records = existing.filtered(
                lambda daily: daily.workflow_state == "correction"
            )
            if correction_records:
                correction_records._resnapshot_after_correction()
        return result


class AccountMove(models.Model):
    _inherit = "account.move"

    restaurant_daily_sales_id = fields.Many2one(
        "restaurant.accounting.daily.sales",
        string="Restaurant Daily Sales Accounting",
        readonly=True,
        copy=False,
        index=True,
    )

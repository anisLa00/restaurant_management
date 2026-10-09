from odoo import Command, api, fields, models
from odoo.exceptions import AccessError, UserError, ValidationError
from odoo.tools import float_is_zero

from .restaurant_purchase_invoice_handoff import ACCOUNTANT_GROUP, require_group


DAILY_WORKFLOW_CONTEXT = "restaurant_daily_sales_accounting_workflow"
DAILY_SYNC_CONTEXT = "restaurant_daily_sales_accounting_sync"


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
    state = fields.Selection(
        [
            ("pending", "Pending Accounting Review"),
            ("reviewed", "Reviewed"),
            ("draft_entry", "Draft Journal Entry"),
            ("posted", "Posted"),
        ],
        required=True, default="pending", readonly=True, copy=False,
        tracking=True, index=True,
    )
    reviewed_by_id = fields.Many2one("res.users", readonly=True, copy=False)
    reviewed_at = fields.Datetime(readonly=True, copy=False)
    journal_entry_id = fields.Many2one(
        "account.move", readonly=True, copy=False, check_company=True,
        domain="[('move_type', '=', 'entry')]",
    )
    posted_by_id = fields.Many2one("res.users", readonly=True, copy=False)
    posted_at = fields.Datetime(readonly=True, copy=False)

    _closing_unique = models.Constraint(
        "UNIQUE(closing_id)",
        "A daily sales accounting record already exists for this closing.",
    )

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
            closing = self.env["restaurant.daily.closing"].browse(values.get("closing_id"))
            if not closing or closing.state != "confirmed":
                raise ValidationError(self.env._("Only reviewed daily closings can enter Accounting."))
            values.update(self._snapshot_values(closing))
            values.update({
                "name": self.env["ir.sequence"].next_by_code(
                    "restaurant.accounting.daily.sales"
                ) or "New",
                "state": "pending",
            })
            prepared.append(values)
        return super().create(prepared)

    def write(self, vals):
        if self.env.context.get(DAILY_WORKFLOW_CONTEXT):
            return super().write(vals)
        require_group(self.env, ACCOUNTANT_GROUP)
        if "state" in vals or set(vals) - {"review_note"}:
            raise AccessError(self.env._("Use the accounting workflow buttons."))
        return super().write(vals)

    def unlink(self):
        raise AccessError(self.env._("Daily accounting records are permanent audit records."))

    def _workflow_write(self, vals):
        return self.with_context(**{DAILY_WORKFLOW_CONTEXT: True}).write(vals)

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
            "state": "reviewed",
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
        if self.state != "reviewed":
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
            "state": "draft_entry",
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
            "state": "posted",
            "posted_by_id": self.env.uid,
            "posted_at": fields.Datetime.now(),
        })
        return True


class RestaurantDailyClosing(models.Model):
    _inherit = "restaurant.daily.closing"

    accounting_daily_sales_id = fields.One2many(
        "restaurant.accounting.daily.sales", "closing_id",
        string="Accounting Record", readonly=True,
    )

    def write(self, vals):
        result = super().write(vals)
        if vals.get("state") == "confirmed":
            Accounting = self.env["restaurant.accounting.daily.sales"].sudo().with_context(
                **{DAILY_SYNC_CONTEXT: True}
            )
            missing = self.filtered(lambda closing: not closing.accounting_daily_sales_id)
            if missing:
                Accounting.create([{"closing_id": closing.id} for closing in missing])
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

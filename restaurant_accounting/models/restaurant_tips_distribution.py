from collections import defaultdict
from datetime import date

from dateutil.relativedelta import relativedelta

from odoo import api, fields, models
from odoo.exceptions import AccessError, UserError, ValidationError


RECEPTION_GROUP = "restaurant_core.group_restaurant_reception"
ACCOUNTANT_GROUP = "restaurant_core.group_restaurant_accountant"
SYSTEM_GROUP = "base.group_system"


def _cycle_dates(value):
    current = fields.Date.to_date(value)
    if current.day >= 15:
        start = current.replace(day=15)
    else:
        start = (current - relativedelta(months=1)).replace(day=15)
    return start, start + relativedelta(months=1)


def _require_group(env, group):
    if not env.su and not env.user.has_group(group):
        raise AccessError(env._("Your role does not allow this operation."))


def _assigned_branch_ids(env):
    return env["restaurant.branch"].sudo().search([
        ("company_id", "in", env.companies.ids),
        ("user_ids", "in", [env.uid]),
        ("active", "=", True),
    ]).ids


class RestaurantTipsCollection(models.Model):
    _name = "restaurant.tips.collection"
    _description = "Branch Tips Collection Sheet"
    _inherit = ["mail.thread", "mail.activity.mixin"]
    _order = "cycle_start desc, branch_id, id desc"
    _mail_post_access = "read"

    name = fields.Char(
        string="Reference", required=True, readonly=True, copy=False,
        default="New", index=True,
    )
    branch_id = fields.Many2one(
        "restaurant.branch", required=True, ondelete="restrict", index=True,
        tracking=True,
    )
    company_id = fields.Many2one(
        related="branch_id.company_id", store=True, index=True,
    )
    currency_id = fields.Many2one(
        related="company_id.currency_id", store=True,
    )
    cycle_start = fields.Date(required=True, readonly=True, index=True)
    cycle_end = fields.Date(
        required=True, readonly=True, index=True,
        help="Exclusive end date. Transactions on this 15th enter the next cycle.",
    )
    state = fields.Selection([
        ("draft", "In Progress"),
        ("submitted", "Submitted to Accountant"),
    ], required=True, default="draft", readonly=True, copy=False,
        tracking=True, index=True)
    source_closing_ids = fields.Many2many(
        "restaurant.waiter.sales.closing",
        "restaurant_tips_collection_closing_rel",
        "collection_id", "closing_id",
        string="Closed Waiter Sales & Tips", readonly=True, copy=False,
    )
    waiter_line_ids = fields.One2many(
        "restaurant.tips.collection.waiter.line", "collection_id",
        string="Waiter Tips", readonly=True, copy=False,
    )
    closing_count = fields.Integer(readonly=True, copy=False)
    waiter_count = fields.Integer(readonly=True, copy=False)
    total_cash_tips = fields.Monetary(readonly=True, copy=False)
    total_card_tips = fields.Monetary(readonly=True, copy=False)
    total_tips = fields.Monetary(readonly=True, copy=False, tracking=True)
    reception_note = fields.Text(tracking=True)
    last_refreshed_by_id = fields.Many2one(
        "res.users", readonly=True, copy=False,
    )
    last_refreshed_at = fields.Datetime(readonly=True, copy=False)
    submitted_by_id = fields.Many2one(
        "res.users", readonly=True, copy=False,
    )
    submitted_at = fields.Datetime(readonly=True, copy=False)

    _branch_cycle_unique = models.Constraint(
        "UNIQUE(branch_id, cycle_start)",
        "Only one Tips Collection sheet is allowed per branch and cycle.",
    )
    _valid_cycle = models.Constraint(
        "CHECK (cycle_end > cycle_start)",
        "The Tips Collection cycle end must be after its start.",
    )

    @api.model
    def _default_cycle(self):
        return _cycle_dates(fields.Date.context_today(self))

    @api.model
    def _default_branch(self):
        branch_ids = _assigned_branch_ids(self.env)
        return branch_ids[0] if len(branch_ids) == 1 else False

    @api.model
    def _cron_prepare_current_cycle(self):
        """Create each branch sheet automatically and keep draft totals current."""
        cycle_start, _cycle_end = _cycle_dates(fields.Date.context_today(self))
        branches = self.env["restaurant.branch"].sudo().search([
            ("active", "=", True),
        ], order="company_id, name, id")
        for branch in branches:
            collection = self.sudo().search([
                ("branch_id", "=", branch.id),
                ("cycle_start", "=", cycle_start),
            ], limit=1)
            if not collection:
                self.sudo().with_context(
                    allowed_company_ids=[branch.company_id.id],
                ).create({
                    "branch_id": branch.id,
                    "cycle_start": cycle_start,
                })
            elif collection.state == "draft":
                collection.sudo().with_context(
                    allowed_company_ids=[branch.company_id.id],
                ).action_refresh()
        return True

    @api.model_create_multi
    def create(self, vals_list):
        _require_group(self.env, RECEPTION_GROUP)
        start, end = self._default_cycle()
        prepared = []
        assigned = set(_assigned_branch_ids(self.env))
        for vals in vals_list:
            values = dict(vals)
            branch_id = values.get("branch_id") or self._default_branch()
            if not branch_id:
                raise ValidationError(self.env._("Select your assigned branch."))
            if not self.env.su and branch_id not in assigned:
                raise AccessError(self.env._(
                    "Reception can prepare Tips Collection only for an assigned branch."
                ))
            cycle_start = fields.Date.to_date(values.get("cycle_start") or start)
            if cycle_start.day != 15:
                raise ValidationError(self.env._("A Tips Collection cycle must start on the 15th."))
            protected = {
                "name", "cycle_end", "state", "source_closing_ids",
                "waiter_line_ids", "closing_count", "waiter_count",
                "total_cash_tips", "total_card_tips", "total_tips",
                "last_refreshed_by_id", "last_refreshed_at",
                "submitted_by_id", "submitted_at", "company_id", "currency_id",
            }
            if set(values) & protected:
                raise AccessError(self.env._(
                    "Tips source totals, workflow, and audit fields are system-controlled."
                ))
            values.update({
                "name": self.env["ir.sequence"].next_by_code(
                    "restaurant.tips.collection"
                ) or "New",
                "branch_id": branch_id,
                "cycle_start": cycle_start,
                "cycle_end": cycle_start + relativedelta(months=1),
                "state": "draft",
            })
            prepared.append(values)
        records = super().create(prepared)
        for record in records:
            record.action_refresh()
        return records

    def write(self, vals):
        if self.env.context.get("tips_system_update") and self.env.su:
            return super().write(vals)
        self.check_access("write")
        _require_group(self.env, RECEPTION_GROUP)
        if any(record.state != "draft" for record in self):
            raise AccessError(self.env._("A submitted Tips Collection sheet is immutable."))
        if not self.env.su:
            assigned = set(_assigned_branch_ids(self.env))
            if any(record.branch_id.id not in assigned for record in self):
                raise AccessError(self.env._("You are not assigned to this branch."))
        if set(vals) - {"reception_note"}:
            raise AccessError(self.env._(
                "Reception can edit only the note; tips are generated automatically."
            ))
        return super().write(vals)

    def unlink(self):
        self.check_access("unlink")
        _require_group(self.env, RECEPTION_GROUP)
        if any(record.state != "draft" for record in self):
            raise AccessError(self.env._("A submitted Tips Collection sheet cannot be deleted."))
        if not self.env.su:
            assigned = set(_assigned_branch_ids(self.env))
            if any(record.branch_id.id not in assigned for record in self):
                raise AccessError(self.env._("You are not assigned to this branch."))
        return super().unlink()

    def _period_domain(self):
        self.ensure_one()
        return [
            ("branch_id", "=", self.branch_id.id),
            ("business_date", ">=", self.cycle_start),
            ("business_date", "<", self.cycle_end),
        ]

    def _refresh_values(self):
        self.ensure_one()
        closings = self.env["restaurant.waiter.sales.closing"].sudo().search(
            self._period_domain() + [("state", "=", "closed")],
            order="business_date, id",
        )
        summaries = self.env["restaurant.waiter.sales.summary"].sudo().search([
            ("waiter_closing_id", "in", closings.ids),
            ("summary_type", "=", "waiter"),
            ("employee_id", "!=", False),
        ], order="employee_id, id")
        aggregate = defaultdict(lambda: {
            "cash": 0.0, "card": 0.0, "tips": 0.0,
            "bills": 0, "closings": set(),
        })
        for summary in summaries:
            # Daily sharing reports show every configured waiter, including
            # zero rows. A zero row is presentation-only and must not create a
            # ranking candidate or a duplicate distribution entitlement.
            if not summary.bill_count:
                continue
            row = aggregate[summary.employee_id.id]
            row["cash"] += summary.cash_tip_amount
            row["card"] += summary.card_tip_amount
            row["tips"] += summary.tips_amount
            row["bills"] += summary.bill_count
            row["closings"].add(summary.waiter_closing_id.id)
        line_values = []
        for employee_id, row in sorted(aggregate.items()):
            line_values.append({
                "collection_id": self.id,
                "employee_id": employee_id,
                "cash_tips": row["cash"],
                "card_tips": row["card"],
                "total_tips": row["tips"],
                "bill_count": row["bills"],
                "source_closing_count": len(row["closings"]),
            })
        return closings, line_values

    def action_refresh(self):
        self.ensure_one()
        self.check_access("write")
        _require_group(self.env, RECEPTION_GROUP)
        if self.state != "draft":
            raise UserError(self.env._("A submitted Tips Collection sheet cannot be refreshed."))
        if not self.env.su and self.branch_id.id not in _assigned_branch_ids(self.env):
            raise AccessError(self.env._("You are not assigned to this branch."))
        closings, line_values = self._refresh_values()
        self.waiter_line_ids.sudo().with_context(tips_system_update=True).unlink()
        if line_values:
            self.env["restaurant.tips.collection.waiter.line"].sudo().with_context(
                tips_system_update=True,
            ).create(line_values)
        self.invalidate_recordset(["waiter_line_ids"])
        self.sudo().with_context(tips_system_update=True).write({
            "source_closing_ids": [(6, 0, closings.ids)],
            "closing_count": len(closings),
            "waiter_count": len(line_values),
            "total_cash_tips": sum(closings.mapped("cash_tips")),
            "total_card_tips": sum(closings.mapped("card_tips")),
            "total_tips": sum(closings.mapped("total_tips")),
            "last_refreshed_by_id": self.env.uid,
            "last_refreshed_at": fields.Datetime.now(),
        })
        return True

    def action_submit(self):
        self.ensure_one()
        self.check_access("write")
        _require_group(self.env, RECEPTION_GROUP)
        if self.state != "draft":
            raise UserError(self.env._("Only an in-progress Tips Collection can be submitted."))
        if fields.Date.context_today(self) < self.cycle_end:
            raise ValidationError(self.env._(
                "This 15-to-15 cycle is still open. Submit it on or after %(date)s.",
                date=self.cycle_end,
            ))
        pending = self.env["restaurant.waiter.sales.closing"].sudo().search_count(
            self._period_domain() + [("state", "in", ["draft", "submitted"])]
        )
        if pending:
            raise ValidationError(self.env._(
                "Close or cancel every Waiter Sales & Tips sheet in this cycle first. "
                "%(count)s sheet(s) are still open.", count=pending,
            ))
        self.action_refresh()
        self.sudo().with_context(tips_system_update=True).write({
            "state": "submitted",
            "submitted_by_id": self.env.uid,
            "submitted_at": fields.Datetime.now(),
        })
        self.message_post(body=self.env._(
            "Tips Collection submitted to Accounting by %s.", self.env.user.name,
        ))
        distribution_model = self.env["restaurant.tips.distribution"].sudo()
        distribution = distribution_model.search([
            ("company_id", "=", self.company_id.id),
            ("cycle_start", "=", self.cycle_start),
        ], limit=1)
        if not distribution:
            distribution = distribution_model.create({
                "company_id": self.company_id.id,
                "cycle_start": self.cycle_start,
            })
        active_branches = distribution._active_branches()
        submitted = distribution._submitted_collections()
        if set(active_branches.ids) == set(submitted.mapped("branch_id").ids):
            distribution._rebuild_source_snapshot()
        return True


class RestaurantTipsCollectionWaiterLine(models.Model):
    _name = "restaurant.tips.collection.waiter.line"
    _description = "Generated Branch Waiter Tips"
    _order = "total_tips desc, employee_id, id"

    collection_id = fields.Many2one(
        "restaurant.tips.collection", required=True, ondelete="cascade", index=True,
    )
    employee_id = fields.Many2one(
        "hr.employee", string="Waiter", required=True, readonly=True,
        ondelete="restrict", index=True,
    )
    branch_id = fields.Many2one(
        related="collection_id.branch_id", store=True, index=True,
    )
    company_id = fields.Many2one(
        related="collection_id.company_id", store=True, index=True,
    )
    currency_id = fields.Many2one(
        related="collection_id.currency_id", store=True,
    )
    cash_tips = fields.Monetary(readonly=True)
    card_tips = fields.Monetary(readonly=True)
    total_tips = fields.Monetary(readonly=True)
    bill_count = fields.Integer(readonly=True)
    source_closing_count = fields.Integer(readonly=True)

    _employee_collection_unique = models.Constraint(
        "UNIQUE(collection_id, employee_id)",
        "A waiter can appear only once in a branch Tips Collection.",
    )

    @api.model_create_multi
    def create(self, vals_list):
        if not (self.env.su and self.env.context.get("tips_system_update")):
            raise AccessError(self.env._("Waiter tips lines are generated automatically."))
        return super().create(vals_list)

    def write(self, vals):
        if not (self.env.su and self.env.context.get("tips_system_update")):
            raise AccessError(self.env._("Waiter tips lines are generated automatically."))
        return super().write(vals)

    def unlink(self):
        if not (self.env.su and self.env.context.get("tips_system_update")):
            raise AccessError(self.env._("Waiter tips lines are generated automatically."))
        return super().unlink()


class RestaurantTipsDistribution(models.Model):
    _name = "restaurant.tips.distribution"
    _description = "Global Tips Distribution Preparation"
    _inherit = ["mail.thread", "mail.activity.mixin"]
    _order = "cycle_start desc, id desc"
    _mail_post_access = "read"

    name = fields.Char(
        string="Reference", required=True, readonly=True, copy=False,
        default="New", index=True,
    )
    company_id = fields.Many2one(
        "res.company", required=True, default=lambda self: self.env.company,
        ondelete="restrict", index=True,
    )
    currency_id = fields.Many2one(
        related="company_id.currency_id", store=True,
    )
    cycle_start = fields.Date(required=True, readonly=True, index=True)
    cycle_end = fields.Date(
        required=True, readonly=True, index=True,
        help="Exclusive end date. Transactions on this 15th enter the next cycle.",
    )
    state = fields.Selection([
        ("draft", "Draft Preparation"),
        ("prepared", "Prepared for Review"),
    ], required=True, default="draft", readonly=True, copy=False,
        tracking=True, index=True)
    collection_ids = fields.Many2many(
        "restaurant.tips.collection",
        "restaurant_tips_distribution_collection_rel",
        "distribution_id", "collection_id",
        string="Submitted Branch Sheets", readonly=True, copy=False,
    )
    branch_line_ids = fields.One2many(
        "restaurant.tips.distribution.branch.line", "distribution_id",
        string="Branch Settings", copy=False,
    )
    ranking_line_ids = fields.One2many(
        "restaurant.tips.ranking.line", "distribution_id",
        string="Waiter Rankings", readonly=True, copy=False,
    )
    allocation_line_ids = fields.One2many(
        "restaurant.tips.allocation.line", "distribution_id",
        string="Prepared Staff Shares", readonly=True, copy=False,
    )
    extra_double_employee_ids = fields.Many2many(
        "hr.employee.public",
        "restaurant_tips_distribution_extra_double_rel",
        "distribution_id", "employee_id",
        string="Extra Double Share Staff",
        domain="[('active', '=', True), ('company_id', '=', company_id)]",
        help="Optional manual double-share selection. A reason is mandatory.",
    )
    extra_double_reason = fields.Text(
        string="Extra Double Reason",
        help="Required whenever Accounting selects extra double-share staff.",
    )
    global_pool = fields.Monetary(readonly=True, copy=False, tracking=True)
    total_units = fields.Integer(readonly=True, copy=False)
    ordinary_share = fields.Monetary(readonly=True, copy=False)
    allocation_total = fields.Monetary(readonly=True, copy=False)
    rounding_adjustment = fields.Monetary(readonly=True, copy=False)
    active_staff_count = fields.Integer(readonly=True, copy=False)
    top_waiter_count = fields.Integer(readonly=True, copy=False)
    prepared_by_id = fields.Many2one("res.users", readonly=True, copy=False)
    prepared_at = fields.Datetime(readonly=True, copy=False)
    last_refreshed_by_id = fields.Many2one("res.users", readonly=True, copy=False)
    last_refreshed_at = fields.Datetime(readonly=True, copy=False)

    _company_cycle_unique = models.Constraint(
        "UNIQUE(company_id, cycle_start)",
        "Only one Tips Distribution is allowed per company and cycle.",
    )
    _valid_cycle = models.Constraint(
        "CHECK (cycle_end > cycle_start)",
        "The Tips Distribution cycle end must be after its start.",
    )

    @api.model
    def _default_cycle(self):
        return _cycle_dates(fields.Date.context_today(self))

    @api.model_create_multi
    def create(self, vals_list):
        _require_group(self.env, ACCOUNTANT_GROUP)
        start, _end = self._default_cycle()
        prepared = []
        for vals in vals_list:
            values = dict(vals)
            protected = {
                "name", "cycle_end", "state", "collection_ids",
                "branch_line_ids", "ranking_line_ids", "allocation_line_ids",
                "global_pool", "total_units", "ordinary_share",
                "allocation_total", "rounding_adjustment", "active_staff_count",
                "top_waiter_count", "prepared_by_id", "prepared_at",
                "last_refreshed_by_id", "last_refreshed_at", "currency_id",
            }
            if set(values) & protected:
                raise AccessError(self.env._(
                    "Tips source data, calculated amounts, and audit fields are system-controlled."
                ))
            cycle_start = fields.Date.to_date(values.get("cycle_start") or start)
            if cycle_start.day != 15:
                raise ValidationError(self.env._("A Tips Distribution cycle must start on the 15th."))
            company_id = values.get("company_id") or self.env.company.id
            if not self.env.su and company_id not in self.env.companies.ids:
                raise AccessError(self.env._("Select an authorized company."))
            values.update({
                "name": self.env["ir.sequence"].next_by_code(
                    "restaurant.tips.distribution"
                ) or "New",
                "company_id": company_id,
                "cycle_start": cycle_start,
                "cycle_end": cycle_start + relativedelta(months=1),
                "state": "draft",
            })
            prepared.append(values)
        return super().create(prepared)

    def write(self, vals):
        if self.env.context.get("tips_system_update") and self.env.su:
            return super().write(vals)
        self.check_access("write")
        _require_group(self.env, ACCOUNTANT_GROUP)
        if any(record.state != "draft" for record in self):
            raise AccessError(self.env._("A prepared Tips Distribution is immutable."))
        if set(vals) - {"extra_double_employee_ids", "extra_double_reason"}:
            raise AccessError(self.env._(
                "Only the audited extra-double selection can be edited manually."
            ))
        result = super().write(vals)
        for record in self:
            invalid = record.extra_double_employee_ids.filtered(
                lambda employee: not employee.active or employee.company_id != record.company_id
            )
            if invalid:
                raise ValidationError(self.env._(
                    "Extra double-share staff must be active employees of this company."
                ))
        return result

    def unlink(self):
        self.check_access("unlink")
        _require_group(self.env, ACCOUNTANT_GROUP)
        if any(record.state != "draft" for record in self):
            raise AccessError(self.env._("A prepared Tips Distribution cannot be deleted."))
        return super().unlink()

    def _active_branches(self):
        self.ensure_one()
        return self.env["restaurant.branch"].sudo().search([
            ("company_id", "=", self.company_id.id),
            ("active", "=", True),
        ], order="name, id")

    def _submitted_collections(self):
        self.ensure_one()
        return self.env["restaurant.tips.collection"].sudo().search([
            ("company_id", "=", self.company_id.id),
            ("cycle_start", "=", self.cycle_start),
            ("cycle_end", "=", self.cycle_end),
            ("state", "=", "submitted"),
            ("branch_id.active", "=", True),
        ], order="branch_id, id")

    def _check_all_branches_submitted(self, branches, collections):
        submitted_branch_ids = set(collections.mapped("branch_id").ids)
        missing = branches.filtered(lambda branch: branch.id not in submitted_branch_ids)
        if missing:
            raise ValidationError(self.env._(
                "Every active branch must submit its Tips Collection first. Missing: %(branches)s",
                branches=", ".join(missing.mapped("name")),
            ))

    def _previous_top_counts(self):
        self.ensure_one()
        counts = {
            line.branch_id.id: line.top_waiter_count
            for line in self.branch_line_ids.sudo()
        }
        if counts:
            return counts
        previous = self.sudo().search([
            ("company_id", "=", self.company_id.id),
            ("cycle_start", "<", self.cycle_start),
        ], order="cycle_start desc, id desc", limit=1)
        if previous:
            counts.update({
                line.branch_id.id: line.top_waiter_count
                for line in previous.branch_line_ids
            })
        return counts

    def _attendance_days(self, employee_ids):
        self.ensure_one()
        days = defaultdict(set)
        if not employee_ids:
            return {}
        entries = self.env["restaurant.attendance.entry"].sudo().search([
            ("employee_id", "in", employee_ids),
            ("attendance_date", ">=", self.cycle_start),
            ("attendance_date", "<", self.cycle_end),
            ("state", "=", "approved"),
            ("status", "in", ["present", "late", "overtime_day"]),
        ])
        for entry in entries:
            days[entry.employee_id.id].add(entry.attendance_date)
        return {employee_id: len(values) for employee_id, values in days.items()}

    def _build_rankings(self, collections, top_counts):
        self.ensure_one()
        result = []
        lines_by_branch = defaultdict(list)
        for collection in collections:
            for line in collection.waiter_line_ids.filtered("employee_id.active"):
                if line.employee_id.company_id == self.company_id:
                    lines_by_branch[collection.branch_id.id].append(line)
        employee_ids = [
            line.employee_id.id
            for lines in lines_by_branch.values()
            for line in lines
        ]
        attendance = self._attendance_days(employee_ids)
        for branch_id, lines in lines_by_branch.items():
            ranked = sorted(lines, key=lambda line: (
                -line.total_tips,
                -attendance.get(line.employee_id.id, 0),
                (line.employee_id.name or "").lower(),
                line.employee_id.id,
            ))
            prior_score = None
            rank = 0
            for position, line in enumerate(ranked, start=1):
                score = (line.total_tips, attendance.get(line.employee_id.id, 0))
                if score != prior_score:
                    rank = position
                    prior_score = score
                result.append({
                    "distribution_id": self.id,
                    "branch_id": branch_id,
                    "employee_id": line.employee_id.id,
                    "collected_tips": line.total_tips,
                    "attendance_days": attendance.get(line.employee_id.id, 0),
                    "rank": rank,
                    "is_top": rank <= top_counts.get(branch_id, 3),
                })
        return result

    def _rebuild_source_snapshot(self):
        self.ensure_one()
        branches = self._active_branches()
        collections = self._submitted_collections()
        self._check_all_branches_submitted(branches, collections)
        top_counts = self._previous_top_counts()
        self.branch_line_ids.sudo().with_context(tips_system_update=True).unlink()
        branch_values = []
        by_branch = {collection.branch_id.id: collection for collection in collections}
        for branch in branches:
            collection = by_branch[branch.id]
            branch_values.append({
                "distribution_id": self.id,
                "branch_id": branch.id,
                "collection_id": collection.id,
                "collected_tips": collection.total_tips,
                "waiter_count": collection.waiter_count,
                "top_waiter_count": top_counts.get(branch.id, 3),
            })
        if branch_values:
            self.env["restaurant.tips.distribution.branch.line"].sudo().with_context(
                tips_system_update=True,
            ).create(branch_values)
        self.invalidate_recordset(["branch_line_ids"])
        self.ranking_line_ids.sudo().with_context(tips_system_update=True).unlink()
        ranking_values = self._build_rankings(collections, {
            line.branch_id.id: line.top_waiter_count
            for line in self.branch_line_ids.sudo()
        })
        if ranking_values:
            self.env["restaurant.tips.ranking.line"].sudo().with_context(
                tips_system_update=True,
            ).create(ranking_values)
        self.invalidate_recordset(["ranking_line_ids"])
        self.sudo().with_context(tips_system_update=True).write({
            "collection_ids": [(6, 0, collections.ids)],
            "global_pool": sum(collections.mapped("total_tips")),
            "last_refreshed_by_id": self.env.uid,
            "last_refreshed_at": fields.Datetime.now(),
        })
        return collections

    def action_refresh(self):
        self.ensure_one()
        self.check_access("write")
        _require_group(self.env, ACCOUNTANT_GROUP)
        if self.state != "draft":
            raise UserError(self.env._("A prepared Tips Distribution cannot be refreshed."))
        self._rebuild_source_snapshot()
        return True

    def _allocation_values(self):
        self.ensure_one()
        employees = self.env["hr.employee"].sudo().search([
            ("company_id", "=", self.company_id.id),
            ("active", "=", True),
        ], order="name, id")
        if not employees:
            raise ValidationError(self.env._("There are no active employees to distribute tips to."))
        top_by_employee = {
            line.employee_id.id: line
            for line in self.ranking_line_ids.filtered("is_top")
        }
        extra_ids = set(self.extra_double_employee_ids.ids)
        top_ids = set(top_by_employee)
        total_units = len(employees) + len(top_ids | extra_ids)
        ordinary = self.global_pool / total_units if total_units else 0.0
        values = []
        for employee in employees:
            top_line = top_by_employee.get(employee.id)
            is_extra = employee.id in extra_ids
            weight = 2 if top_line or is_extra else 1
            values.append({
                "distribution_id": self.id,
                "employee_id": employee.id,
                "branch_id": employee.restaurant_branch_id.id or False,
                "waiter_rank": top_line.rank if top_line else 0,
                "is_top_waiter": bool(top_line),
                "is_manual_double": is_extra,
                "share_weight": weight,
                "amount": self.currency_id.round(ordinary * weight),
            })
        allocated = sum(item["amount"] for item in values)
        residual = self.currency_id.round(self.global_pool - allocated)
        if residual and values:
            target_employee_id = False
            if top_by_employee:
                target_employee_id = min(
                    top_by_employee.values(),
                    key=lambda line: (
                        line.rank,
                        -line.collected_tips,
                        line.employee_id.id,
                    ),
                ).employee_id.id
            target = next(
                (item for item in values if item["employee_id"] == target_employee_id),
                values[0],
            )
            target["amount"] = self.currency_id.round(target["amount"] + residual)
        return employees, values, total_units, ordinary, residual

    def action_prepare(self):
        self.ensure_one()
        self.check_access("write")
        _require_group(self.env, ACCOUNTANT_GROUP)
        if self.state != "draft":
            raise UserError(self.env._("Only a draft Tips Distribution can be prepared."))
        if self.extra_double_employee_ids and not (self.extra_double_reason or "").strip():
            raise ValidationError(self.env._(
                "Enter the reason for every manual extra double-share selection."
            ))
        self._rebuild_source_snapshot()
        employees, values, total_units, ordinary, residual = self._allocation_values()
        self.allocation_line_ids.sudo().with_context(tips_system_update=True).unlink()
        self.env["restaurant.tips.allocation.line"].sudo().with_context(
            tips_system_update=True,
        ).create(values)
        self.invalidate_recordset(["allocation_line_ids"])
        self.sudo().with_context(tips_system_update=True).write({
            "state": "prepared",
            "total_units": total_units,
            "ordinary_share": ordinary,
            "allocation_total": sum(self.allocation_line_ids.mapped("amount")),
            "rounding_adjustment": residual,
            "active_staff_count": len(employees),
            "top_waiter_count": len(self.ranking_line_ids.filtered("is_top")),
            "prepared_by_id": self.env.uid,
            "prepared_at": fields.Datetime.now(),
        })
        self.message_post(body=self.env._(
            "Tips distribution prepared by %(user)s. This is a calculation only; "
            "no payout was recorded.", user=self.env.user.name,
        ))
        return True


class RestaurantTipsDistributionBranchLine(models.Model):
    _name = "restaurant.tips.distribution.branch.line"
    _description = "Tips Distribution Branch Setting"
    _order = "branch_id, id"

    distribution_id = fields.Many2one(
        "restaurant.tips.distribution", required=True, ondelete="cascade", index=True,
    )
    branch_id = fields.Many2one(
        "restaurant.branch", required=True, readonly=True,
        ondelete="restrict", index=True,
    )
    collection_id = fields.Many2one(
        "restaurant.tips.collection", required=True, readonly=True,
        ondelete="restrict",
    )
    company_id = fields.Many2one(
        related="distribution_id.company_id", store=True, index=True,
    )
    currency_id = fields.Many2one(
        related="distribution_id.currency_id", store=True,
    )
    collected_tips = fields.Monetary(readonly=True)
    waiter_count = fields.Integer(readonly=True)
    top_waiter_count = fields.Integer(
        string="Top Waiters", required=True, default=3,
        help="Waiters at or above this branch rank receive a double share. Ties are included.",
    )

    _branch_distribution_unique = models.Constraint(
        "UNIQUE(distribution_id, branch_id)",
        "A branch can appear only once in a Tips Distribution.",
    )
    _positive_top_count = models.Constraint(
        "CHECK (top_waiter_count > 0)",
        "Top Waiters must be at least one.",
    )

    @api.model_create_multi
    def create(self, vals_list):
        if not (self.env.su and self.env.context.get("tips_system_update")):
            raise AccessError(self.env._("Branch distribution rows are generated automatically."))
        return super().create(vals_list)

    def write(self, vals):
        if self.env.su and self.env.context.get("tips_system_update"):
            return super().write(vals)
        self.check_access("write")
        _require_group(self.env, ACCOUNTANT_GROUP)
        if any(line.distribution_id.state != "draft" for line in self):
            raise AccessError(self.env._("Prepared branch settings are immutable."))
        if set(vals) != {"top_waiter_count"}:
            raise AccessError(self.env._("Accounting can edit only the number of Top Waiters."))
        return super().write(vals)

    def unlink(self):
        if not (self.env.su and self.env.context.get("tips_system_update")):
            raise AccessError(self.env._("Branch distribution rows are generated automatically."))
        return super().unlink()


class RestaurantTipsRankingLine(models.Model):
    _name = "restaurant.tips.ranking.line"
    _description = "Generated Branch Waiter Ranking"
    _order = "branch_id, rank, collected_tips desc, employee_id, id"

    distribution_id = fields.Many2one(
        "restaurant.tips.distribution", required=True, ondelete="cascade", index=True,
    )
    branch_id = fields.Many2one(
        "restaurant.branch", required=True, readonly=True,
        ondelete="restrict", index=True,
    )
    employee_id = fields.Many2one(
        "hr.employee", string="Waiter", required=True, readonly=True,
        ondelete="restrict", index=True,
    )
    company_id = fields.Many2one(
        related="distribution_id.company_id", store=True, index=True,
    )
    currency_id = fields.Many2one(
        related="distribution_id.currency_id", store=True,
    )
    collected_tips = fields.Monetary(readonly=True)
    attendance_days = fields.Integer(readonly=True)
    rank = fields.Integer(readonly=True)
    is_top = fields.Boolean(string="Double Share", readonly=True)

    _employee_distribution_unique = models.Constraint(
        "UNIQUE(distribution_id, employee_id)",
        "A waiter can appear only once in a Tips Distribution ranking.",
    )

    @api.model_create_multi
    def create(self, vals_list):
        if not (self.env.su and self.env.context.get("tips_system_update")):
            raise AccessError(self.env._("Waiter rankings are generated automatically."))
        return super().create(vals_list)

    def write(self, vals):
        if not (self.env.su and self.env.context.get("tips_system_update")):
            raise AccessError(self.env._("Waiter rankings are generated automatically."))
        return super().write(vals)

    def unlink(self):
        if not (self.env.su and self.env.context.get("tips_system_update")):
            raise AccessError(self.env._("Waiter rankings are generated automatically."))
        return super().unlink()


class RestaurantTipsAllocationLine(models.Model):
    _name = "restaurant.tips.allocation.line"
    _description = "Prepared Employee Tips Share"
    _order = "branch_id, employee_id, id"

    distribution_id = fields.Many2one(
        "restaurant.tips.distribution", required=True, ondelete="cascade", index=True,
    )
    employee_id = fields.Many2one(
        "hr.employee", required=True, readonly=True,
        ondelete="restrict", index=True,
    )
    branch_id = fields.Many2one(
        "restaurant.branch", readonly=True, ondelete="restrict", index=True,
    )
    company_id = fields.Many2one(
        related="distribution_id.company_id", store=True, index=True,
    )
    currency_id = fields.Many2one(
        related="distribution_id.currency_id", store=True,
    )
    waiter_rank = fields.Integer(readonly=True)
    is_top_waiter = fields.Boolean(readonly=True)
    is_manual_double = fields.Boolean(readonly=True)
    share_weight = fields.Integer(readonly=True)
    amount = fields.Monetary(readonly=True)

    _employee_distribution_unique = models.Constraint(
        "UNIQUE(distribution_id, employee_id)",
        "An employee can receive only one prepared share per Tips Distribution.",
    )

    @api.model_create_multi
    def create(self, vals_list):
        if not (self.env.su and self.env.context.get("tips_system_update")):
            raise AccessError(self.env._("Tips shares are generated automatically."))
        return super().create(vals_list)

    def write(self, vals):
        if not (self.env.su and self.env.context.get("tips_system_update")):
            raise AccessError(self.env._("Tips shares are generated automatically."))
        return super().write(vals)

    def unlink(self):
        if not (self.env.su and self.env.context.get("tips_system_update")):
            raise AccessError(self.env._("Tips shares are generated automatically."))
        return super().unlink()


class RestaurantWaiterSalesClosing(models.Model):
    _inherit = "restaurant.waiter.sales.closing"

    def write(self, vals):
        result = super().write(vals)
        if vals.get("state") == "closed":
            for closing in self:
                collections = self.env["restaurant.tips.collection"].sudo().search([
                    ("branch_id", "=", closing.branch_id.id),
                    ("cycle_start", "<=", closing.business_date),
                    ("cycle_end", ">", closing.business_date),
                    ("state", "=", "draft"),
                ])
                for collection in collections:
                    collection.sudo().action_refresh()
        return result

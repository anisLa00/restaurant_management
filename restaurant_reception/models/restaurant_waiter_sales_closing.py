from collections import defaultdict
from io import BytesIO
from pathlib import Path
from xml.sax.saxutils import escape

from odoo import api, fields, models
from odoo.exceptions import AccessError, UserError, ValidationError
from odoo.tools.misc import format_date
from reportlab.lib import colors
from reportlab.lib.enums import TA_CENTER
from reportlab.lib.pagesizes import A3, A4, landscape
from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
from reportlab.lib.units import mm
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont
from reportlab.platypus import Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle

from .reception_security import (
    MANAGER_GROUP,
    RECEPTION_GROUP,
    check_employee_company,
    lock_records,
    require_assigned_branches,
    require_branch_manager_review,
    require_draft,
    require_role,
)


class RestaurantWaiterSalesClosing(models.Model):
    _name = "restaurant.waiter.sales.closing"
    _description = "Waiter Sales and Tips Closing"
    _inherit = ["mail.thread", "mail.activity.mixin"]
    _order = "business_date desc, id desc"
    _mail_post_access = "read"

    name = fields.Char(
        string="Reference",
        required=True,
        readonly=True,
        copy=False,
        default="New",
        index=True,
    )
    branch_id = fields.Many2one(
        "restaurant.branch",
        required=True,
        ondelete="restrict",
        index=True,
        tracking=True,
    )
    business_date = fields.Date(
        required=True,
        default=fields.Date.context_today,
        index=True,
        tracking=True,
    )
    state = fields.Selection(
        [
            ("draft", "Draft"),
            ("correction", "Correction"),
            ("submitted", "Submitted"),
            ("closed", "Closed"),
            ("cancelled", "Cancelled"),
        ],
        required=True,
        default="draft",
        readonly=True,
        copy=False,
        tracking=True,
        index=True,
    )
    reception_user_id = fields.Many2one(
        "res.users",
        string="Prepared By",
        required=True,
        readonly=True,
        default=lambda self: self.env.user,
    )
    bill_ids = fields.One2many(
        "restaurant.waiter.sales.bill",
        "waiter_closing_id",
        string="Bills",
        copy=True,
    )
    summary_line_ids = fields.One2many(
        "restaurant.waiter.sales.summary",
        "waiter_closing_id",
        string="Waiter Summary",
        readonly=True,
        copy=False,
    )
    total_sales = fields.Monetary(
        string="Total Sales (VAT Included)",
        compute="_compute_totals", store=True, readonly=True, tracking=True,
        help="Sum of final receipt totals. VAT is already included and is not added again.",
    )
    dine_in_sales = fields.Monetary(
        string="Dine-in Sales (VAT Included)",
        compute="_compute_totals", store=True, readonly=True,
    )
    pickup_sales = fields.Monetary(
        string="Pickup Sales (VAT Included)",
        compute="_compute_totals", store=True, readonly=True,
    )
    delivery_sales = fields.Monetary(
        string="Delivery Sales (VAT Included)",
        compute="_compute_totals", store=True, readonly=True,
    )
    pickup_delivery_sales = fields.Monetary(
        string="Pickup / Delivery Sales (VAT Included)",
        compute="_compute_totals", store=True, readonly=True,
    )
    waiter_sales = fields.Monetary(
        string="Waiter Sales (VAT Included)",
        compute="_compute_totals",
        store=True,
        readonly=True,
    )
    cash_tips = fields.Monetary(
        compute="_compute_totals", store=True, readonly=True,
    )
    card_tips = fields.Monetary(
        compute="_compute_totals", store=True, readonly=True,
    )
    total_tips = fields.Monetary(
        compute="_compute_totals", store=True, readonly=True, tracking=True,
    )
    waiter_tip_contribution = fields.Monetary(
        compute="_compute_totals", store=True, readonly=True,
    )
    guest_count = fields.Integer(
        compute="_compute_totals", store=True, readonly=True,
    )
    bill_count = fields.Integer(
        compute="_compute_totals", store=True, readonly=True,
    )
    dine_in_bill_count = fields.Integer(
        compute="_compute_totals", store=True, readonly=True,
    )
    pickup_bill_count = fields.Integer(
        compute="_compute_totals", store=True, readonly=True,
    )
    delivery_bill_count = fields.Integer(
        compute="_compute_totals", store=True, readonly=True,
    )
    review_count = fields.Integer(
        string="Reviews",
        compute="_compute_totals",
        store=True,
        readonly=True,
    )
    notes = fields.Text(tracking=True)
    submitted_by_id = fields.Many2one(
        "res.users", string="Submitted By", readonly=True, copy=False,
    )
    submitted_at = fields.Datetime(readonly=True, copy=False)
    closed_by_id = fields.Many2one(
        "res.users", string="Closed By", readonly=True, copy=False,
    )
    closed_at = fields.Datetime(readonly=True, copy=False)
    correction_reason = fields.Text(
        copy=False,
        help="Required before management or Accounting opens a correction.",
    )
    last_correction_reason = fields.Text(readonly=True, copy=False)
    last_reopened_by_id = fields.Many2one(
        "res.users", string="Last Reopened By", readonly=True, copy=False,
    )
    last_reopened_at = fields.Datetime(readonly=True, copy=False)
    reopen_count = fields.Integer(readonly=True, copy=False)
    first_closed_by_id = fields.Many2one(
        "res.users", string="First Closed By", readonly=True, copy=False,
    )
    first_closed_at = fields.Datetime(readonly=True, copy=False)
    cancelled_by_id = fields.Many2one(
        "res.users", string="Cancelled By", readonly=True, copy=False,
    )
    cancelled_at = fields.Datetime(readonly=True, copy=False)
    company_id = fields.Many2one(
        related="branch_id.company_id", store=True, index=True,
    )
    currency_id = fields.Many2one(
        related="company_id.currency_id", store=True,
    )

    _active_waiter_closing_unique = models.UniqueIndex(
        "(branch_id, business_date) WHERE state != 'cancelled'",
        "An active Waiter Sales & Tips closing already exists for this branch and date.",
    )

    @api.depends(
        "bill_ids.service_type",
        "bill_ids.is_daily_total",
        "bill_ids.bill_count",
        "bill_ids.table_count",
        "bill_ids.bill_amount",
        "bill_ids.guest_count",
        "bill_ids.cash_tip_amount",
        "bill_ids.card_tip_amount",
        "bill_ids.review_count",
    )
    def _compute_totals(self):
        for closing in self:
            bills = closing.bill_ids
            dine_in = bills.filtered(lambda bill: bill.service_type == "dine_in")
            pickup = bills.filtered(lambda bill: bill.service_type == "pickup")
            delivery = bills.filtered(lambda bill: bill.service_type == "delivery")
            closing.dine_in_sales = sum(dine_in.mapped("bill_amount"))
            closing.pickup_sales = sum(pickup.mapped("bill_amount"))
            closing.delivery_sales = sum(delivery.mapped("bill_amount"))
            closing.pickup_delivery_sales = (
                closing.pickup_sales + closing.delivery_sales
            )
            closing.waiter_sales = closing.dine_in_sales
            closing.total_sales = sum(bills.mapped("bill_amount"))
            closing.cash_tips = sum(bills.mapped("cash_tip_amount"))
            closing.card_tips = sum(bills.mapped("card_tip_amount"))
            closing.total_tips = closing.cash_tips + closing.card_tips
            closing.waiter_tip_contribution = sum(dine_in.mapped("tip_total"))
            closing.guest_count = sum(dine_in.mapped("guest_count"))
            closing.bill_count = sum(
                bill.bill_count if bill.is_daily_total else 1 for bill in bills
            )
            closing.dine_in_bill_count = sum(
                bill.bill_count if bill.is_daily_total else 1 for bill in dine_in
            )
            closing.pickup_bill_count = sum(
                bill.bill_count if bill.is_daily_total else 1 for bill in pickup
            )
            closing.delivery_bill_count = sum(
                bill.bill_count if bill.is_daily_total else 1 for bill in delivery
            )
            closing.review_count = sum(bills.mapped("review_count"))

    @api.model_create_multi
    def create(self, vals_list):
        require_role(self.env, RECEPTION_GROUP)
        protected = {
            "name", "state", "reception_user_id", "summary_line_ids",
            "total_sales", "dine_in_sales", "pickup_sales",
            "delivery_sales", "pickup_delivery_sales", "waiter_sales",
            "cash_tips", "card_tips", "total_tips",
            "waiter_tip_contribution", "guest_count", "bill_count",
            "dine_in_bill_count", "pickup_bill_count",
            "delivery_bill_count", "review_count", "submitted_by_id",
            "submitted_at", "closed_by_id", "closed_at",
            "last_reopened_by_id", "last_reopened_at", "reopen_count",
            "last_correction_reason", "first_closed_by_id", "first_closed_at",
            "cancelled_by_id", "cancelled_at", "company_id", "currency_id",
        }
        prepared = []
        for values in vals_list:
            if set(values) & protected:
                raise AccessError(
                    self.env._("Workflow, audit, and calculated fields are system-controlled.")
                )
            values = dict(values)
            values.update({
                "name": self.env["ir.sequence"].next_by_code(
                    "restaurant.waiter.sales.closing"
                ),
                "state": "draft",
                "reception_user_id": self.env.uid,
            })
            prepared.append(values)
        records = super().create(prepared)
        require_assigned_branches(records.branch_id)
        records._sync_daily_rows()
        records._rebuild_summaries()
        records._sync_daily_closing_links()
        return records

    def _require_editable(self):
        if any(closing.state not in ("draft", "correction") for closing in self):
            raise AccessError(self.env._(
                "Waiter Sales & Tips can be edited only in Draft or Correction."
            ))
        return True

    def write(self, vals):
        lock_records(self)
        if "state" in vals:
            if set(vals) != {"state"}:
                raise AccessError(
                    self.env._("Save your changes before changing the workflow state.")
                )
            target = vals["state"]
            for closing in self:
                closing._check_transition(target)
            for closing in self:
                values = {"state": target}
                now = fields.Datetime.now()
                correction_reason = closing.correction_reason
                if target == "submitted":
                    values.update(submitted_by_id=self.env.uid, submitted_at=now)
                elif target == "closed":
                    values.update(closed_by_id=self.env.uid, closed_at=now)
                    if not closing.first_closed_at:
                        values.update(
                            first_closed_by_id=self.env.uid,
                            first_closed_at=now,
                        )
                elif target == "draft":
                    values.update(
                        last_reopened_by_id=self.env.uid,
                        last_reopened_at=now,
                        reopen_count=closing.reopen_count + 1,
                        correction_reason=False,
                    )
                elif target == "correction":
                    values.update(
                        last_reopened_by_id=self.env.uid,
                        last_reopened_at=now,
                        reopen_count=closing.reopen_count + 1,
                        last_correction_reason=correction_reason,
                        correction_reason=False,
                        submitted_by_id=False,
                        submitted_at=False,
                        closed_by_id=False,
                        closed_at=False,
                    )
                elif target == "cancelled":
                    values.update(cancelled_by_id=self.env.uid, cancelled_at=now)
                super(RestaurantWaiterSalesClosing, closing).write(values)
                if target in ("draft", "correction"):
                    closing.message_post(body=self.env._(
                        "Waiter closing reopened by %s. Reason: %s",
                        self.env.user.name,
                        correction_reason,
                    ))
                closing.message_post(body=self.env._(
                    "Workflow changed to %s by %s.",
                    dict(self._fields["state"].selection)[target],
                    self.env.user.name,
                ))
            self.flush_recordset(["state"])
            self._sync_daily_closing_links()
            return True

        if (
            all(closing.state in ("submitted", "closed") for closing in self)
            and self.env.user.has_group(MANAGER_GROUP)
            and not self.env.user.has_group(
                "restaurant_core.group_restaurant_operations_manager"
            )
            and not self.env.user.has_group(
                "restaurant_core.group_restaurant_owner"
            )
        ):
            if set(vals) - {"correction_reason"}:
                raise AccessError(
                    self.env._("Management can only enter the correction reason.")
                )
            require_assigned_branches(self.branch_id)
            return super().write(vals)

        if set(vals) - {"branch_id", "business_date", "bill_ids", "notes"}:
            raise AccessError(
                self.env._("Workflow, audit, and calculated fields cannot be changed manually.")
            )
        require_role(self.env, RECEPTION_GROUP)
        self._require_editable()
        require_assigned_branches(self.branch_id)
        if "branch_id" in vals:
            require_assigned_branches(
                self.env["restaurant.branch"].browse(vals["branch_id"])
            )
        linked_daily_closings = self.env["restaurant.daily.closing"].sudo().search([
            ("waiter_sales_closing_id", "in", self.ids),
        ])
        result = super().write(vals)
        if "branch_id" in vals:
            self.bill_ids.filtered("employee_id")._check_service_details()
            self._rebuild_summaries()
        if set(vals) & {"branch_id", "business_date"}:
            linked_daily_closings.with_context(waiter_closing_sync=True).write({
                "waiter_sales_closing_id": False,
            })
            self._sync_daily_closing_links()
        return result

    def unlink(self):
        lock_records(self, "unlink")
        require_role(self.env, RECEPTION_GROUP)
        require_draft(self)
        require_assigned_branches(self.branch_id)
        linked = self.env["restaurant.daily.closing"].sudo().search([
            ("waiter_sales_closing_id", "in", self.ids),
        ])
        linked.with_context(waiter_closing_sync=True).write({
            "waiter_sales_closing_id": False,
        })
        return super().unlink()

    def _check_transition(self, target):
        self.ensure_one()
        transitions = {
            ("draft", "submitted"): RECEPTION_GROUP,
            ("correction", "submitted"): RECEPTION_GROUP,
            ("submitted", "closed"): MANAGER_GROUP,
            ("submitted", "draft"): MANAGER_GROUP,
            ("closed", "correction"): MANAGER_GROUP,
            ("draft", "cancelled"): RECEPTION_GROUP,
            ("submitted", "cancelled"): MANAGER_GROUP,
        }
        group = transitions.get((self.state, target))
        if not group:
            raise UserError(self.env._("This waiter closing transition is not allowed."))
        if group == MANAGER_GROUP:
            require_branch_manager_review(self.env)
        else:
            require_role(self.env, group)
        require_assigned_branches(self.branch_id)
        if (
            self.state in ("draft", "correction")
            and target == "submitted"
            and not any(
                not line.is_daily_total
                or line.bill_count
                or line.table_count
                or line.guest_count
                or line.bill_amount
                or line.tip_total
                or line.review_count
                for line in self.bill_ids
            )
        ):
            raise ValidationError(
                self.env._(
                    "Enter the daily sales, bills, tables, tips, or reviews before submitting."
                )
            )
        if (
            (self.state, target) in (("submitted", "draft"), ("closed", "correction"))
            and not (self.correction_reason or "").strip()
        ):
            raise ValidationError(
                self.env._("Enter a correction reason before returning to Draft.")
            )
        if self.state == "closed" and target == "correction":
            linked_daily = self.env["restaurant.daily.closing"].sudo().search([
                ("waiter_sales_closing_id", "=", self.id),
                ("state", "!=", "cancelled"),
            ])
            if any(daily.state == "confirmed" for daily in linked_daily):
                raise ValidationError(self.env._(
                    "This day is already with Accounting. Accounting must use "
                    "Return to Reception for Correction first."
                ))

    def action_submit(self):
        self.ensure_one()
        return self.write({"state": "submitted"})

    def action_close(self):
        self.ensure_one()
        return self.write({"state": "closed"})

    def action_open_correction(self):
        self.ensure_one()
        return self.write({"state": "correction"})

    def action_return_to_draft(self):
        self.ensure_one()
        return self.write({"state": "draft"})

    def action_cancel(self):
        self.ensure_one()
        return self.write({"state": "cancelled"})

    def _open_from_accounting_for_correction(self, reason, accountant):
        """Open the same closed sheet after the audited Accounting return."""
        for closing in self:
            if closing.state != "closed":
                continue
            now = fields.Datetime.now()
            models.Model.write(closing.sudo(), {
                "state": "correction",
                "last_reopened_by_id": accountant.id,
                "last_reopened_at": now,
                "reopen_count": closing.reopen_count + 1,
                "last_correction_reason": reason,
                "correction_reason": False,
                "submitted_by_id": False,
                "submitted_at": False,
                "closed_by_id": False,
                "closed_at": False,
            })
            closing.sudo().message_post(body=self.env._(
                "The same Waiter Sales & Tips sheet was opened for correction "
                "by Accounting user %(user)s. Reason: %(reason)s",
                user=accountant.name,
                reason=reason,
            ))
        return True

    def action_refresh_waiters(self):
        self.ensure_one()
        require_role(self.env, RECEPTION_GROUP)
        self._require_editable()
        require_assigned_branches(self.branch_id)
        self._sync_daily_rows()
        self._rebuild_summaries()
        return True

    def action_download_waiter_table_pdf(self):
        self.ensure_one()
        self.check_access("read")
        if self.state != "closed":
            raise UserError(self.env._(
                "Close the Waiter Sales & Tips sheet before downloading its PDF."
            ))
        pdf_content = self._render_waiter_table_pdf()
        suffix = " - Updated" if self.reopen_count else ""
        filename = self.env._(
            "Waiter Sales & Tips - %(branch)s - %(date)s%(suffix)s.pdf",
            branch=self.branch_id.name,
            date=self.business_date,
            suffix=suffix,
        )
        attachment = self.env["ir.attachment"].sudo().create({
            "name": filename,
            "type": "binary",
            "raw": pdf_content,
            "mimetype": "application/pdf",
            "res_model": self._name,
            "res_id": self.id,
        })
        self.message_post(body=self.env._(
            "Generated the %(kind)s waiter table PDF for sharing.",
            kind="updated" if self.reopen_count else "closed",
        ))
        return {
            "type": "ir.actions.act_url",
            "url": f"/web/content/{attachment.id}?download=true",
            "target": "self",
        }

    def _render_waiter_table_pdf(self):
        self.ensure_one()
        font_name = "Helvetica"
        font_path = Path("/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf")
        if font_path.exists():
            font_name = "RestaurantWaiterDejaVu"
            if font_name not in pdfmetrics.getRegisteredFontNames():
                pdfmetrics.registerFont(TTFont(font_name, str(font_path)))

        styles = getSampleStyleSheet()
        normal = ParagraphStyle(
            "RestaurantWaiterNormal", parent=styles["BodyText"],
            fontName=font_name, fontSize=8.2, leading=10,
        )
        title = ParagraphStyle(
            "RestaurantWaiterTitle", parent=styles["Title"],
            fontName=font_name, fontSize=15, leading=18,
            alignment=TA_CENTER, spaceAfter=4,
        )
        center = ParagraphStyle(
            "RestaurantWaiterCenter", parent=normal, alignment=TA_CENTER,
        )
        table_header = ParagraphStyle(
            "RestaurantWaiterTableHeader",
            parent=center,
            textColor=colors.white,
        )

        def paragraph(value, style=normal, markup=False):
            text = str(value or "")
            if not markup:
                text = escape(text)
            return Paragraph(text or "&#160;", style)

        def money(value):
            return f"{value:,.2f}"

        heading = "UPDATED WAITER SALES & TIPS" if self.reopen_count else "WAITER SALES & TIPS"
        story = [
            paragraph(self.company_id.name, title),
            paragraph(heading, title),
            paragraph(
                self.env._(
                    "Branch: %(branch)s | Business Date: %(date)s | Reference: %(reference)s",
                    branch=self.branch_id.name,
                    date=format_date(self.env, self.business_date, date_format="dd MMMM yyyy"),
                    reference=self.name,
                ),
                center,
            ),
            paragraph(
                self.env._(
                    "All Bill amounts are final receipt totals with VAT included. "
                    "Tips are shown separately."
                ),
                center,
            ),
        ]
        if self.reopen_count:
            story.extend([
                Spacer(1, 2 * mm),
                paragraph(
                    self.env._(
                        "Updated after correction %(number)s | Reason: %(reason)s",
                        number=self.reopen_count,
                        reason=self.last_correction_reason or "-",
                    ),
                    center,
                ),
            ])
        story.append(Spacer(1, 5 * mm))

        summaries = self.summary_line_ids.sorted(
            key=lambda line: (
                line.sort_sequence or 1_000_000,
                (line.summary_name or "").lower(),
                line.id,
            )
        )

        def bill_tip_pairs(summary):
            if summary.summary_type == "waiter":
                source_rows = self.bill_ids.filtered(
                    lambda bill: bill.service_type == "dine_in"
                    and bill.employee_id == summary.employee_id
                )
            else:
                source_rows = self.bill_ids.filtered(
                    lambda bill: bill.service_type in ("pickup", "delivery")
                )
            pairs = []
            for bill in source_rows.sorted("id"):
                if bill.entry_mode == "order_details" and bill.order_ids:
                    pairs.extend(
                        (order.bill_amount, order.tip_total)
                        for order in bill.order_ids.sorted(
                            lambda order: (order.sequence, order.id)
                        )
                    )
                elif bill.bill_amount or bill.tip_total:
                    # Historical Daily Total rows remain one aggregate pair;
                    # their old totals are never guessed into individual orders.
                    pairs.append((bill.bill_amount, bill.tip_total))
            return pairs

        summary_pairs = {
            summary.id: bill_tip_pairs(summary)
            for summary in summaries
        }
        pair_count = max(
            [len(pairs) for pairs in summary_pairs.values()] or [0]
        )
        pair_count = max(pair_count, 1)
        headers = ["Rank", "Name", "Tables", "No. of Guests"]
        for number in range(1, pair_count + 1):
            headers.extend([
                self.env._("Bill %(number)s", number=number),
                self.env._("Tip %(number)s", number=number),
            ])
        headers.extend(["Total (VAT Incl.)", "Tips", "Tips %", "Reviews"])
        rows = [[paragraph(label, table_header) for label in headers]]
        for line in summaries:
            pair_values = []
            for bill_amount, tip_amount in summary_pairs[line.id]:
                pair_values.extend([
                    paragraph(money(bill_amount), center),
                    paragraph(money(tip_amount), center),
                ])
            pair_values.extend(
                paragraph("", center)
                for _index in range(2 * pair_count - len(pair_values))
            )
            rows.append([
                paragraph(
                    line.sales_rank if line.summary_type == "waiter" else "-",
                    center,
                ),
                paragraph(line.summary_name),
                paragraph(line.table_references or "-", center),
                paragraph(line.guest_counts or "-", center),
                *pair_values,
                paragraph(money(line.sales_amount), center),
                paragraph(money(line.tips_amount), center),
                paragraph(f"{line.tip_percentage:.2f}%", center),
                paragraph(line.review_count, center),
            ])
        rows.append([
            paragraph("", center),
            paragraph("TOTAL", center),
            paragraph("", center),
            paragraph(self.guest_count, center),
            *[
                paragraph("", center)
                for _index in range(2 * pair_count)
            ],
            paragraph(money(self.total_sales), center),
            paragraph(money(self.total_tips), center),
            paragraph("", center), paragraph(self.review_count, center),
        ])
        column_widths = [12 * mm, 34 * mm, 28 * mm, 28 * mm]
        column_widths.extend([12 * mm, 12 * mm] * pair_count)
        column_widths.extend([20 * mm, 18 * mm, 16 * mm, 14 * mm])
        table = Table(
            rows,
            colWidths=column_widths,
            repeatRows=1,
        )
        table.setStyle(TableStyle([
            ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#1F4E78")),
            ("TEXTCOLOR", (0, 0), (-1, 0), colors.white),
            ("BACKGROUND", (0, -1), (-1, -1), colors.HexColor("#D9EAF7")),
            ("FONTNAME", (0, 0), (-1, -1), font_name),
            ("FONTSIZE", (0, 0), (-1, -1), 7.7),
            ("GRID", (0, 0), (-1, -1), 0.4, colors.HexColor("#8A8A8A")),
            ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
            ("ROWBACKGROUNDS", (0, 1), (-1, -2), [colors.white, colors.HexColor("#F7FAFC")]),
            ("TOPPADDING", (0, 0), (-1, -1), 4),
            ("BOTTOMPADDING", (0, 0), (-1, -1), 4),
        ]))
        story.extend([table, Spacer(1, 4 * mm)])
        story.append(paragraph(
            self.env._(
                "Generated from the closed Waiter Sales & Tips sheet. "
                "This report shows sales and collected tips only; it does not record a payout."
            ),
            center,
        ))
        buffer = BytesIO()
        base_page_size = landscape(A4 if pair_count <= 5 else A3)
        required_width = sum(column_widths) + 20 * mm
        page_size = (
            max(base_page_size[0], required_width),
            base_page_size[1],
        )
        document = SimpleDocTemplate(
            buffer, pagesize=page_size,
            leftMargin=10 * mm, rightMargin=10 * mm,
            topMargin=10 * mm, bottomMargin=10 * mm,
            title=heading,
        )
        document.build(story)
        return buffer.getvalue()

    def _branch_waiters(self):
        self.ensure_one()
        configured = self.env["hr.employee"].sudo().search([
            ("active", "=", True),
            ("company_id", "=", self.company_id.id),
            ("restaurant_branch_id", "=", self.branch_id.id),
            ("job_id.name", "ilike", "waiter"),
        ])
        attendance_entries = self.env["restaurant.attendance.entry"].sudo().search([
            ("branch_id", "=", self.branch_id.id),
            ("attendance_date", "=", self.business_date),
            ("state", "!=", "rejected"),
        ])
        unavailable_employee_ids = attendance_entries.filtered(
            lambda entry: entry.status in {
                "absent", "day_off", "annual_leave", "sick_leave",
                "emergency_leave",
            } or entry.system_leave_locked
        ).mapped("employee_id").ids
        configured -= self.env["hr.employee"].sudo().browse(
            unavailable_employee_ids
        )
        return configured | self.bill_ids.filtered(
            lambda bill: bill.service_type == "dine_in"
            and bill.employee_id.id not in unavailable_employee_ids
        ).mapped("employee_id")

    def _sync_daily_rows(self):
        """Keep one editable daily-total row per active branch waiter."""
        bill_model = self.env["restaurant.waiter.sales.bill"].sudo().with_context(
            waiter_daily_rows_sync=True,
        )
        for closing in self:
            # Historical sheets with individual bill rows stay exactly as recorded.
            if closing.bill_ids.filtered(lambda line: not line.is_daily_total):
                continue
            desired_waiters = closing._branch_waiters()
            obsolete_rows = closing.bill_ids.filtered(
                lambda line: line.is_auto_waiter
                and line.employee_id not in desired_waiters
            )
            removable_rows = obsolete_rows.filtered(
                lambda line: not (
                    line.bill_count
                    or line.table_count
                    or line.guest_count
                    or line.bill_amount
                    or line.tip_total
                    or line.review_count
                )
            )
            if removable_rows:
                bill_model.browse(removable_rows.ids).unlink()
            existing_waiters = closing.bill_ids.filtered(
                lambda line: line.is_daily_total and line.service_type == "dine_in"
            ).mapped("employee_id")
            values_list = []
            for waiter in desired_waiters - existing_waiters:
                values_list.append({
                    "waiter_closing_id": closing.id,
                    "is_daily_total": True,
                    "is_auto_waiter": True,
                    "entry_mode": "order_details",
                    "service_type": "dine_in",
                    "employee_id": waiter.id,
                    "bill_reference": f"AUTO-WAITER-{waiter.id}",
                })
            if values_list:
                bill_model.create(values_list)

    def _rebuild_summaries(self):
        summary_model = self.env["restaurant.waiter.sales.summary"].sudo()
        bill_model = self.env["restaurant.waiter.sales.bill"]
        for closing in self:
            summary_model.search([
                ("waiter_closing_id", "=", closing.id),
            ]).unlink()
            bills = closing.bill_ids.sorted("id")
            by_waiter = defaultdict(lambda: bill_model)
            for waiter in closing._branch_waiters():
                by_waiter[waiter.id] |= bill_model
            for bill in bills.filtered(
                lambda item: item.service_type == "dine_in"
            ):
                by_waiter[bill.employee_id.id] |= bill

            waiter_values = []
            for employee_id, waiter_bills in sorted(
                by_waiter.items(),
                key=lambda item: (
                    self.env["hr.employee"].sudo().browse(item[0]).name or ""
                ).lower(),
            ):
                employee = self.env["hr.employee"].sudo().browse(employee_id)
                sales = sum(waiter_bills.mapped("bill_amount"))
                tips = sum(waiter_bills.mapped("tip_total"))
                daily_rows = waiter_bills.filtered("is_daily_total")
                legacy_rows = waiter_bills - daily_rows
                if not legacy_rows and not any(
                    line.bill_count
                    or line.table_count
                    or line.guest_count
                    or line.bill_amount
                    or line.tip_total
                    or line.review_count
                    for line in daily_rows
                ):
                    # Keep the ready-to-fill row on the entry screen, but do not
                    # publish an empty/off waiter in the closed summary or PDF.
                    continue
                waiter_values.append({
                    "waiter_closing_id": closing.id,
                    "summary_type": "waiter",
                    "summary_name": employee.display_name,
                    "employee_id": employee_id,
                    "table_references": ", ".join(
                        value
                        for value in (
                            daily_rows.mapped("table_references")
                            + legacy_rows.mapped("table_reference")
                        )
                        if value
                    ),
                    "guest_counts": ", ".join(
                        value
                        for value in (
                            daily_rows.mapped("guest_counts")
                            + [
                                str(value)
                                for value in legacy_rows.mapped("guest_count")
                            ]
                        )
                        if value
                    ),
                    "order_breakdown": " | ".join(
                        value
                        for value in (
                            daily_rows.mapped("order_breakdown")
                            + [
                                "%s / %s" % (
                                    f"{line.bill_amount:g}",
                                    f"{line.tip_total:g}",
                                )
                                for line in legacy_rows
                            ]
                        )
                        if value
                    ),
                    "bill_count": sum(daily_rows.mapped("bill_count")) + len(legacy_rows),
                    "table_count": sum(daily_rows.mapped("table_count")) + len(legacy_rows),
                    "guest_count": sum(waiter_bills.mapped("guest_count")),
                    "sales_amount": sales,
                    "cash_tip_amount": sum(
                        waiter_bills.mapped("cash_tip_amount")
                    ),
                    "card_tip_amount": sum(
                        waiter_bills.mapped("card_tip_amount")
                    ),
                    "tips_amount": tips,
                    "tip_percentage": 100 * tips / sales if sales else 0,
                    "review_count": sum(waiter_bills.mapped("review_count")),
                })

            waiter_values.sort(key=lambda values: (
                -closing.currency_id.round(values["sales_amount"]),
                (values["summary_name"] or "").lower(),
                values["employee_id"],
            ))
            previous_sales = None
            current_rank = 0
            for position, values in enumerate(waiter_values, start=1):
                rounded_sales = closing.currency_id.round(values["sales_amount"])
                if previous_sales is None or rounded_sales != previous_sales:
                    current_rank = position
                    previous_sales = rounded_sales
                values.update({
                    "sales_rank": current_rank,
                    "sort_sequence": position,
                })

            values_list = list(waiter_values)

            takeaway = bills.filtered(
                lambda item: item.service_type in ("pickup", "delivery")
            )
            if takeaway:
                sales = sum(takeaway.mapped("bill_amount"))
                tips = sum(takeaway.mapped("tip_total"))
                daily_takeaway = takeaway.filtered("is_daily_total")
                legacy_takeaway = takeaway - daily_takeaway
                values_list.append({
                    "waiter_closing_id": closing.id,
                    "summary_type": "pickup_delivery",
                    "summary_name": self.env._("Pickup / Delivery"),
                    "sales_rank": 0,
                    "sort_sequence": 1_000_000,
                    "bill_count": (
                        sum(daily_takeaway.mapped("bill_count"))
                        + len(legacy_takeaway)
                    ),
                    "table_count": 0,
                    "order_breakdown": " | ".join(
                        "%s / %s" % (
                            f"{line.bill_amount:g}",
                            f"{line.tip_total:g}",
                        )
                        for line in takeaway
                        if line.bill_amount or line.tip_total
                    ),
                    "sales_amount": sales,
                    "cash_tip_amount": sum(
                        takeaway.mapped("cash_tip_amount")
                    ),
                    "card_tip_amount": sum(
                        takeaway.mapped("card_tip_amount")
                    ),
                    "tips_amount": tips,
                    "tip_percentage": 100 * tips / sales if sales else 0,
                    "review_count": sum(takeaway.mapped("review_count")),
                })
            if values_list:
                summary_model.create(values_list)

    def _sync_daily_closing_links(self):
        daily_model = self.env["restaurant.daily.closing"].sudo().with_context(
            waiter_closing_sync=True,
        )
        for closing in self:
            linked = daily_model.search([
                ("waiter_sales_closing_id", "=", closing.id),
            ])
            if closing.state == "cancelled":
                linked.write({"waiter_sales_closing_id": False})
                continue
            daily = daily_model.search([
                ("branch_id", "=", closing.branch_id.id),
                ("closing_date", "=", closing.business_date),
                ("state", "!=", "cancelled"),
                ("service_tracking_mode", "=", "standalone"),
            ], limit=1)
            (linked - daily).write({"waiter_sales_closing_id": False})
            if daily and daily.waiter_sales_closing_id != closing:
                daily.write({"waiter_sales_closing_id": closing.id})


class RestaurantWaiterSalesBill(models.Model):
    _name = "restaurant.waiter.sales.bill"
    _description = "Waiter Sales and Tips Bill"
    _order = "id"

    waiter_closing_id = fields.Many2one(
        "restaurant.waiter.sales.closing",
        required=True,
        ondelete="cascade",
        index=True,
    )
    service_type = fields.Selection(
        [
            ("dine_in", "Dine-in"),
            ("pickup", "Pickup"),
            ("delivery", "Delivery"),
        ],
        required=True,
        default="dine_in",
        index=True,
    )
    is_daily_total = fields.Boolean(
        string="Daily Total Row", default=False, readonly=True, copy=False,
        index=True,
    )
    is_auto_waiter = fields.Boolean(
        string="Automatic Waiter Row", default=False, readonly=True,
        copy=False, index=True,
    )
    entry_mode = fields.Selection(
        [
            ("daily_total", "Daily Total"),
            ("order_details", "Detailed Orders"),
        ],
        required=True,
        default="daily_total",
        string="Entry Method",
        help=(
            "Detailed Orders records every table separately. Daily Total is "
            "kept for historical or already-entered aggregate rows."
        ),
    )
    order_ids = fields.One2many(
        "restaurant.waiter.sales.order",
        "waiter_line_id",
        string="Orders",
        copy=True,
    )
    table_references = fields.Char(
        string="Table List",
        compute="_compute_order_display",
        store=True,
    )
    guest_counts = fields.Char(
        string="Guests per Order",
        compute="_compute_order_display",
        store=True,
    )
    order_breakdown = fields.Char(
        string="Bills / Tips",
        compute="_compute_order_display",
        store=True,
    )
    tip_percentage = fields.Float(
        string="Tips %",
        compute="_compute_order_display",
        store=True,
        aggregator=False,
    )
    employee_id = fields.Many2one(
        "hr.employee", string="Waiter", ondelete="restrict", index=True,
    )
    bill_reference = fields.Char(string="Bill / Check", required=True, index=True)
    table_reference = fields.Char(string="Table")
    guest_count = fields.Integer(string="Guests", default=0)
    bill_count = fields.Integer(string="Bills", default=0)
    table_count = fields.Integer(string="Tables", default=0)
    bill_amount = fields.Monetary(
        string="Bill Total (VAT Included)",
        required=True,
        default=0,
        help="Enter the final receipt total including VAT. VAT is not added again.",
    )
    cash_tip_amount = fields.Monetary(string="Cash Tip", required=True, default=0)
    card_tip_amount = fields.Monetary(string="Card Tip", required=True, default=0)
    tip_total = fields.Monetary(
        string="Total Tip", compute="_compute_tip_total", store=True,
    )
    review_count = fields.Integer(string="Reviews", default=0)
    note = fields.Char()
    entered_by_id = fields.Many2one(
        "res.users", required=True, readonly=True, default=lambda self: self.env.user,
    )
    entered_at = fields.Datetime(
        required=True, readonly=True, default=fields.Datetime.now,
    )
    branch_id = fields.Many2one(
        related="waiter_closing_id.branch_id", store=True, index=True,
    )
    business_date = fields.Date(
        related="waiter_closing_id.business_date", store=True, index=True,
    )
    state = fields.Selection(
        related="waiter_closing_id.state", store=True, index=True,
    )
    company_id = fields.Many2one(
        related="waiter_closing_id.company_id", store=True, index=True,
    )
    currency_id = fields.Many2one(
        related="waiter_closing_id.currency_id", store=True,
    )

    _bill_reference_unique = models.UniqueIndex(
        "(waiter_closing_id, bill_reference)",
        "This bill/check is already recorded in the waiter closing.",
    )
    _nonnegative_values = models.Constraint(
        "CHECK (guest_count >= 0 AND bill_count >= 0 AND table_count >= 0 "
        "AND bill_amount >= 0 "
        "AND cash_tip_amount >= 0 AND card_tip_amount >= 0 "
        "AND review_count >= 0)",
        "Guests, bills, tips, and reviews cannot be negative.",
    )

    @api.depends("cash_tip_amount", "card_tip_amount")
    def _compute_tip_total(self):
        for bill in self:
            bill.tip_total = bill.cash_tip_amount + bill.card_tip_amount

    @api.depends(
        "order_ids.table_reference",
        "order_ids.guest_count",
        "order_ids.bill_amount",
        "order_ids.cash_tip_amount",
        "order_ids.card_tip_amount",
        "bill_amount",
        "cash_tip_amount",
        "card_tip_amount",
        "entry_mode",
    )
    def _compute_order_display(self):
        for line in self:
            orders = line.order_ids.sorted(lambda order: (order.sequence, order.id))
            if line.entry_mode == "order_details":
                line.table_references = ", ".join(
                    orders.mapped("table_reference")
                )
                line.guest_counts = ", ".join(
                    str(value) for value in orders.mapped("guest_count")
                )
                line.order_breakdown = " | ".join(
                    "%s / %s" % (
                        f"{order.bill_amount:g}",
                        f"{order.tip_total:g}",
                    )
                    for order in orders
                )
            else:
                line.table_references = False
                line.guest_counts = False
                line.order_breakdown = False
            tips = line.cash_tip_amount + line.card_tip_amount
            line.tip_percentage = (
                100 * tips / line.bill_amount if line.bill_amount else 0
            )

    def _sync_order_totals(self):
        """Refresh one waiter aggregate from its auditable order rows."""
        closings = self.env["restaurant.waiter.sales.closing"]
        for line in self.filtered(
            lambda current: current.is_auto_waiter
            and current.entry_mode == "order_details"
        ):
            orders = line.order_ids
            models.Model.write(line.sudo(), {
                "bill_count": len(orders),
                "table_count": len(orders.filtered("table_reference")),
                "guest_count": sum(orders.mapped("guest_count")),
                "bill_amount": sum(orders.mapped("bill_amount")),
                "cash_tip_amount": sum(orders.mapped("cash_tip_amount")),
                "card_tip_amount": sum(orders.mapped("card_tip_amount")),
                "review_count": sum(orders.mapped("review_count")),
            })
            closings |= line.waiter_closing_id
        if closings:
            closings._rebuild_summaries()

    def action_open_orders(self):
        self.ensure_one()
        if not self.is_auto_waiter:
            raise UserError(self.env._("Orders are available on waiter rows only."))
        return {
            "type": "ir.actions.act_window",
            "name": self.env._("Orders - %s", self.employee_id.display_name),
            "res_model": self._name,
            "res_id": self.id,
            "view_id": self.env.ref(
                "restaurant_reception.restaurant_waiter_sales_bill_orders_view_form"
            ).id,
            "view_mode": "form",
            "target": "new",
        }

    @api.onchange("service_type")
    def _onchange_service_type(self):
        if self.service_type in ("pickup", "delivery"):
            self.employee_id = False
            self.table_reference = False
            self.guest_count = 0

    @api.constrains(
        "service_type", "employee_id", "table_reference", "guest_count",
        "waiter_closing_id", "bill_reference", "entry_mode", "order_ids",
    )
    def _check_service_details(self):
        for bill in self:
            if bill.service_type == "dine_in" and bill.employee_id:
                if bill.employee_id.restaurant_branch_id != bill.branch_id:
                    raise ValidationError(self.env._(
                        "Select a waiter assigned to the same branch as this sheet."
                    ))
                job_name = (bill.employee_id.job_id.name or "").strip().lower()
                if "waiter" not in job_name:
                    raise ValidationError(self.env._(
                        "%s is not assigned to the Waiter job.",
                        bill.employee_id.display_name,
                    ))
            if bill.is_daily_total:
                if bill.service_type == "dine_in" and not bill.employee_id:
                    raise ValidationError(self.env._(
                        "Every automatic waiter row must have a waiter."
                    ))
                if bill.service_type != "dine_in" and bill.employee_id:
                    raise ValidationError(self.env._(
                        "Pickup and delivery totals cannot be assigned to a waiter."
                    ))
                if bill.is_auto_waiter and bill.service_type != "dine_in":
                    raise ValidationError(self.env._(
                        "Automatic waiter rows must stay Dine-in."
                    ))
                if bill.entry_mode == "order_details" and not bill.is_auto_waiter:
                    raise ValidationError(self.env._(
                        "Detailed Orders are available on automatic waiter rows only."
                    ))
                if bill.entry_mode == "daily_total" and bill.order_ids:
                    raise ValidationError(self.env._(
                        "Remove the detailed orders before using Daily Total."
                    ))
                continue
            if not (bill.bill_reference or "").strip():
                raise ValidationError(self.env._("Enter the bill/check reference."))
            if bill.service_type == "dine_in":
                if not bill.employee_id:
                    raise ValidationError(
                        self.env._("Select the waiter for every dine-in bill.")
                    )
                if not (bill.table_reference or "").strip():
                    raise ValidationError(
                        self.env._("Enter the table for every dine-in bill.")
                    )
                check_employee_company(bill)
            elif bill.employee_id or (bill.table_reference or "").strip():
                raise ValidationError(self.env._(
                    "Pickup and delivery bills stay separate: do not assign a waiter or table."
                ))
            elif bill.guest_count:
                raise ValidationError(self.env._(
                    "Guest count is recorded on dine-in bills only."
                ))

    @api.model_create_multi
    def create(self, vals_list):
        if self.env.su and self.env.context.get("waiter_daily_rows_sync"):
            return super().create(vals_list)
        require_role(self.env, RECEPTION_GROUP)
        editable = {
            "waiter_closing_id", "service_type", "employee_id",
            "bill_reference", "table_reference", "guest_count",
            "is_daily_total", "entry_mode", "order_ids",
            "bill_count", "table_count",
            "bill_amount", "cash_tip_amount", "card_tip_amount",
            "review_count", "note",
        }
        if any(set(values) - editable for values in vals_list):
            raise AccessError(
                self.env._("Bill audit and calculated fields are system-controlled.")
            )
        defaults = self.default_get(["waiter_closing_id"])
        closing_ids = [
            values.get("waiter_closing_id", defaults.get("waiter_closing_id"))
            for values in vals_list
        ]
        if not all(closing_ids):
            raise ValidationError(self.env._("Select the Waiter Sales & Tips closing."))
        closings = self.env["restaurant.waiter.sales.closing"].browse(closing_ids)
        lock_records(closings)
        closings._require_editable()
        require_assigned_branches(closings.branch_id)
        prepared = []
        for offset, values in enumerate(vals_list, start=1):
            values = dict(values)
            if values.get("is_auto_waiter"):
                raise AccessError(self.env._(
                    "Automatic waiter rows are generated by the system."
                ))
            if values.get("is_daily_total"):
                service_type = values.get("service_type", "dine_in")
                if service_type == "dine_in":
                    raise AccessError(self.env._(
                        "Use Refresh Staff Rows to add waiter rows."
                    ))
                values["employee_id"] = False
                values["table_reference"] = False
                closing_id = values.get(
                    "waiter_closing_id", defaults.get("waiter_closing_id")
                )
                existing_count = self.sudo().search_count([
                    ("waiter_closing_id", "=", closing_id),
                ])
                values["bill_reference"] = (
                    values.get("bill_reference")
                    or f"DAILY-{service_type.upper()}-{existing_count + offset}"
                )
            if "bill_reference" in values:
                values["bill_reference"] = (values["bill_reference"] or "").strip()
            if "table_reference" in values:
                values["table_reference"] = (values["table_reference"] or "").strip()
            prepared.append(values)
        bills = super().create(prepared)
        bills.waiter_closing_id._rebuild_summaries()
        return bills

    def write(self, vals):
        self.check_access("write")
        require_role(self.env, RECEPTION_GROUP)
        editable = {
            "service_type", "employee_id", "bill_reference", "table_reference",
            "guest_count", "bill_count", "table_count", "bill_amount", "cash_tip_amount",
            "card_tip_amount", "review_count", "note", "entry_mode", "order_ids",
        }
        if set(vals) - editable:
            raise AccessError(
                self.env._("Bills cannot be moved or their audit fields overwritten.")
            )
        closings = self.waiter_closing_id
        if self.filtered("is_auto_waiter") and set(vals) & {
            "service_type", "employee_id", "bill_reference", "table_reference",
        }:
            raise AccessError(self.env._(
                "The waiter identity is refreshed from the branch staff list."
            ))
        target_mode = vals.get("entry_mode")
        for line in self.filtered("is_auto_waiter"):
            if target_mode == "order_details" and not line.order_ids and any((
                line.bill_count,
                line.table_count,
                line.guest_count,
                line.bill_amount,
                line.tip_total,
                line.review_count,
            )):
                raise ValidationError(self.env._(
                    "Clear the existing Daily Total values before switching "
                    "this waiter to Detailed Orders."
                ))
            if target_mode == "daily_total" and line.order_ids:
                raise ValidationError(self.env._(
                    "Remove the detailed orders before switching to Daily Total."
                ))
        manual_total_fields = {
            "bill_count", "table_count", "guest_count", "bill_amount",
            "cash_tip_amount", "card_tip_amount", "review_count",
        }
        detailed_lines = self.filtered(
            lambda line: line.is_auto_waiter
            and (target_mode or line.entry_mode) == "order_details"
        )
        if detailed_lines and set(vals) & manual_total_fields:
            raise AccessError(self.env._(
                "Detailed waiter totals come from the Orders table."
            ))
        lock_records(closings)
        closings._require_editable()
        require_assigned_branches(closings.branch_id)
        values = dict(vals)
        if "bill_reference" in values:
            values["bill_reference"] = (values["bill_reference"] or "").strip()
        if "table_reference" in values:
            values["table_reference"] = (values["table_reference"] or "").strip()
        result = super().write(values)
        closings._rebuild_summaries()
        return result

    def unlink(self):
        if self.env.su and self.env.context.get("waiter_daily_rows_sync"):
            return super().unlink()
        self.check_access("unlink")
        require_role(self.env, RECEPTION_GROUP)
        if self.filtered("is_auto_waiter"):
            raise AccessError(self.env._(
                "Automatic waiter rows cannot be deleted; refresh the staff list instead."
            ))
        closings = self.waiter_closing_id
        lock_records(closings)
        closings._require_editable()
        require_assigned_branches(closings.branch_id)
        result = super().unlink()
        closings._rebuild_summaries()
        return result


class RestaurantWaiterSalesOrder(models.Model):
    _name = "restaurant.waiter.sales.order"
    _description = "Waiter Sales and Tips Order"
    _order = "sequence, id"
    _rec_name = "reference"

    waiter_line_id = fields.Many2one(
        "restaurant.waiter.sales.bill",
        string="Waiter Row",
        required=True,
        ondelete="cascade",
        index=True,
    )
    waiter_closing_id = fields.Many2one(
        related="waiter_line_id.waiter_closing_id",
        store=True,
        readonly=True,
        index=True,
    )
    employee_id = fields.Many2one(
        related="waiter_line_id.employee_id",
        store=True,
        readonly=True,
        index=True,
    )
    sequence = fields.Integer(default=10)
    reference = fields.Char(
        string="Order",
        required=True,
        readonly=True,
        copy=False,
        default="New",
        index=True,
    )
    pos_reference = fields.Char(string="Bill / Check Reference", index=True)
    table_reference = fields.Char(string="Table", required=True)
    guest_count = fields.Integer(string="Guests", required=True, default=1)
    bill_amount = fields.Monetary(
        string="Bill Total (VAT Included)",
        required=True,
        default=0,
        help="Enter the final receipt total including VAT. VAT is not added again.",
    )
    cash_tip_amount = fields.Monetary(string="Cash Tip", required=True, default=0)
    card_tip_amount = fields.Monetary(string="Card Tip", required=True, default=0)
    tip_total = fields.Monetary(
        string="Tip",
        compute="_compute_tip_total",
        store=True,
    )
    review_count = fields.Integer(string="Reviews", default=0)
    note = fields.Char()
    entered_by_id = fields.Many2one(
        "res.users",
        required=True,
        readonly=True,
        default=lambda self: self.env.user,
    )
    entered_at = fields.Datetime(
        required=True,
        readonly=True,
        default=fields.Datetime.now,
    )
    branch_id = fields.Many2one(
        related="waiter_closing_id.branch_id",
        store=True,
        readonly=True,
        index=True,
    )
    business_date = fields.Date(
        related="waiter_closing_id.business_date",
        store=True,
        readonly=True,
        index=True,
    )
    state = fields.Selection(
        related="waiter_closing_id.state",
        store=True,
        readonly=True,
        index=True,
    )
    company_id = fields.Many2one(
        related="waiter_closing_id.company_id",
        store=True,
        readonly=True,
        index=True,
    )
    currency_id = fields.Many2one(
        related="waiter_closing_id.currency_id",
        store=True,
        readonly=True,
    )

    _reference_unique = models.UniqueIndex(
        "(waiter_closing_id, reference)",
        "This waiter order is already recorded in the closing.",
    )
    _pos_reference_unique = models.UniqueIndex(
        "(waiter_closing_id, pos_reference) "
        "WHERE pos_reference IS NOT NULL AND pos_reference != ''",
        "This Bill / Check Reference is already recorded in the closing.",
    )
    _nonnegative_values = models.Constraint(
        "CHECK (guest_count >= 0 AND bill_amount >= 0 "
        "AND cash_tip_amount >= 0 AND card_tip_amount >= 0 "
        "AND review_count >= 0)",
        "Guests, bill, tips, and reviews cannot be negative.",
    )

    @api.depends("cash_tip_amount", "card_tip_amount")
    def _compute_tip_total(self):
        for order in self:
            order.tip_total = order.cash_tip_amount + order.card_tip_amount

    @api.constrains("waiter_line_id", "table_reference", "pos_reference")
    def _check_order_details(self):
        for order in self:
            waiter_line = order.waiter_line_id
            if not (
                waiter_line.is_auto_waiter
                and waiter_line.is_daily_total
                and waiter_line.service_type == "dine_in"
                and waiter_line.entry_mode == "order_details"
            ):
                raise ValidationError(self.env._(
                    "Detailed orders must belong to an automatic Dine-in waiter row."
                ))
            if not (order.table_reference or "").strip():
                raise ValidationError(self.env._("Enter the table for every order."))

    @api.model_create_multi
    def create(self, vals_list):
        require_role(self.env, RECEPTION_GROUP)
        editable = {
            "waiter_line_id", "sequence", "pos_reference", "table_reference",
            "guest_count", "bill_amount", "cash_tip_amount", "card_tip_amount",
            "review_count", "note",
        }
        if any(set(values) - editable for values in vals_list):
            raise AccessError(self.env._(
                "Order audit and calculated fields are system-controlled."
            ))
        line_ids = [values.get("waiter_line_id") for values in vals_list]
        if not all(line_ids):
            raise ValidationError(self.env._("Select the waiter row for every order."))
        waiter_lines = self.env["restaurant.waiter.sales.bill"].browse(line_ids)
        closings = waiter_lines.waiter_closing_id
        lock_records(closings)
        closings._require_editable()
        require_assigned_branches(closings.branch_id)
        prepared = []
        for values in vals_list:
            values = dict(values)
            values["reference"] = (
                self.env["ir.sequence"].next_by_code(
                    "restaurant.waiter.sales.order"
                ) or "New"
            )
            if "table_reference" in values:
                values["table_reference"] = (
                    values["table_reference"] or ""
                ).strip()
            if "pos_reference" in values:
                values["pos_reference"] = (
                    values["pos_reference"] or ""
                ).strip() or False
            prepared.append(values)
        orders = super().create(prepared)
        orders.waiter_line_id._sync_order_totals()
        return orders

    def write(self, values):
        self.check_access("write")
        require_role(self.env, RECEPTION_GROUP)
        editable = {
            "sequence", "pos_reference", "table_reference", "guest_count",
            "bill_amount", "cash_tip_amount", "card_tip_amount",
            "review_count", "note",
        }
        if set(values) - editable:
            raise AccessError(self.env._(
                "Orders cannot be moved or their audit fields overwritten."
            ))
        waiter_lines = self.waiter_line_id
        closings = waiter_lines.waiter_closing_id
        lock_records(closings)
        closings._require_editable()
        require_assigned_branches(closings.branch_id)
        values = dict(values)
        if "table_reference" in values:
            values["table_reference"] = (
                values["table_reference"] or ""
            ).strip()
        if "pos_reference" in values:
            values["pos_reference"] = (
                values["pos_reference"] or ""
            ).strip() or False
        result = super().write(values)
        waiter_lines._sync_order_totals()
        return result

    def unlink(self):
        self.check_access("unlink")
        require_role(self.env, RECEPTION_GROUP)
        waiter_lines = self.waiter_line_id
        closings = waiter_lines.waiter_closing_id
        lock_records(closings)
        closings._require_editable()
        require_assigned_branches(closings.branch_id)
        result = super().unlink()
        waiter_lines._sync_order_totals()
        return result


class RestaurantWaiterSalesSummary(models.Model):
    _name = "restaurant.waiter.sales.summary"
    _description = "Generated Waiter Sales and Tips Summary"
    _order = "sort_sequence, summary_name, id"
    _rec_name = "summary_name"

    waiter_closing_id = fields.Many2one(
        "restaurant.waiter.sales.closing",
        required=True,
        ondelete="cascade",
        index=True,
    )
    summary_type = fields.Selection(
        [("waiter", "Waiter"), ("pickup_delivery", "Pickup / Delivery")],
        required=True,
        readonly=True,
        index=True,
    )
    summary_name = fields.Char(string="Name", required=True, readonly=True)
    sales_rank = fields.Integer(
        string="Rank",
        readonly=True,
        help=(
            "Sales rank within this sheet, based on Total Sales (VAT Included). "
            "Equal totals share the same rank and the following rank is skipped."
        ),
    )
    sort_sequence = fields.Integer(readonly=True, index=True)
    employee_id = fields.Many2one(
        "hr.employee", string="Waiter", ondelete="restrict", readonly=True,
    )
    table_references = fields.Char(string="Tables", readonly=True)
    guest_counts = fields.Char(string="Guests per Bill", readonly=True)
    order_breakdown = fields.Char(string="Bills / Tips", readonly=True)
    bill_count = fields.Integer(string="Bills", readonly=True)
    table_count = fields.Integer(string="Tables Served", readonly=True)
    guest_count = fields.Integer(string="Total Guests", readonly=True)
    sales_amount = fields.Monetary(
        string="Total Sales (VAT Included)", readonly=True,
    )
    cash_tip_amount = fields.Monetary(string="Cash Tips", readonly=True)
    card_tip_amount = fields.Monetary(string="Card Tips", readonly=True)
    tips_amount = fields.Monetary(string="Tips", readonly=True)
    tip_percentage = fields.Float(string="Tips %", readonly=True, aggregator=False)
    review_count = fields.Integer(string="Reviews", readonly=True)
    branch_id = fields.Many2one(
        related="waiter_closing_id.branch_id", store=True, index=True,
    )
    business_date = fields.Date(
        related="waiter_closing_id.business_date", store=True, index=True,
    )
    state = fields.Selection(
        related="waiter_closing_id.state", store=True, index=True,
    )
    company_id = fields.Many2one(
        related="waiter_closing_id.company_id", store=True, index=True,
    )
    currency_id = fields.Many2one(
        related="waiter_closing_id.currency_id", store=True,
    )

    @api.model_create_multi
    def create(self, vals_list):
        if not self.env.su:
            raise AccessError(self.env._("Waiter summaries are generated from bills."))
        return super().create(vals_list)

    def write(self, vals):
        if not self.env.su:
            raise AccessError(self.env._("Waiter summaries are generated from bills."))
        return super().write(vals)

    def unlink(self):
        if not self.env.su:
            raise AccessError(self.env._("Waiter summaries are generated from bills."))
        return super().unlink()

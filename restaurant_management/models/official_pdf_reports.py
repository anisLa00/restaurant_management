from io import BytesIO
from pathlib import Path
from xml.sax.saxutils import escape

from reportlab.lib import colors
from reportlab.lib.enums import TA_CENTER, TA_LEFT
from reportlab.lib.pagesizes import A4, landscape
from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
from reportlab.lib.units import mm
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont
from reportlab.platypus import (
    Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle,
)

from odoo import fields, models
from odoo.exceptions import UserError
from odoo.tools.misc import format_date


def _font_name():
    font_name = 'Helvetica'
    font_path = Path('/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf')
    if font_path.exists():
        font_name = 'RestaurantOfficialDejaVu'
        if font_name not in pdfmetrics.getRegisteredFontNames():
            pdfmetrics.registerFont(TTFont(font_name, str(font_path)))
    return font_name


def _selection_label(record, field_name, value=None):
    value = value if value is not None else record[field_name]
    options = dict(record._fields[field_name]._description_selection(record.env))
    return options.get(value, value or '-')


def _number(value, decimals=2):
    return f'{value or 0:,.{decimals}f}'


def _date(record, value):
    return format_date(record.env, value, date_format='dd MMM yyyy') if value else '-'


def _datetime(record, value):
    if not value:
        return '-'
    local_value = fields.Datetime.context_timestamp(record, value)
    return local_value.strftime('%d %b %Y %H:%M')


def _render_official_pdf(record, title, meta, sections, notice=None, updated=None):
    buffer = BytesIO()
    font_name = _font_name()
    styles = getSampleStyleSheet()
    body = ParagraphStyle(
        'OfficialBody', parent=styles['BodyText'], fontName=font_name,
        fontSize=7.5, leading=9, alignment=TA_LEFT,
    )
    center = ParagraphStyle(
        'OfficialCenter', parent=body, alignment=TA_CENTER,
    )
    heading = ParagraphStyle(
        'OfficialHeading', parent=styles['Title'], fontName=font_name,
        fontSize=15, leading=18, textColor=colors.HexColor('#24466f'),
        alignment=TA_CENTER, spaceAfter=3 * mm,
    )
    section_style = ParagraphStyle(
        'OfficialSection', parent=styles['Heading2'], fontName=font_name,
        fontSize=10, leading=12, textColor=colors.HexColor('#24466f'),
        spaceBefore=2 * mm, spaceAfter=2 * mm,
    )

    def para(value, style=body):
        return Paragraph(escape(str(value if value not in (False, None, '') else '-')), style)

    def footer(canvas, document):
        canvas.saveState()
        canvas.setFont(font_name, 7)
        canvas.setFillColor(colors.HexColor('#667085'))
        canvas.drawString(12 * mm, 7 * mm, str(record.company_id.name))
        canvas.drawRightString(
            landscape(A4)[0] - 12 * mm, 7 * mm,
            record.env._('Page %s', document.page),
        )
        canvas.restoreState()

    document = SimpleDocTemplate(
        buffer, pagesize=landscape(A4),
        leftMargin=12 * mm, rightMargin=12 * mm,
        topMargin=11 * mm, bottomMargin=13 * mm,
        title=title, author=record.company_id.name,
    )
    story = [
        para(record.company_id.name, heading),
        para(title, heading),
    ]
    meta_rows = [[para(label), para(value)] for label, value in meta]
    meta_table = Table(meta_rows, colWidths=[38 * mm, 220 * mm])
    meta_table.setStyle(TableStyle([
        ('BACKGROUND', (0, 0), (0, -1), colors.HexColor('#eaf1f8')),
        ('TEXTCOLOR', (0, 0), (0, -1), colors.HexColor('#24466f')),
        ('FONTNAME', (0, 0), (-1, -1), font_name),
        ('BOX', (0, 0), (-1, -1), 0.35, colors.HexColor('#cbd5e1')),
        ('INNERGRID', (0, 0), (-1, -1), 0.25, colors.HexColor('#d9e2ec')),
        ('VALIGN', (0, 0), (-1, -1), 'TOP'),
        ('LEFTPADDING', (0, 0), (-1, -1), 4),
        ('RIGHTPADDING', (0, 0), (-1, -1), 4),
        ('TOPPADDING', (0, 0), (-1, -1), 3),
        ('BOTTOMPADDING', (0, 0), (-1, -1), 3),
    ]))
    story.extend([meta_table, Spacer(1, 3 * mm)])
    if updated:
        story.extend([
            para(record.env._('UPDATED - %(reason)s', reason=updated), center),
            Spacer(1, 2 * mm),
        ])

    for index, (section_title, headers, rows, widths) in enumerate(sections):
        if index:
            story.append(Spacer(1, 3 * mm))
        story.append(para(section_title, section_style))
        safe_rows = rows or [[record.env._('No records')] + [''] * (len(headers) - 1)]
        data = [[para(header, center) for header in headers]] + [
            [para(value) for value in row] for row in safe_rows
        ]
        table = Table(
            data,
            colWidths=[width * mm for width in widths],
            repeatRows=1,
            hAlign='LEFT',
        )
        table.setStyle(TableStyle([
            ('BACKGROUND', (0, 0), (-1, 0), colors.HexColor('#4778ad')),
            ('TEXTCOLOR', (0, 0), (-1, 0), colors.white),
            ('FONTNAME', (0, 0), (-1, -1), font_name),
            ('GRID', (0, 0), (-1, -1), 0.3, colors.HexColor('#cbd5e1')),
            ('ROWBACKGROUNDS', (0, 1), (-1, -1), [colors.white, colors.HexColor('#f7f9fc')]),
            ('VALIGN', (0, 0), (-1, -1), 'TOP'),
            ('LEFTPADDING', (0, 0), (-1, -1), 3),
            ('RIGHTPADDING', (0, 0), (-1, -1), 3),
            ('TOPPADDING', (0, 0), (-1, -1), 3),
            ('BOTTOMPADDING', (0, 0), (-1, -1), 3),
        ]))
        story.append(table)
    if notice:
        story.extend([Spacer(1, 4 * mm), para(notice, center)])
    document.build(story, onFirstPage=footer, onLaterPages=footer)
    return buffer.getvalue()


def _download(record, title, filename, content):
    attachment = record.env['ir.attachment'].sudo().create({
        'name': filename,
        'type': 'binary',
        'raw': content,
        'mimetype': 'application/pdf',
        'res_model': record._name,
        'res_id': record.id,
    })
    record.message_post(body=record.env._('Generated final PDF for sharing: %s', title))
    return {
        'type': 'ir.actions.act_url',
        'url': f'/web/content/{attachment.id}?download=true',
        'target': 'self',
    }


class RestaurantShiftRosterOfficialPdf(models.Model):
    _inherit = 'restaurant.shift.roster'

    def action_download_official_pdf(self):
        self.ensure_one()
        self.check_access('read')
        if self.state != 'published':
            raise UserError(self.env._('Publish the Shift Roster before downloading its PDF.'))
        rows = []
        weekday = dict(self.env['restaurant.shift.roster.line']._fields[
            'day_off_weekday'
        ]._description_selection(self.env))
        shift = dict(self.env['restaurant.shift.roster.line']._fields[
            'staff_shift'
        ]._description_selection(self.env))
        for line in self.line_ids.sorted(lambda item: (item.employee_id.name or '', item.id)):
            rows.append([
                line.employee_number or '-', line.employee_id.name,
                line.job_id.name or '-', shift.get(line.staff_shift, '-'),
                weekday.get(line.day_off_weekday, '-'),
                line.approved_leave_summary or '-',
            ])
        title = self.env._('MONTHLY SHIFT ROSTER')
        content = _render_official_pdf(
            self, title,
            [(self.env._('Branch'), self.branch_id.name),
             (self.env._('Period'), f'{_date(self, self.month_start)} - {_date(self, self.month_end)}'),
             (self.env._('Status'), _selection_label(self, 'state'))],
            [(self.env._('Staff Schedule'),
              ['Employee No.', 'Employee', 'Job / Role', 'Shift', 'Weekly Day Off', 'Approved Leave'],
              rows, [24, 46, 40, 28, 34, 88])],
            notice=self.env._('Approved leave is shown automatically after HR approval.'),
        )
        return _download(
            self, title,
            f'Shift Roster - {self.branch_id.name} - {self.month_start}.pdf',
            content,
        )


class RestaurantAttendanceSheetOfficialPdf(models.Model):
    _inherit = 'restaurant.attendance.sheet'

    def action_download_official_pdf(self):
        self.ensure_one()
        self.check_access('read')
        if self.state != 'approved':
            raise UserError(self.env._('HR must approve the Daily Attendance before downloading its PDF.'))
        rows = []
        status_field = self.env['restaurant.attendance.entry']._fields['status']
        labels = dict(status_field._description_selection(self.env))
        for line in self.line_ids.sorted(lambda item: (item.employee_id.name or '', item.id)):
            overtime = '-'
            if line.status == 'overtime_day':
                overtime = self.env._('1 Day')
            elif line.overtime_hours:
                overtime = self.env._('%s Hours', _number(line.overtime_hours))
            worked_hours = '-'
            if line.check_in and line.check_out:
                worked_hours = _number(
                    (line.check_out - line.check_in).total_seconds() / 3600
                )
            rows.append([
                line.employee_id.name,
                line.staff_shift or '-', labels.get(line.status, line.status),
                _datetime(line, line.check_in), _datetime(line, line.check_out),
                worked_hours, line.late_minutes, overtime,
                line.exception_summary or '-',
            ])
        corrections = self.line_ids.filtered(lambda line: line.correction_count)
        updated = '; '.join(filter(None, corrections.mapped('last_correction_reason'))) or False
        title = self.env._('DAILY ATTENDANCE')
        content = _render_official_pdf(
            self, title,
            [(self.env._('Branch'), self.branch_id.name),
             (self.env._('Attendance Date'), _date(self, self.attendance_date)),
             (self.env._('Status'), _selection_label(self, 'state')),
             (self.env._('Approved By'), self.approved_by.name or '-')],
            [(self.env._('Staff Attendance'),
              ['Employee', 'Shift', 'Status', 'Check In', 'Check Out', 'Total Hours', 'Late Min', 'Overtime', 'Exception'],
              rows, [42, 22, 28, 34, 34, 20, 18, 20, 42])],
            updated=updated,
        )
        suffix = ' - Updated' if updated else ''
        return _download(
            self, title,
            f'Daily Attendance - {self.branch_id.name} - {self.attendance_date}{suffix}.pdf',
            content,
        )


class RestaurantDailyClosingOfficialPdf(models.Model):
    _inherit = 'restaurant.daily.closing'

    def action_download_official_pdf(self):
        self.ensure_one()
        self.check_access('read')
        if self.state != 'confirmed':
            raise UserError(self.env._('Review the Daily Closing before downloading its PDF.'))
        payment_labels = dict(self.env['restaurant.reception.payment.line']._fields[
            'payment_type'
        ]._description_selection(self.env))
        payment_rows = [[
            payment_labels.get(line.payment_type, line.payment_type),
            line.method_name or '-', _number(line.amount), line.note or '-',
        ] for line in self.payment_line_ids]
        summary_rows = [
            [self.env._('Total Sales (VAT Included)'), _number(self.reported_total_sales)],
            [self.env._('Net Sales (Excl. VAT)'), _number(self.net_sales_excluding_vat)],
            [self.env._('Calculated VAT'), _number(self.calculated_vat_amount)],
            [self.env._('Payments Total'), _number(self.payment_total)],
            [self.env._('Payment Difference'), _number(self.payment_difference)],
            [self.env._('Guests'), self.guest_count],
            [self.env._('Bills'), self.service_bill_count],
            [self.env._('Tips'), _number(self.reported_total_tips)],
            [self.env._('Discounts'), _number(self.total_discounts)],
            [self.env._('Reviews'), self.total_reviews],
        ]
        title = self.env._('DAILY CLOSING')
        content = _render_official_pdf(
            self, title,
            [(self.env._('Reference'), self.name),
             (self.env._('Branch'), self.branch_id.name),
             (self.env._('Business Date'), _date(self, self.closing_date)),
             (self.env._('Reviewed By'), self.branch_manager_id.name or '-')],
            [(self.env._('Day Summary'), ['Metric', 'Value'], summary_rows, [90, 55]),
             (self.env._('Payment Breakdown'), ['Type', 'Method', 'Amount', 'Note'], payment_rows, [45, 65, 35, 105])],
            updated=(self.env._('Correction revision %s - see Audit Trail', self.reopen_count)
                     if self.reopen_count else False),
        )
        suffix = ' - Updated' if self.reopen_count else ''
        return _download(
            self, title,
            f'Daily Closing - {self.branch_id.name} - {self.closing_date}{suffix}.pdf',
            content,
        )


class RestaurantAttendanceMonthOfficialPdf(models.Model):
    _inherit = 'restaurant.attendance.month'

    def action_download_official_pdf(self):
        self.ensure_one()
        self.check_access('read')
        if self.state != 'closed':
            raise UserError(self.env._('Close Monthly Attendance before downloading its PDF.'))
        rows = [[
            line.employee_id.name, line.job_id.name or '-', line.present_days,
            line.late_count, line.late_minutes, line.absence_days,
            line.paid_leave_days, line.unpaid_leave_days, line.day_off_days,
            line.overtime_days, line.pending_missing_rows,
        ] for line in self.line_ids.sorted(lambda item: (item.employee_id.name or '', item.id))]
        title = self.env._('MONTHLY ATTENDANCE')
        content = _render_official_pdf(
            self, title,
            [(self.env._('Branch'), self.branch_id.name),
             (self.env._('Period'), f'{_date(self, self.month_start)} - {_date(self, self.month_end)}'),
             (self.env._('Closed By'), self.closed_by.name or '-')],
            [(self.env._('Employee Summary'),
              ['Employee', 'Job', 'Present', 'Late', 'Late Min', 'Absent', 'Paid Leave', 'Unpaid Leave', 'Day Off', 'OT Days', 'Pending'],
              rows, [42, 34, 20, 18, 22, 20, 24, 27, 20, 20, 20])],
            updated=self.last_correction_reason if self.correction_count else False,
        )
        suffix = ' - Updated' if self.correction_count else ''
        return _download(
            self, title,
            f'Monthly Attendance - {self.branch_id.name} - {self.month_start}{suffix}.pdf',
            content,
        )


class RestaurantDailyStockOfficialPdf(models.Model):
    _inherit = 'restaurant.stock.daily'

    def action_download_official_pdf(self):
        self.ensure_one()
        self.check_access('read')
        if self.state != 'closed':
            raise UserError(self.env._('Close Daily Stock before downloading its PDF.'))

        sheets = self
        active_sections = self._active_sections_for_branch(self.branch_id)
        if self.section_id and active_sections:
            sheets = self.search([
                ('company_id', '=', self.company_id.id),
                ('branch_id', '=', self.branch_id.id),
                ('stock_date', '=', self.stock_date),
                ('section_id', 'in', active_sections.ids),
                ('state', '=', 'closed'),
            ])
            missing_sections = active_sections - sheets.section_id
            if missing_sections:
                raise UserError(self.env._(
                    'Close all Stock Check sections before downloading the combined PDF. '
                    'Still open or missing: %s',
                    ', '.join(missing_sections.mapped('name')),
                ))

        section_order = {'kitchen': 0, 'bar': 1, 'disposable': 2}
        sheets = sheets.sorted(lambda sheet: (
            section_order.get(sheet.section_id.section_type, 99),
            sheet.section_id.name or '',
            sheet.id,
        ))
        report_sections = []
        for sheet in sheets:
            rows = [[
                line.product_id.display_name,
                _selection_label(line, 'operation_mode'),
                line.uom_id.name or '-',
                _number(line.opening_qty), _number(line.received_qty),
                (
                    'N/A'
                    if line.operation_mode == 'consumption'
                    else _number(line.sold_qty)
                ),
                (
                    'N/A'
                    if line.operation_mode == 'consumption'
                    else _number(line.auto_complimentary_qty)
                ),
                (
                    'N/A'
                    if line.operation_mode == 'menu'
                    else _number(line.consumption_qty)
                ),
                _number(line.total_waste_qty),
                _number(line.damaged_qty), _number(line.expected_closing_qty),
                _number(line.actual_closing_qty), _number(line.difference_qty),
                _number(line.request_tomorrow_qty),
            ] for line in sheet.line_ids.sorted(
                lambda item: (item.product_id.display_name or '', item.id)
            )]
            report_sections.append((
                sheet.section_id.name or self.env._('Stock Check'),
                ['Product', 'Item Type', 'UoM', 'Opening', 'Received', 'Sold',
                 'Complimentary', 'Consumption', 'Waste', 'Damaged',
                 'Expected', 'Actual', 'Difference', 'Request'],
                rows,
                [30, 20, 14, 16, 16, 14, 22, 23, 14, 16, 17, 15, 17, 17],
            ))

        references = ', '.join(sheets.mapped('name'))
        reviewers = ', '.join(sorted(set(sheets.mapped('reviewed_by_id.name')))) or '-'
        section_names = ', '.join(sheets.section_id.mapped('name')) or self.env._('Legacy Combined Stock')
        title = self.env._('DAILY STOCK - COMBINED STOCK CHECK')
        content = _render_official_pdf(
            self, title,
            [(self.env._('References'), references),
             (self.env._('Branch'), self.branch_id.name),
             (self.env._('Sections'), section_names),
             (self.env._('Stock Date'), _date(self, self.stock_date)),
             (self.env._('Reviewed By'), reviewers)],
            report_sections,
        )
        return _download(
            self, title,
            f'Daily Stock - {self.branch_id.name} - {self.stock_date}.pdf',
            content,
        )


class RestaurantStockEventSheetOfficialPdf(models.Model):
    _inherit = 'restaurant.stock.event.sheet'

    def action_download_official_pdf(self):
        self.ensure_one()
        self.check_access('read')
        if self.state != 'approved':
            raise UserError(self.env._('Approve the Service Event sheet before downloading its PDF.'))
        event_labels = dict(self.env['restaurant.stock.event']._fields[
            'event_type'
        ]._description_selection(self.env))
        prep_labels = dict(self.env['restaurant.stock.event']._fields[
            'preparation_status'
        ]._description_selection(self.env))
        rows = [[
            event_labels.get(line.event_type, line.event_type),
            line.menu_item_id.display_name, line.variant_id.display_name,
            _number(line.quantity), _number(line.reported_amount),
            line.reason, prep_labels.get(line.preparation_status, line.preparation_status),
        ] for line in self.line_ids]
        title = self.env._('CANCELLATION & COMPLIMENTARY')
        content = _render_official_pdf(
            self, title,
            [(self.env._('Reference'), self.name),
             (self.env._('Branch'), self.branch_id.name),
             (self.env._('Service Date'), _date(self, self.service_date)),
             (self.env._('Order / Bill Reference'), self.order_reference or '-'),
             (self.env._('Receipt Proofs'), len(self.receipt_proof_ids)),
             (self.env._('Approved By'), self.manager_id.name or '-')],
            [(self.env._('Approved Items'),
              ['Type', 'Item', 'Size', 'Qty', 'Amount', 'Reason', 'Preparation'],
              rows, [34, 52, 42, 20, 28, 56, 34])],
            notice=self.env._('Receipt proofs remain attached to this audited Odoo sheet.'),
        )
        return _download(
            self, title,
            f'Service Events - {self.branch_id.name} - {self.service_date} - {self.name}.pdf',
            content,
        )


class RestaurantTipsDistributionOfficialPdf(models.Model):
    _inherit = 'restaurant.tips.distribution'

    def action_download_official_pdf(self):
        self.ensure_one()
        self.check_access('read')
        if self.state != 'prepared':
            raise UserError(self.env._('Prepare Tips Distribution before downloading its PDF.'))
        rows = [[
            line.branch_id.name or '-', line.employee_id.name,
            line.waiter_rank or '-', 'Yes' if line.is_top_waiter else 'No',
            'Yes' if line.is_manual_double else 'No', line.share_weight,
            _number(line.amount),
        ] for line in self.allocation_line_ids]
        title = self.env._('TIPS DISTRIBUTION')
        content = _render_official_pdf(
            self, title,
            [(self.env._('Reference'), self.name),
             (self.env._('Cycle'), f'{_date(self, self.cycle_start)} - {_date(self, self.cycle_end)}'),
             (self.env._('Global Pool'), _number(self.global_pool)),
             (self.env._('Ordinary Share'), _number(self.ordinary_share)),
             (self.env._('Total Units'), self.total_units),
             (self.env._('Prepared By'), self.prepared_by_id.name or '-')],
            [(self.env._('Prepared Staff Shares'),
              ['Branch', 'Employee', 'Waiter Rank', 'Top Waiter', 'Manual Double', 'Units', 'Amount'],
              rows, [42, 58, 28, 28, 32, 20, 34])],
            notice=self.env._(
                'Preparation only. This PDF does not record a payment, payslip, or journal entry.'
            ),
        )
        return _download(
            self, title,
            f'Tips Distribution - {self.cycle_start} - {self.cycle_end}.pdf',
            content,
        )

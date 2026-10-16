import logging

from odoo import Command, SUPERUSER_ID, api


_logger = logging.getLogger(__name__)


def migrate(cr, version):
    """Move open legacy line receipts to their one-order sheet header.

    Approved and other historical sheets remain untouched. A Draft with
    conflicting line references is also left unchanged for manual review
    instead of guessing which order owns its items.
    """
    env = api.Environment(cr, SUPERUSER_ID, {})
    Sheet = env["restaurant.stock.event.sheet"].sudo()
    migrated = Sheet.browse()
    conflicted = Sheet.browse()

    for sheet in Sheet.search([("state", "=", "draft")], order="id"):
        line_references = {
            (reference or "").strip()
            for reference in sheet.line_ids.mapped("order_reference")
            if (reference or "").strip()
        }
        sheet_reference = (sheet.order_reference or "").strip()
        if not sheet_reference and len(line_references) == 1:
            sheet_reference = next(iter(line_references))

        if len(line_references) > 1 or (
            sheet_reference
            and line_references
            and line_references != {sheet_reference}
        ):
            conflicted |= sheet
            continue

        attachments = sheet.line_ids.attachment_ids
        unique_attachments = env["ir.attachment"].browse()
        seen_checksums = set()
        for attachment in attachments.sorted("id"):
            key = attachment.checksum or "attachment:%s" % attachment.id
            if key in seen_checksums:
                continue
            seen_checksums.add(key)
            unique_attachments |= attachment
        values = {}
        if sheet_reference and sheet.order_reference != sheet_reference:
            values["order_reference"] = sheet_reference
        if unique_attachments and not sheet.receipt_proof_ids:
            values["receipt_proof_ids"] = [
                Command.set(unique_attachments.ids)
            ]
        if values:
            sheet.with_context(
                restaurant_stock_event_sheet_system_write=True
            ).write(values)
        movable_attachments = unique_attachments.filtered(
            lambda attachment: (
                not attachment.res_model
                or not attachment.res_id
                or (
                    attachment.res_model == "restaurant.stock.event"
                    and attachment.res_id in sheet.line_ids.ids
                )
            )
        )
        if movable_attachments:
            movable_attachments.write({
                "res_model": sheet._name,
                "res_id": sheet.id,
            })
        if sheet_reference:
            sheet.line_ids.with_context(
                restaurant_stock_event_system_write=True
            ).write({"order_reference": sheet_reference})
        if values or sheet_reference:
            migrated |= sheet

    _logger.info(
        "Shared order/receipt migration updated %s open sheets; %s Draft "
        "sheets with conflicting references were left unchanged for review. "
        "Approved history was not modified.",
        len(migrated),
        len(conflicted),
    )

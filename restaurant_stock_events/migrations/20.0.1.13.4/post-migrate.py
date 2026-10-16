import logging

from odoo import Command, SUPERUSER_ID, api


_logger = logging.getLogger(__name__)


def migrate(cr, version):
    """Deduplicate identical receipt uploads on open one-order sheets."""
    env = api.Environment(cr, SUPERUSER_ID, {})
    Sheet = env["restaurant.stock.event.sheet"].sudo()
    updated = Sheet.browse()

    for sheet in Sheet.search([("state", "=", "draft")], order="id"):
        unique_attachments = env["ir.attachment"].browse()
        seen_checksums = set()
        for attachment in sheet.receipt_proof_ids.sorted("id"):
            key = attachment.checksum or "attachment:%s" % attachment.id
            if key in seen_checksums:
                continue
            seen_checksums.add(key)
            unique_attachments |= attachment

        if unique_attachments.ids != sheet.receipt_proof_ids.sorted("id").ids:
            sheet.with_context(
                restaurant_stock_event_sheet_system_write=True
            ).write({
                "receipt_proof_ids": [Command.set(unique_attachments.ids)],
            })
            updated |= sheet

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

    _logger.info(
        "Shared receipt cleanup deduplicated %s open sheets. Approved "
        "history and attachment records were not deleted.",
        len(updated),
    )

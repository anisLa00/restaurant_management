from odoo import SUPERUSER_ID, api


def migrate(cr, version):
    env = api.Environment(cr, SUPERUSER_ID, {})
    Event = env["restaurant.stock.event"].sudo()
    Sheet = env["restaurant.stock.event.sheet"].sudo().with_context(
        restaurant_stock_event_sheet_migration=True
    )
    state_map = {
        "draft": "draft",
        "kitchen_review": "stockkeeper_review",
        "manager_review": "manager_review",
        "approved": "approved",
        "rejected": "rejected",
        "cancelled": "cancelled",
    }
    for event in Event.search([("sheet_id", "=", False)], order="id"):
        sheet = Sheet.create({
            "name": event.name,
            "branch_id": event.branch_id.id,
            "service_date": event.service_date,
            "order_reference": event.order_reference,
            "closing_id": event.closing_id.id,
            "notes": event.note,
            "kitchen_note": event.kitchen_note,
            "manager_note": event.manager_note,
            "state": state_map[event.state],
            "reception_user_id": event.reception_user_id.id,
            "stockkeeper_id": event.kitchen_checked_by_id.id,
            "stockkeeper_reviewed_at": event.kitchen_checked_at,
            "manager_id": event.manager_id.id,
            "manager_reviewed_at": event.manager_reviewed_at,
        })
        event.with_context(restaurant_stock_event_system_write=True).write({
            "sheet_id": sheet.id,
        })

from odoo import SUPERUSER_ID, api


def migrate(cr, version):
    """Remove the parallel requisition flow and manager request creation."""
    env = api.Environment(cr, SUPERUSER_ID, {})

    obsolete_xmlids = [
        "restaurant_purchase_requisition_menu",
        "restaurant_purchase_requisition_action",
        "restaurant_purchase_requisition_view_list",
        "restaurant_purchase_requisition_view_form",
        "restaurant_purchase_requisition_view_search",
        "restaurant_purchase_requisition_offer_view_list",
        "restaurant_purchase_requisition_offer_view_form",
        "seq_restaurant_purchase_requisition",
        "stock_request_manager_create",
        "stock_request_manager_delete",
        "stock_request_line_manager_create",
        "stock_request_line_manager_delete",
        "purchase_requisition_manager",
        "purchase_requisition_purchasing_read",
        "purchase_requisition_purchasing_update",
        "purchase_requisition_central_read",
        "purchase_requisition_central_update",
        "purchase_requisition_operations",
        "purchase_requisition_owner",
        "purchase_requisition_system",
        "purchase_requisition_line_manager",
        "purchase_requisition_line_purchasing",
        "purchase_requisition_line_central",
        "purchase_requisition_line_operations",
        "purchase_requisition_line_owner",
        "purchase_requisition_line_system",
        "purchase_requisition_offer_purchasing",
        "purchase_requisition_offer_manager",
        "purchase_requisition_offer_central",
        "purchase_requisition_offer_operations",
        "purchase_requisition_offer_owner",
        "purchase_requisition_offer_system",
        "purchase_requisition_offer_line_purchasing",
        "purchase_requisition_offer_line_manager",
        "purchase_requisition_offer_line_central",
        "purchase_requisition_offer_line_operations",
        "purchase_requisition_offer_line_owner",
        "purchase_requisition_offer_line_system",
    ]

    # Unlink through each record's own model so menus, actions, views,
    # sequences, and access grants disappear cleanly on upgraded databases.
    for name in obsolete_xmlids:
        record = env.ref(f"restaurant_stock.{name}", raise_if_not_found=False)
        if record:
            record.unlink()

    # These tables contain no live business records.  The active purchasing
    # path remains restaurant.stock.request, generated from Daily Stock.
    cr.execute("DROP TABLE IF EXISTS restaurant_purchase_requisition_offer_line CASCADE")
    cr.execute("DROP TABLE IF EXISTS restaurant_purchase_requisition_offer CASCADE")
    cr.execute("DROP TABLE IF EXISTS restaurant_purchase_requisition_line CASCADE")
    cr.execute("DROP TABLE IF EXISTS restaurant_purchase_requisition CASCADE")


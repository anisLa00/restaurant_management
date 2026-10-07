{
    "name": "Restaurant Stock Events",
    "summary": (
        "Auditable cancellation and complimentary workflow with Daily Stock "
        "integration"
    ),
    "version": "20.0.1.12.0",
    "category": "Services",
    "license": "LGPL-3",
    "author": "Restaurant Project",
    "depends": [
        "restaurant_reception",
        "restaurant_stock",
        "mail",
    ],
    "data": [
        "security/ir.access.csv",
        "data/ir_sequence_data.xml",
        "views/restaurant_mapping_profile_views.xml",
        "views/restaurant_stock_event_views.xml",
        "views/restaurant_reception_closing_views.xml",
        "views/restaurant_stock_daily_views.xml",
        "views/restaurant_stock_event_menus.xml",
    ],
    "application": False,
    "installable": True,
}

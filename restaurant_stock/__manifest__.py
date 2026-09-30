{
    'name': 'Restaurant Stock',
    'summary': 'Branch stock control, opening/closing counts and replenishment workflow',
    'version': '20.0.1.1.0',
    'category': 'Inventory',
    'license': 'LGPL-3',
    'author': 'Restaurant Project',

    'depends': [
        'restaurant_core',
        'stock',
        'purchase_stock',
        'mail',
    ],

    'data': [
    'security/restaurant_stock_security.xml',
    'security/ir.access.csv',
    'data/ir_sequence_data.xml',
    'data/restaurant_warehouse_data.xml',
    'views/restaurant_stock_daily_views.xml',
    'views/restaurant_stock_request_views.xml',
    'views/restaurant_product_views.xml',
    'views/restaurant_stock_menus.xml',
    ],

    'application': False,
    'installable': True,
}

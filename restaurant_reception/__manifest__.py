{
    'name': 'Restaurant Reception',
    'summary': 'Daily closings, waiter sales and attendance review staging',
    'version': '20.0.1.1.0',
    'category': 'Services',
    'author': 'Restaurant Project',
    'license': 'LGPL-3',
    'depends': ['restaurant_core', 'hr', 'hr_attendance', 'mail'],
    'data': [
        'security/ir.access.csv',
        'data/ir_sequence_data.xml',
        'views/restaurant_daily_closing_views.xml',
        'views/restaurant_waiter_daily_line_views.xml',
        'views/restaurant_attendance_entry_views.xml',
        'views/restaurant_reception_menus.xml',
    ],
    'application': False,
    'installable': True,
}

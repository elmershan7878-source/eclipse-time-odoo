{
    'name': 'Operational Risk Monitor',
    'version': '19.0.1.0.2',
    'summary': 'Detects failing scheduled actions, undelivered emails, rejected '
               'e-invoices, late deliveries, overdue receivables and sales drops, '
               'prioritises them P0-P3 and tracks their resolution.',
    'author': 'Eclipse Time',
    'maintainer': 'Eclipse Time',
    'support': 'elmershan7878@gmail.com',
    'website': 'https://github.com/elmershan7878-source/eclipse-time-odoo',
    'category': 'Productivity',
    'license': 'LGPL-3',
    'depends': ['base_setup', 'mail'],
    'data': [
        'security/security.xml',
        'security/ir.model.access.csv',
        'data/risk_check_data.xml',
        'data/ir_cron_data.xml',
        'views/digest_templates.xml',
        'views/risk_signal_views.xml',
        'views/risk_check_views.xml',
        'views/risk_metric_views.xml',
        'views/res_config_settings_views.xml',
        'views/menus.xml',
    ],
    'images': ['static/description/banner.png'],
    'installable': True,
    'application': True,
}

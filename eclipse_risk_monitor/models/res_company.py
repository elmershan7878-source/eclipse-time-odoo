from odoo import fields, models


class ResCompany(models.Model):
    _inherit = 'res.company'

    eclipse_risk_user_id = fields.Many2one(
        'res.users', string='Risk Monitor Responsible',
        help='Receives a to-do activity for every new P0/P1 signal of this company.')
    eclipse_risk_digest = fields.Boolean(
        string='Daily Risk Digest', default=True,
        help='Send a daily email with the open P0-P2 signals to the Risk Monitor managers of this company.')

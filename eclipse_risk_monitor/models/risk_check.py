from odoo import api, fields, models

from .scoring import WEIGHT_LIMIT, clamp_weight


class EclipseRiskCheck(models.Model):
    _name = 'eclipse.risk.check'
    _description = 'Risk Check'
    _order = 'sequence, id'

    name = fields.Char(required=True, translate=True)
    code = fields.Char(required=True, readonly=True)
    sequence = fields.Integer(default=10)
    active = fields.Boolean(default=True)
    area = fields.Selection([
        ('technical', 'Technical'),
        ('financial', 'Financial'),
        ('operational', 'Operational'),
        ('customer', 'Customer'),
    ], required=True, readonly=True)
    required_model = fields.Char(
        readonly=True,
        help='Technical model this check needs. When the related app is not installed the check is skipped.')
    description = fields.Text(translate=True, readonly=True)
    confidence = fields.Float(
        default=1.0, readonly=True,
        help='Certainty of the rule: 1.0 for facts read from the database, lower for statistical deviations.')
    threshold = fields.Float(
        help='Main threshold of the check (meaning shown in the description).')
    grace_days = fields.Integer(help='Days of tolerance before something counts as late or overdue.')
    weight = fields.Float(
        readonly=True, digits=(3, 2),
        help='Learned adjustment from user feedback: +0.05 per "useful", -0.05 per "not useful", '
             'bounded to +/-%.2f.' % WEIGHT_LIMIT)
    available = fields.Boolean(compute='_compute_available')
    last_run = fields.Datetime(readonly=True)
    last_error = fields.Char(readonly=True)
    signal_count = fields.Integer(compute='_compute_signal_count')

    _sql_constraints = [
        ('code_unique', 'unique(code)', 'The check code must be unique.'),
    ]

    @api.depends('required_model')
    def _compute_available(self):
        for check in self:
            check.available = not check.required_model or check.required_model in self.env

    def _compute_signal_count(self):
        data = self.env['eclipse.risk.signal']._read_group(
            [('check_id', 'in', self.ids), ('state', 'in', ('open', 'acknowledged'))],
            ['check_id'], ['__count'])
        counts = {check.id: count for check, count in data}
        for check in self:
            check.signal_count = counts.get(check.id, 0)

    def _apply_feedback(self, delta):
        for check in self.sudo():
            check.weight = clamp_weight((check.weight or 0.0) + delta)

    def action_reset_weight(self):
        self.sudo().write({'weight': 0.0})

    def action_view_signals(self):
        action = self.env['ir.actions.act_window']._for_xml_id(
            'eclipse_risk_monitor.action_eclipse_risk_signal')
        action['domain'] = [('check_id', 'in', self.ids)]
        action['context'] = {'search_default_filter_active': 1}
        return action

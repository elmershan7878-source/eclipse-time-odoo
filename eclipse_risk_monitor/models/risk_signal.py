from odoo import api, fields, models
from odoo.exceptions import UserError

from .scoring import WEIGHT_STEP

PRIORITIES = [('p0', 'P0 - Critical'), ('p1', 'P1 - High'), ('p2', 'P2 - Medium'), ('p3', 'P3 - Low')]
AREAS = [('technical', 'Technical'), ('financial', 'Financial'),
         ('operational', 'Operational'), ('customer', 'Customer')]


class EclipseRiskSignal(models.Model):
    _name = 'eclipse.risk.signal'
    _description = 'Risk Signal'
    _inherit = ['mail.thread', 'mail.activity.mixin']
    _order = 'state_sequence, score desc, last_seen desc, id desc'

    name = fields.Char(required=True, readonly=True, tracking=True)
    company_id = fields.Many2one('res.company', required=True, index=True, readonly=True)
    check_id = fields.Many2one('eclipse.risk.check', required=True, ondelete='cascade', readonly=True, index=True)
    area = fields.Selection(AREAS, required=True, readonly=True)
    fingerprint = fields.Char(required=True, readonly=True, index=True,
                              help='Stable key of the problem: same problem, same signal.')
    state = fields.Selection([
        ('open', 'Open'),
        ('acknowledged', 'In progress'),
        ('resolved', 'Resolved'),
        ('dismissed', 'Dismissed'),
    ], default='open', required=True, readonly=True, tracking=True, index=True)
    state_sequence = fields.Integer(compute='_compute_state_sequence', store=True)
    priority = fields.Selection(PRIORITIES, required=True, readonly=True, tracking=True, index=True)
    score = fields.Integer(readonly=True, aggregator='max')
    severity = fields.Integer(readonly=True)
    confidence = fields.Float(readonly=True, digits=(3, 2))
    occurrences = fields.Integer(readonly=True, help='Events counted by the check for this problem.')
    occurrences_24h = fields.Integer(readonly=True)
    occurrences_prev_24h = fields.Integer(readonly=True)
    accelerating = fields.Boolean(readonly=True, help='At least 3 events in 24 h and 50 % more than the previous 24 h.')
    compound = fields.Boolean(string='Compound risk', readonly=True,
                              help='Related open problems exist in other areas in the last 48 h.')
    related_signal_ids = fields.Many2many(
        'eclipse.risk.signal', 'eclipse_risk_signal_rel', 'signal_id', 'related_id',
        string='Related signals', readonly=True)
    problem = fields.Text(readonly=True)
    cause = fields.Text(string='Probable cause', readonly=True)
    recommendation = fields.Text(string='Recommended next step', readonly=True)
    score_explanation = fields.Text(readonly=True)
    res_model = fields.Char(readonly=True)
    res_id = fields.Many2oneReference(model_field='res_model', readonly=True)
    user_id = fields.Many2one('res.users', string='Responsible', tracking=True,
                              domain="[('share', '=', False)]")
    first_seen = fields.Datetime(readonly=True)
    last_seen = fields.Datetime(readonly=True)
    resolved_on = fields.Datetime(readonly=True)
    verified = fields.Boolean(
        string='Resolution verified', readonly=True,
        help='The check ran again after the problem was handled and no longer detected it.')
    reopen_count = fields.Integer(readonly=True)
    feedback = fields.Selection([('useful', 'Useful'), ('not_useful', 'Not useful')], readonly=True)
    notified = fields.Boolean(readonly=True)

    _fingerprint_unique = models.Constraint(
        'unique(company_id, fingerprint)',
        'A problem can only have one signal per company.',
    )

    @api.depends('state')
    def _compute_state_sequence(self):
        order = {'open': 1, 'acknowledged': 2, 'resolved': 3, 'dismissed': 4}
        for signal in self:
            signal.state_sequence = order.get(signal.state, 9)

    # ------------------------------------------------------------------ actions
    def action_acknowledge(self):
        for signal in self:
            if signal.state not in ('open', 'acknowledged'):
                raise UserError(self.env._('Only open signals can be taken in charge.'))
            signal.write({'state': 'acknowledged', 'user_id': signal.user_id.id or self.env.user.id})
        return True

    def action_mark_useful(self):
        self._give_feedback('useful')
        return True

    def action_dismiss(self):
        self._give_feedback('not_useful')
        self.write({'state': 'dismissed'})
        self.activity_unlink(['mail.mail_activity_data_todo'])
        return True

    def action_reopen(self):
        self.filtered(lambda s: s.state == 'dismissed').write({'state': 'open'})
        return True

    def _give_feedback(self, value):
        delta = WEIGHT_STEP if value == 'useful' else -WEIGHT_STEP
        for signal in self:
            if signal.feedback == value:
                continue
            # changing an earlier (opposite) opinion also cancels its effect
            signal.check_id._apply_feedback(delta * (2 if signal.feedback else 1))
            signal.feedback = value

    def action_open_record(self):
        self.ensure_one()
        if not self.res_model or not self.res_id or self.res_model not in self.env:
            raise UserError(self.env._('This signal is not linked to a single record.'))
        record = self.env[self.res_model].browse(self.res_id).exists()
        if not record:
            raise UserError(self.env._('The related record no longer exists.'))
        record.check_access('read')
        return {
            'type': 'ir.actions.act_window',
            'res_model': self.res_model,
            'res_id': self.res_id,
            'view_mode': 'form',
            'target': 'current',
        }

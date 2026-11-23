from odoo import fields, models


class ResConfigSettings(models.TransientModel):
    _inherit = 'res.config.settings'

    eclipse_risk_user_id = fields.Many2one(
        related='company_id.eclipse_risk_user_id', readonly=False)
    eclipse_risk_digest = fields.Boolean(
        related='company_id.eclipse_risk_digest', readonly=False)

    def action_eclipse_risk_scan_now(self):
        self.env['eclipse.risk.engine'].check_access_manager()
        self.env['eclipse.risk.engine'].sudo()._scan_company(self.env.company)
        return self.env['ir.actions.act_window']._for_xml_id(
            'eclipse_risk_monitor.action_eclipse_risk_signal')

    def action_eclipse_risk_export(self):
        return self.env['eclipse.risk.engine'].action_export_feed()

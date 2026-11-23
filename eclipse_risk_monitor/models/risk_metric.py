from odoo import api, fields, models


class EclipseRiskMetric(models.Model):
    _name = 'eclipse.risk.metric'
    _description = 'Daily Operational Metric'
    _order = 'day desc, metric'
    _rec_name = 'metric'

    company_id = fields.Many2one('res.company', required=True, index=True, readonly=True)
    day = fields.Date(required=True, index=True, readonly=True)
    metric = fields.Selection([
        ('cron_failing', 'Failing scheduled actions'),
        ('cron_overdue', 'Overdue scheduled actions'),
        ('mail_exceptions_24h', 'Undelivered emails (24 h)'),
        ('einvoice_errors', 'E-invoicing documents with errors'),
        ('ar_overdue_amount', 'Overdue receivables (company currency)'),
        ('ar_overdue_count', 'Overdue customer invoices'),
        ('late_deliveries', 'Late outgoing deliveries'),
        ('negative_quants', 'Negative stock lines'),
        ('sales_orders_confirmed', 'Sales orders confirmed (previous day)'),
        ('payment_errors_24h', 'Payment transactions in error (24 h)'),
    ], required=True, readonly=True)
    value = fields.Float(readonly=True)
    partial = fields.Boolean(readonly=True, help='The day is not over: the value can still change.')

    _sql_constraints = [
        ('metric_day_unique', 'unique(company_id, day, metric)', 'One value per metric, day and company.'),
    ]

    @api.model
    def _store(self, company, day, values, partial=True):
        existing = self.search([('company_id', '=', company.id), ('day', '=', day)])
        by_metric = {rec.metric: rec for rec in existing}
        for metric, value in values.items():
            if value is None:
                continue
            rec = by_metric.get(metric)
            if rec:
                rec.write({'value': value, 'partial': partial})
            else:
                self.create({'company_id': company.id, 'day': day, 'metric': metric,
                             'value': value, 'partial': partial})

    @api.model
    def _value(self, company, day, metric):
        rec = self.search([('company_id', '=', company.id), ('day', '=', day), ('metric', '=', metric)], limit=1)
        return rec.value if rec else None

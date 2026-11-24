import json
import base64
from datetime import datetime, time, timedelta

from odoo import fields
from odoo.exceptions import AccessError
from odoo.tests import TransactionCase, tagged, new_test_user

from odoo.addons.eclipse_risk_monitor.models import scoring


@tagged('post_install', '-at_install')
class TestScoring(TransactionCase):

    def test_formula_matches_engine(self):
        # severity 3, 6 occurrences, certainty 1.0 -> 41 + 8 + 20 = 69 -> P1
        self.assertEqual(scoring.compute_score(3, 6, 1.0), 69)
        self.assertEqual(scoring.priority_from_score(69, 3), 'p1')
        # critical severity is at least P1 even with a low score
        self.assertEqual(scoring.priority_from_score(30, 4), 'p1')
        # compound (2 other areas = +30) and acceleration (+15) are capped at 100
        self.assertEqual(scoring.compute_score(4, 20, 1.0, 0.2, 2, True), 100)
        self.assertEqual(scoring.compound_bonus(5), 30)
        self.assertEqual(scoring.priority_from_score(39, 2), 'p3')
        self.assertEqual(scoring.priority_from_score(40, 2), 'p2')
        self.assertEqual(scoring.priority_from_score(85, 2), 'p0')

    def test_weight_is_bounded(self):
        self.assertEqual(scoring.clamp_weight(0.9), 0.2)
        self.assertEqual(scoring.clamp_weight(-0.9), -0.2)
        self.assertEqual(scoring.compute_score(1, 0, 0.0, -5), 0)

    def test_acceleration(self):
        self.assertTrue(scoring.is_accelerating(3, 1))
        self.assertFalse(scoring.is_accelerating(2, 0))
        self.assertFalse(scoring.is_accelerating(6, 4))


@tagged('post_install', '-at_install')
class TestRiskMonitor(TransactionCase):

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.engine = cls.env['eclipse.risk.engine']
        cls.Signal = cls.env['eclipse.risk.signal']
        cls.company = cls.env.ref('base.main_company')
        cls.now = fields.Datetime.now()
        # neutralise the scheduled actions of the test database
        cls.env['ir.cron'].search([]).write({'nextcall': cls.now + timedelta(hours=1), 'failure_count': 0})
        # start from a clean outgoing queue (installing apps can leave failed emails behind)
        cls.env['mail.mail'].search([('state', '=', 'exception')]).unlink()
        cls.cron = cls.env.ref('eclipse_risk_monitor.ir_cron_risk_digest')
        cls.manager = new_test_user(cls.env, login='risk_manager', groups='base.group_user,eclipse_risk_monitor.group_risk_manager',
                                    email='manager@example.com')
        cls.user = new_test_user(cls.env, login='risk_user', groups='base.group_user,eclipse_risk_monitor.group_risk_user')

    def _scan(self, now=None):
        return self.engine.sudo()._scan_company(self.company, now=now or self.now)

    def _signal(self, fingerprint):
        return self.Signal.sudo().search([('company_id', '=', self.company.id), ('fingerprint', '=', fingerprint)])

    def _failed_mail(self, count, model=False, reason='SMTP 550 mailbox unavailable for client@example.com'):
        return self.env['mail.mail'].create([{
            'subject': 'Test %s' % i, 'body_html': '<p>x</p>', 'email_to': 'client@example.com',
            'state': 'exception', 'failure_reason': reason, 'model': model,
        } for i in range(count)])

    # ------------------------------------------------------------ detectors
    def test_cron_failure_detected_then_verified(self):
        self.cron.write({'failure_count': 3, 'first_failure_date': self.now - timedelta(hours=5)})
        self._scan()
        signal = self._signal('TECH_CRON:fail:%s' % self.cron.id)
        self.assertEqual(len(signal), 1)
        self.assertEqual(signal.state, 'open')
        self.assertEqual(signal.severity, 3)
        self.assertEqual(signal.res_model, 'ir.cron')
        self.assertIn('Run Manually', signal.recommendation)
        # the problem is fixed: next scan resolves and verifies it
        self.cron.write({'failure_count': 0, 'first_failure_date': False})
        self._scan(self.now + timedelta(hours=1))
        self.assertEqual(signal.state, 'resolved')
        self.assertTrue(signal.verified)
        # it comes back: same signal is reopened, not duplicated
        self.cron.write({'failure_count': 1})
        self._scan(self.now + timedelta(hours=2))
        self.assertEqual(signal.state, 'open')
        self.assertEqual(signal.reopen_count, 1)
        self.assertEqual(len(self._signal('TECH_CRON:fail:%s' % self.cron.id)), 1)

    def test_stalled_cron_worker_is_critical(self):
        crons = self.env['ir.cron'].search([('active', '=', True)], limit=3)
        crons.write({'nextcall': self.now - timedelta(days=2), 'interval_type': 'hours', 'interval_number': 1})
        self._scan()
        signal = self._signal('TECH_CRON:stalled')
        self.assertEqual(signal.severity, 4)
        self.assertIn(signal.priority, ('p0', 'p1'))
        self.assertEqual(signal.occurrences, 3)

    def test_mail_failures_sanitised_and_compound(self):
        # only the checks under test: demo data may contain real overdue invoices
        # that would (correctly) turn the email signal into a compound risk
        self.env['eclipse.risk.check'].search([('code', 'not in', ('TECH_MAIL', 'TECH_CRON'))]).active = False
        self._failed_mail(2)
        self._scan()
        mail = self._signal('TECH_MAIL:exceptions')
        self.assertEqual(mail.severity, 2)
        self.assertNotIn('client@example.com', mail.cause)
        self.assertIn('[email]', mail.cause)
        self.assertFalse(mail.compound)
        # same area (technical) links do not make a compound risk ...
        self.cron.write({'failure_count': 5})
        self._scan()
        self.assertFalse(mail.compound)
        self.assertIn(self._signal('TECH_CRON:fail:%s' % self.cron.id), mail.related_signal_ids)
        # ... an invoice email failing raises the severity
        if 'account.move' in self.env:
            self._failed_mail(1, model='account.move')
            self._scan()
            self.assertEqual(mail.severity, 3)
            self.assertEqual(mail.occurrences, 3)

    def test_mail_acceleration(self):
        older = self._failed_mail(1)
        self.env.cr.execute("UPDATE mail_mail SET create_date = %s WHERE id = %s",
                            (self.now - timedelta(hours=30), older.id))
        self._failed_mail(4)
        self._scan()
        signal = self._signal('TECH_MAIL:exceptions')
        self.assertTrue(signal.accelerating)
        self.assertEqual(signal.occurrences_prev_24h, 1)

    def test_unavailable_checks_are_skipped(self):
        checks = self.env['eclipse.risk.check'].search([])
        self.assertEqual(len(checks), 8)
        for check in checks:
            self.assertEqual(check.available, (not check.required_model) or check.required_model in self.env)
        self._scan()
        self.assertFalse(checks.filtered('last_error'), checks.mapped('last_error'))

    def test_overdue_receivables(self):
        if 'account.move' not in self.env:
            self.skipTest('Accounting not installed')
        partner = self.env['res.partner'].create({'name': 'Overdue customer'})
        today = fields.Date.today()
        invoice = self.env['account.move'].create({
            'move_type': 'out_invoice', 'partner_id': partner.id,
            'invoice_date': today - timedelta(days=60), 'invoice_date_due': today - timedelta(days=40),
            'invoice_line_ids': [(0, 0, {'name': 'Service', 'quantity': 1, 'price_unit': 5000, 'tax_ids': [(6, 0, [])]})],
        })
        invoice.action_post()
        invoice.invoice_date_due = today - timedelta(days=40)
        self._scan()
        signal = self._signal('FIN_AR:overdue')
        self.assertTrue(signal)
        self.assertFalse(signal.compound)
        score_alone = signal.score
        # invoice emails failing (technical area) are a direct cause: compound risk +15
        self._failed_mail(2, model='account.move')
        self._scan()
        self.assertTrue(signal.compound)
        self.assertEqual(signal.score, min(100, score_alone + 15))
        self.assertIn(self._signal('TECH_MAIL:exceptions'), signal.related_signal_ids)
        metric = self.env['eclipse.risk.metric'].search([('company_id', '=', self.company.id),
                                                          ('metric', '=', 'ar_overdue_amount'),
                                                          ('day', '=', self.now.date())])
        self.assertGreaterEqual(metric.value, 5000)
        self.assertTrue(metric.partial)

    def test_late_delivery_and_negative_stock(self):
        if 'stock.picking' not in self.env:
            self.skipTest('Inventory not installed')
        product = self.env['product.product'].create({'name': 'Risk test product', 'type': 'consu', 'is_storable': True})
        warehouse = self.env['stock.warehouse'].search([('company_id', '=', self.company.id)], limit=1)
        picking = self.env['stock.picking'].create({
            'picking_type_id': warehouse.out_type_id.id,
            'location_id': warehouse.lot_stock_id.id,
            'location_dest_id': self.env.ref('stock.stock_location_customers').id,
            'scheduled_date': self.now - timedelta(days=3),
            'move_ids': [(0, 0, {'name': product.name, 'product_id': product.id, 'product_uom_qty': 2,
                                 'product_uom': product.uom_id.id,
                                 'location_id': warehouse.lot_stock_id.id,
                                 'location_dest_id': self.env.ref('stock.stock_location_customers').id})],
        })
        picking.action_confirm()
        picking.scheduled_date = self.now - timedelta(days=3)
        self.env['stock.quant']._update_available_quantity(product, warehouse.lot_stock_id, -4)
        self._scan()
        late = self._signal('OPS_LATE:late')
        self.assertTrue(late)
        self.assertGreaterEqual(late.occurrences, 1)
        negative = self._signal('OPS_NEGSTOCK:negative')
        self.assertIn('Risk test product', negative.problem)
        # late deliveries and negative stock are linked, but in the same area
        self.assertIn(negative, late.related_signal_ids)

    def test_sales_drop(self):
        if 'sale.order' not in self.env:
            self.skipTest('Sales not installed')
        company = self.env['res.company'].create({'name': 'Sales drop test company'})
        partner = self.env['res.partner'].create({'name': 'Buyer'})
        yesterday = self.now.date() - timedelta(days=1)
        vals = []
        for _week in range(1, 5):
            vals += [{'partner_id': partner.id, 'company_id': company.id} for _i in range(6)]
        orders = self.env['sale.order'].with_company(company).create(vals)
        for index, order in enumerate(orders):
            week = index // 6 + 1
            order.write({'state': 'sale',
                         'date_order': datetime.combine(yesterday - timedelta(weeks=week), time(10, 0))})
        self.engine.sudo()._scan_company(company, now=self.now)
        signal = self.Signal.sudo().search([('company_id', '=', company.id), ('fingerprint', '=', 'CUS_SALES:drop')])
        self.assertTrue(signal)
        self.assertAlmostEqual(signal.confidence, 0.7)
        self.assertIn('-100', signal.problem)
        # the main company is unaffected
        self.assertFalse(self._signal('CUS_SALES:drop').filtered(lambda s: s.company_id == company))

    def test_verifactu_rejection_uses_latest_record(self):
        if 'l10n_es_edi_verifactu.document' not in self.env:
            self.skipTest('Veri*Factu not installed')
        Doc = self.env['l10n_es_edi_verifactu.document'].sudo()
        partner = self.env['res.partner'].create({'name': 'Cliente VeriFactu'})
        moves = self.env['account.move'].create([{
            'move_type': 'out_invoice', 'partner_id': partner.id,
            'invoice_line_ids': [(0, 0, {'name': 'x', 'quantity': 1, 'price_unit': 100})],
        } for _i in range(2)])
        Doc.create([
            {'company_id': self.company.id, 'move_id': moves[0].id, 'document_type': 'submission',
             'state': 'rejected', 'errors': '<p>Error 1100 for cliente@example.com</p>'},
            {'company_id': self.company.id, 'move_id': moves[1].id, 'document_type': 'submission',
             'state': 'rejected'},
        ])
        # the second invoice was corrected and accepted afterwards
        self.env.cr.execute("UPDATE l10n_es_edi_verifactu_document SET create_date = create_date - interval '1 hour'")
        Doc.create({'company_id': self.company.id, 'move_id': moves[1].id, 'document_type': 'submission',
                    'state': 'accepted'})
        self._scan()
        signal = self._signal('FIN_EINVOICE:verifactu:rejected')
        self.assertEqual(signal.severity, 4)
        self.assertEqual(signal.occurrences, 1)
        self.assertEqual(signal.priority, 'p1')
        self.assertNotIn('cliente@example.com', signal.cause)
        self.assertEqual(signal.res_id, moves[0].id)

    def test_spanish_translation(self):
        self.env['res.lang']._activate_lang('es_ES')
        self.env['ir.module.module']._load_module_terms(['eclipse_risk_monitor'], ['es_ES'], overwrite=True)
        self.company.partner_id.lang = 'es_ES'
        self._failed_mail(1)
        self.engine.sudo()._cron_scan()  # scheduled scans write in the company's language
        signal = self._signal('TECH_MAIL:exceptions')
        self.assertIn('no se han podido enviar', signal.name)
        self.assertIn('servidor de correo saliente', signal.recommendation)
        es = {'lang': 'es_ES'}
        self.assertEqual(self.env.ref('eclipse_risk_monitor.check_tech_mail').with_context(**es).name,
                         'Correos que no se han podido enviar')
        self.assertIn('TicketBAI y FacturaE no usan account_edi',
                      self.env.ref('eclipse_risk_monitor.check_fin_einvoice').with_context(**es).description)
        self.assertEqual(self.env.ref('eclipse_risk_monitor.menu_risk_root').with_context(**es).name,
                         'Monitor de riesgos')
        Signal_es = self.Signal.with_context(**es)
        self.assertEqual(dict(Signal_es._fields['priority']._description_selection(Signal_es.env))['p0'],
                         'P0 - Crítico')
        self.engine._cron_digest()
        digest = self.env['mail.mail'].search([('subject', 'like', '[Monitor de riesgos]')])
        self.assertTrue(digest)
        self.assertIn('Siguiente paso', digest.body_html)

    # --------------------------------------------------- isolation, access
    def test_multi_company_isolation(self):
        other = self.env['res.company'].create({'name': 'Isolated company'})
        outsider = new_test_user(self.env, login='risk_outsider', company_id=other.id, company_ids=[(6, 0, other.ids)],
                                 groups='base.group_user,eclipse_risk_monitor.group_risk_manager')
        self._failed_mail(1)
        self._scan()
        signal = self._signal('TECH_MAIL:exceptions')
        self.assertTrue(signal)
        as_outsider = self.Signal.with_user(outsider)
        self.assertFalse(as_outsider.search([]))
        with self.assertRaises(AccessError):
            as_outsider.browse(signal.id).read(['name'])
        metrics = self.env['eclipse.risk.metric'].with_user(outsider).search([])
        self.assertFalse(metrics.filtered(lambda m: m.company_id != other))
        # the main company user still sees it
        self.assertIn(signal, self.Signal.with_user(self.user).search([]))

    def test_user_cannot_configure_or_export(self):
        check = self.env.ref('eclipse_risk_monitor.check_tech_mail')
        with self.assertRaises(AccessError):
            check.with_user(self.user).write({'threshold': 1})
        with self.assertRaises(AccessError):
            self.engine.with_user(self.user).action_export_feed()
        with self.assertRaises(AccessError):
            self.env['res.config.settings'].with_user(self.user).create({}).action_eclipse_risk_scan_now()

    def test_bounded_learning_from_feedback(self):
        check = self.env.ref('eclipse_risk_monitor.check_tech_mail')
        self._failed_mail(1)
        self._scan()
        signal = self._signal('TECH_MAIL:exceptions').with_user(self.user)
        signal.action_mark_useful()
        signal.action_mark_useful()  # a second click does not count twice
        self.assertAlmostEqual(check.weight, 0.05)
        signal.action_dismiss()       # changing opinion cancels the first one
        self.assertAlmostEqual(check.weight, -0.05)
        self.assertEqual(signal.state, 'dismissed')
        for _i in range(10):
            check._apply_feedback(-0.05)
        self.assertAlmostEqual(check.weight, -0.20)

    # ------------------------------------------------- notify, digest, export
    def test_p1_creates_activity_for_responsible(self):
        self.company.eclipse_risk_user_id = self.user
        crons = self.env['ir.cron'].search([('active', '=', True)], limit=3)
        crons.write({'nextcall': self.now - timedelta(days=2), 'interval_type': 'hours', 'interval_number': 1})
        self._scan()
        signal = self._signal('TECH_CRON:stalled')
        self.assertEqual(signal.user_id, self.user)
        self.assertTrue(signal.notified)
        self.assertEqual(signal.activity_ids.user_id, self.user)
        # second scan: no duplicate activity
        self._scan(self.now + timedelta(minutes=30))
        self.assertEqual(len(signal.activity_ids), 1)
        # solved: the activity is closed
        self.env['ir.cron'].search([]).write({'nextcall': self.now + timedelta(hours=3)})
        self._scan(self.now + timedelta(hours=1))
        self.assertEqual(signal.state, 'resolved')
        self.assertFalse(signal.activity_ids)

    def test_digest_email(self):
        # language-independent: the digest is written in the company's language
        self._failed_mail(12)
        self._scan()
        signal = self._signal('TECH_MAIL:exceptions')
        Mail = self.env['mail.mail']
        before = Mail.search([('email_to', 'like', 'manager@example.com')])
        self.engine._cron_digest()
        digest = Mail.search([('email_to', 'like', 'manager@example.com')]) - before
        self.assertEqual(len(digest), 1)
        self.assertIn(self.company.name, digest.subject)
        self.assertIn(signal.name, digest.body_html)

    def test_export_contains_no_personal_data(self):
        self._failed_mail(3)
        self._scan()
        action = self.engine.with_user(self.manager).action_export_feed()
        attachment_id = int(action['url'].split('/web/content/')[1].split('?')[0])
        attachment = self.env['ir.attachment'].browse(attachment_id)
        raw = base64.b64decode(attachment.datas).decode()
        payload = json.loads(raw)
        self.assertEqual(payload['format'], 'eclipse-time.metric-points/1')
        self.assertTrue(any(p['metric'] == 'mail_exceptions_24h' and p['value'] == 3 for p in payload['points']))
        self.assertTrue(all(set(p) == {'metric', 'day', 'value', 'partial'} for p in payload['points']))
        self.assertNotIn('@', raw)
        self.assertFalse(attachment.res_model)
        # another internal user cannot read the generated file
        with self.assertRaises(AccessError):
            attachment.with_user(self.user).read(['datas'])

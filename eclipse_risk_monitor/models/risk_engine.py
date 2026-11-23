import json
import logging
import re
from collections import Counter
from datetime import datetime, time, timedelta

from odoo import api, fields, models, release
from odoo.exceptions import AccessError

from .scoring import compute_score, is_accelerating, priority_from_score, compound_bonus, clamp_weight

_logger = logging.getLogger(__name__)

# Checks whose problems are directly and causally related (compound risk).
# Only direct links: the email queue is itself a scheduled action; e-invoices
# are sent by scheduled actions; invoices that never reach the customer are
# paid late; quotations/confirmations not delivered and failing payments cut
# confirmed orders; negative stock blocks deliveries.  A bonus is only given
# when the related problem belongs to another area.
_LINKS = {
    'TECH_CRON': {'TECH_MAIL', 'FIN_EINVOICE'},
    'TECH_MAIL': {'FIN_AR', 'CUS_SALES'},
    'FIN_EINVOICE': {'FIN_AR'},
    'FIN_PAYMENT': {'CUS_SALES'},
    'OPS_NEGSTOCK': {'OPS_LATE'},
}
LINKS = {}
for _code, _others in _LINKS.items():
    for _other in _others:
        LINKS.setdefault(_code, set()).add(_other)
        LINKS.setdefault(_other, set()).add(_code)

CRON_UNITS = {'minutes': 60, 'hours': 3600, 'days': 86400, 'weeks': 604800, 'months': 2592000}
SALES_MIN_BASELINE = 5.0
_EMAIL_RE = re.compile(r'[\w.+-]+@[\w-]+(\.[\w-]+)+')
_DIGITS_RE = re.compile(r'\+?\d[\d\s().-]{7,}\d')


def sanitize(text, limit=160):
    """Remove email addresses and phone-like numbers from technical messages."""
    text = re.sub(r'<[^>]+>', ' ', str(text or ''))
    text = _EMAIL_RE.sub('[email]', text)
    text = _DIGITS_RE.sub('[number]', text)
    text = re.sub(r'\s+', ' ', text).strip()
    return text[:limit]


class EclipseRiskEngine(models.AbstractModel):
    _name = 'eclipse.risk.engine'
    _description = 'Operational Risk Monitor Engine'

    # ----------------------------------------------------------------- access
    @api.model
    def check_access_manager(self):
        if not self.env.su and not self.env.user.has_group('eclipse_risk_monitor.group_risk_manager'):
            raise AccessError(self.env._('Only Risk Monitor managers can do this.'))

    @api.model
    def _main_company(self):
        company = self.env.ref('base.main_company', raise_if_not_found=False)
        return company or self.env['res.company'].sudo().search([], order='id', limit=1)

    # ------------------------------------------------------------------ scans
    @api.model
    def _cron_scan(self):
        for company in self.env['res.company'].sudo().search([]):
            try:
                with self.env.cr.savepoint():
                    self.sudo().with_company(company)._scan_company(company)
            except Exception:
                _logger.exception('Risk Monitor: scan failed for company %s', company.id)

    @api.model
    def _scan_company(self, company, now=None):
        now = now or fields.Datetime.now()
        checks = self.env['eclipse.risk.check'].sudo().search([('active', '=', True)])
        detections, ran, metrics = [], self.env['eclipse.risk.check'], {}
        for check in checks:
            method = getattr(self, '_detect_%s' % check.code.lower(), None)
            if not method or not check.available:
                continue
            try:
                with self.env.cr.savepoint():
                    found, values = method(company, check, now)
                detections += found
                for day, day_values in values.items():
                    metrics.setdefault(day, {}).update(day_values)
                ran |= check
                check.write({'last_run': now, 'last_error': False})
            except Exception as exc:  # one broken check must not stop the others
                _logger.exception('Risk Monitor: check %s failed', check.code)
                check.write({'last_run': now, 'last_error': sanitize(exc, 250)})
        today = now.date()
        Metric = self.env['eclipse.risk.metric'].sudo()
        for day, values in metrics.items():
            Metric._store(company, day, values, partial=(day >= today))
        signals = self._upsert_signals(company, detections, ran, now)
        self._notify(signals)
        return signals

    def _upsert_signals(self, company, detections, ran_checks, now):
        Signal = self.env['eclipse.risk.signal'].sudo().with_context(mail_create_nosubscribe=True)
        existing = Signal.search([('company_id', '=', company.id)])
        by_fp = {signal.fingerprint: signal for signal in existing}
        window = now - timedelta(hours=48)
        recent = existing.filtered(
            lambda s: s.state in ('open', 'acknowledged') and s.last_seen and s.last_seen >= window)
        area_of = {s.check_id.code: s.area for s in recent}
        for det in detections:
            area_of[det['check'].code] = det['check'].area

        touched = Signal
        for det in detections:
            check = det['check']
            code = check.code
            fingerprint = '%s:%s' % (code, det['key'])
            related_codes = LINKS.get(code, set()) & set(area_of)
            linked_areas = len({area_of[c] for c in related_codes} - {check.area})
            accelerating = bool(det.get('occ_24h') is not None
                                and is_accelerating(det.get('occ_24h'), det.get('occ_prev_24h')))
            severity = det['severity']
            occurrences = det.get('occurrences', 1)
            score = compute_score(severity, occurrences, check.confidence, check.weight,
                                  linked_areas, accelerating)
            explanation = self.env._(
                "Severity %(sev)s/4: %(sev_pts)s pts · occurrences %(occ)s: %(occ_pts)s pts · "
                "rule certainty %(conf).2f: %(conf_pts)s pts · learned weight %(weight)+.2f: %(w_pts)s pts · "
                "related areas %(areas)s: +%(cb)s · accelerating: +%(tb)s · total %(score)s/100",
                sev=severity, sev_pts=round(55.0 * severity / 4), occ=occurrences,
                occ_pts=round(25.0 * min(occurrences, 20) / 20), conf=check.confidence,
                conf_pts=round(20.0 * check.confidence), weight=clamp_weight(check.weight),
                w_pts=round(clamp_weight(check.weight) * 100), areas=linked_areas,
                cb=compound_bonus(linked_areas), tb=15 if accelerating else 0, score=score)
            vals = {
                'name': det['name'],
                'area': check.area,
                'severity': severity,
                'confidence': check.confidence,
                'occurrences': occurrences,
                'occurrences_24h': det.get('occ_24h') or 0,
                'occurrences_prev_24h': det.get('occ_prev_24h') or 0,
                'accelerating': accelerating,
                'compound': linked_areas > 0,
                'score': score,
                'priority': priority_from_score(score, severity),
                'problem': det['problem'],
                'cause': det.get('cause'),
                'recommendation': det.get('recommendation'),
                'score_explanation': explanation,
                'res_model': det.get('res_model') or False,
                'res_id': det.get('res_id') or False,
                'last_seen': now,
            }
            signal = by_fp.get(fingerprint)
            if signal:
                if signal.state == 'resolved':
                    vals.update(state='open', resolved_on=False, verified=False, notified=False,
                                reopen_count=signal.reopen_count + 1)
                elif signal.priority in ('p2', 'p3') and vals['priority'] in ('p0', 'p1'):
                    vals['notified'] = False  # escalated: tell the responsible again
                signal.write(vals)
            else:
                vals.update(company_id=company.id, check_id=check.id, fingerprint=fingerprint,
                            first_seen=now, state='open',
                            user_id=company.eclipse_risk_user_id.id or False)
                signal = Signal.create(vals)
                by_fp[fingerprint] = signal
            touched |= signal

        # related signals (both directions are recomputed at every scan)
        pool = touched | recent
        for signal in touched:
            codes = LINKS.get(signal.check_id.code, set())
            related = pool.filtered(lambda s, c=codes, me=signal: s != me and s.check_id.code in c
                                    and s.state in ('open', 'acknowledged'))
            signal.related_signal_ids = [(6, 0, related.ids)]

        # verification: problems handled since the last scan are no longer detected
        gone = existing.filtered(
            lambda s: s.check_id in ran_checks and s not in touched and s.state in ('open', 'acknowledged', 'dismissed'))
        for signal in gone:
            verified = signal.state != 'dismissed'
            signal.write({'state': 'resolved', 'resolved_on': now, 'verified': verified,
                          'related_signal_ids': [(5, 0, 0)]})
            if verified:
                signal.message_post(body=self.env._(
                    'No longer detected by the check at %(date)s UTC: resolution verified.',
                    date=fields.Datetime.to_string(now)))
            signal.activity_feedback(['mail.mail_activity_data_todo'],
                                     feedback=self.env._('Resolved: no longer detected.'))
        return touched

    def _notify(self, signals):
        group = 'eclipse_risk_monitor.group_risk_user'
        for signal in signals.filtered(lambda s: s.state == 'open' and s.priority in ('p0', 'p1') and not s.notified):
            user = signal.user_id or signal.company_id.eclipse_risk_user_id
            if not user or not user.active or not user.has_group(group):
                continue
            signal.activity_schedule(
                'mail.mail_activity_data_todo', user_id=user.id,
                summary='%s %s' % (dict(signal._fields['priority'].selection)[signal.priority].split(' ')[0],
                                   signal.name[:90]),
                note=signal.recommendation or '')
            signal.notified = True

    # ----------------------------------------------------------------- digest
    @api.model
    def _cron_digest(self):
        group = self.env.ref('eclipse_risk_monitor.group_risk_manager')
        for company in self.env['res.company'].sudo().search([('eclipse_risk_digest', '=', True)]):
            signals = self.env['eclipse.risk.signal'].sudo().search([
                ('company_id', '=', company.id), ('state', 'in', ('open', 'acknowledged')),
                ('priority', 'in', ('p0', 'p1', 'p2'))])
            if not signals:
                continue
            users = group.user_ids.filtered(lambda u: u.active and company in u.company_ids and u.email)
            if not users:
                continue
            counts = Counter(signals.mapped('priority'))
            body = self.env['ir.qweb']._render('eclipse_risk_monitor.digest_email', {
                'company': company, 'signals': signals, 'counts': counts,
                'priorities': dict(signals._fields['priority'].selection),
            })
            self.env['mail.mail'].sudo().create({
                'subject': self.env._('[Risk Monitor] %(company)s: %(n)s open signal(s), %(p0)s P0 / %(p1)s P1',
                                      company=company.name, n=len(signals), p0=counts.get('p0', 0),
                                      p1=counts.get('p1', 0)),
                'body_html': body,
                'email_to': ','.join(users.mapped('email_formatted')),
                'email_from': company.email_formatted or self.env.user.email_formatted,
                'auto_delete': True,
            })

    # ----------------------------------------------------------------- export
    @api.model
    def action_export_feed(self):
        """JSON with daily metrics (Eclipse Time metric-points format) and
        anonymous signal summaries of the current company, generated only on
        demand. Contains no names, emails or partner data."""
        self.check_access_manager()
        company = self.env.company
        now = fields.Datetime.now()
        since = now.date() - timedelta(days=90)
        metrics = self.env['eclipse.risk.metric'].sudo().search(
            [('company_id', '=', company.id), ('day', '>=', since)], order='day, metric')
        signals = self.env['eclipse.risk.signal'].sudo().search(
            [('company_id', '=', company.id), ('last_seen', '>=', datetime.combine(since, time.min))])
        payload = {
            'format': 'eclipse-time.metric-points/1',
            'generated_at': now.isoformat() + 'Z',
            'source': 'odoo-%s' % release.major_version,
            'currency': company.currency_id.name,
            'points': [{'metric': m.metric, 'day': fields.Date.to_string(m.day),
                        'value': m.value, 'partial': m.partial} for m in metrics],
            'signals': [{'check': s.check_id.code, 'area': s.area, 'priority': s.priority.upper(),
                         'score': s.score, 'state': s.state, 'occurrences': s.occurrences,
                         'compound': s.compound, 'accelerating': s.accelerating,
                         'first_seen': fields.Datetime.to_string(s.first_seen),
                         'last_seen': fields.Datetime.to_string(s.last_seen),
                         'verified': s.verified} for s in signals],
        }
        attachment = self.env['ir.attachment'].create({
            'name': 'risk-monitor-feed-%s.json' % fields.Date.to_string(now.date()),
            'raw': json.dumps(payload, indent=1).encode(),
            'mimetype': 'application/json',
        })
        return {'type': 'ir.actions.act_url', 'url': '/web/content/%s?download=true' % attachment.id,
                'target': 'self'}

    # ------------------------------------------------------------- detectors
    # Each detector returns (detections, {day: {metric: value}}).

    def _detect_tech_cron(self, company, check, now):
        if company != self._main_company():
            return [], {}  # scheduled actions are database-wide: reported once, in the main company
        Cron = self.env['ir.cron'].sudo()
        crons = Cron.search([('active', '=', True)])
        found, overdue = [], []
        has_failures = 'failure_count' in Cron._fields
        for cron in crons:
            interval = (cron.interval_number or 1) * CRON_UNITS.get(cron.interval_type, 86400)
            if cron.nextcall and (now - cron.nextcall).total_seconds() > max(interval, 3600):
                overdue.append(cron)
            failures = cron.failure_count if has_failures else 0
            if failures:
                found.append({
                    'check': check, 'key': 'fail:%s' % cron.id,
                    'name': self.env._('Scheduled action failing: %s', cron.name),
                    'severity': 3 if failures >= 3 else 2,
                    'occurrences': failures,
                    'problem': self.env._(
                        "The scheduled action '%(name)s' failed %(n)s consecutive time(s) since %(since)s UTC.",
                        name=cron.name, n=failures,
                        since=fields.Datetime.to_string(cron.first_failure_date) if cron.first_failure_date else '?'),
                    'cause': self.env._(
                        'The job raises an error every time it runs. Odoo deactivates a scheduled action after '
                        '5 consecutive failures spread over at least 7 days; from then on the process it '
                        'automates stops without further warning.'),
                    'recommendation': self.env._(
                        'Open the scheduled action, use "Run Manually" to see the error, check the server log '
                        'around the failure time and fix the cause before it gets deactivated.'),
                    'res_model': 'ir.cron', 'res_id': cron.id,
                })
        if len(overdue) >= 3:
            worst = max(overdue, key=lambda c: now - c.nextcall)
            found.append({
                'check': check, 'key': 'stalled', 'severity': 4, 'occurrences': len(overdue),
                'name': self.env._('%s scheduled actions are not being executed', len(overdue)),
                'problem': self.env._(
                    '%(n)s active scheduled actions are overdue; the most delayed (%(name)s) is %(hours)s h late.',
                    n=len(overdue), name=worst.name,
                    hours=int((now - worst.nextcall).total_seconds() // 3600)),
                'cause': self.env._(
                    'Scheduled actions are not being processed: the cron worker may be stopped or saturated '
                    '(e.g. max_cron_threads = 0, a long-running job blocking the queue, or the server was down). '
                    'Emails, e-invoices and other automated processes wait until it runs again.'),
                'recommendation': self.env._(
                    'Check that the Odoo server runs with cron threads enabled and look in the server log for '
                    'long-running or blocked jobs. Then confirm that this signal disappears at the next scan.'),
            })
        else:
            for cron in overdue:
                found.append({
                    'check': check, 'key': 'late:%s' % cron.id, 'severity': 2, 'occurrences': 1,
                    'name': self.env._('Scheduled action overdue: %s', cron.name),
                    'problem': self.env._("The scheduled action '%(name)s' should have run %(hours)s h ago.",
                                          name=cron.name,
                                          hours=int((now - cron.nextcall).total_seconds() // 3600)),
                    'cause': self.env._('The job was skipped or is blocked by another job.'),
                    'recommendation': self.env._('Run it manually once and check the server log.'),
                    'res_model': 'ir.cron', 'res_id': cron.id,
                })
        failing = sum(1 for c in crons if has_failures and c.failure_count)
        return found, {now.date(): {'cron_failing': failing, 'cron_overdue': len(overdue)}}

    def _mail_company_domain(self, company):
        if 'record_company_id' not in self.env['mail.message']._fields:
            return [] if company == self._main_company() else [('id', '=', 0)]
        if company == self._main_company():
            return ['|', ('mail_message_id.record_company_id', '=', company.id),
                    ('mail_message_id.record_company_id', '=', False)]
        return [('mail_message_id.record_company_id', '=', company.id)]

    def _detect_tech_mail(self, company, check, now):
        Mail = self.env['mail.mail'].sudo()
        base = [('state', '=', 'exception')] + self._mail_company_domain(company)
        last = Mail.search(base + [('create_date', '>=', now - timedelta(hours=24))])
        prev = Mail.search_count(base + [('create_date', '>=', now - timedelta(hours=48)),
                                         ('create_date', '<', now - timedelta(hours=24))])
        metrics = {now.date(): {'mail_exceptions_24h': len(last)}}
        if not last:
            return [], metrics
        invoices = len(last.filtered(lambda m: m.model == 'account.move'))
        reasons = Counter(sanitize(m.failure_reason or m.failure_type or '', 120) or '?' for m in last)
        top_reason, top_count = reasons.most_common(1)[0]
        return [{
            'check': check, 'key': 'exceptions',
            'severity': 3 if (len(last) >= max(int(check.threshold or 10), 1) or invoices) else 2,
            'occurrences': len(last), 'occ_24h': len(last), 'occ_prev_24h': prev,
            'name': self.env._('%s email(s) could not be sent in the last 24 h', len(last)),
            'problem': self.env._('%(n)s outgoing email(s) failed in the last 24 h, %(inv)s of them about invoices.',
                                  n=len(last), inv=invoices),
            'cause': self.env._('Most frequent error (%(count)s): %(reason)s', count=top_count, reason=top_reason),
            'recommendation': self.env._(
                'Check the outgoing mail server and the recipient addresses, then retry the failed emails '
                '(Settings > Technical > Emails, filter "Failed"). Invoices that were not received are '
                'often paid late.'),
        }], metrics

    def _detect_fin_einvoice(self, company, check, now):
        found, total = [], 0
        if 'l10n_es_edi_verifactu.document' in self.env:
            Doc = self.env['l10n_es_edi_verifactu.document'].sudo()
            recent = Doc.search([('company_id', '=', company.id), ('create_date', '>=', now - timedelta(days=7)),
                                 ('state', 'in', ('rejected', 'registered_with_errors'))])
            latest = {}
            if recent:
                for doc in Doc.search([('company_id', '=', company.id), ('move_id', 'in', recent.move_id.ids)],
                                      order='create_date desc, id desc'):
                    latest.setdefault(doc.move_id.id, doc)
            for state, severity in (('rejected', 4), ('registered_with_errors', 3)):
                docs = [d for d in latest.values() if d.state == state]
                if not docs:
                    continue
                total += len(docs)
                found.append({
                    'check': check, 'key': 'verifactu:%s' % state, 'severity': severity, 'occurrences': len(docs),
                    'name': self.env._('Veri*Factu: %(n)s invoice record(s) %(state)s by the AEAT',
                                       n=len(docs), state=dict(Doc._fields['state'].selection)[state].lower()),
                    'problem': self.env._('Latest Veri*Factu record of %(n)s invoice(s): %(refs)s.',
                                          n=len(docs), refs=', '.join(d.move_id.name or '?' for d in docs[:5])),
                    'cause': sanitize(docs[0].errors, 200) or self.env._('See the error on the Veri*Factu document.'),
                    'recommendation': self.env._(
                        'Open each invoice, read the AEAT error on its Veri*Factu document, correct the data '
                        'and send it again. Rejected records are not registered with the tax agency.'),
                    'res_model': 'account.move' if len(docs) == 1 else False,
                    'res_id': docs[0].move_id.id if len(docs) == 1 else False,
                })
        if 'account.edi.document' in self.env:
            Edi = self.env['account.edi.document'].sudo()
            errors = Edi.search([('move_id.company_id', '=', company.id), ('blocking_level', '=', 'error'),
                                 ('state', 'in', ('to_send', 'to_cancel'))])
            stuck = Edi.search([('move_id.company_id', '=', company.id), ('state', 'in', ('to_send', 'to_cancel')),
                                ('blocking_level', '!=', 'error'), ('write_date', '<', now - timedelta(hours=24))])
            for key, docs, severity, label in (
                    ('edi:error', errors, 4, self.env._('blocked by an error')),
                    ('edi:stuck', stuck, 3, self.env._('waiting for more than 24 h'))):
                if not docs:
                    continue
                total += len(docs)
                found.append({
                    'check': check, 'key': key, 'severity': severity, 'occurrences': len(docs),
                    'name': self.env._('%(n)s electronic invoice(s) %(label)s', n=len(docs), label=label),
                    'problem': self.env._('E-invoicing documents (%(fmt)s) for: %(refs)s.',
                                          fmt=', '.join(sorted(set(docs.mapped('edi_format_id.name')))),
                                          refs=', '.join(docs[:5].mapped('move_id.name'))),
                    'cause': sanitize(docs[0].error, 200) or self.env._(
                        'The documents are queued but the sending job did not complete.'),
                    'recommendation': self.env._(
                        'Open the invoices, read the electronic invoicing error and process them again; if they '
                        'are only waiting, check the scheduled action that sends them.'),
                    'res_model': 'account.move' if len(docs) == 1 else False,
                    'res_id': docs.move_id.id if len(docs) == 1 else False,
                })
        return found, {now.date(): {'einvoice_errors': total}}

    def _detect_fin_ar(self, company, check, now):
        grace = check.grace_days
        today = now.date()
        domain = [('company_id', '=', company.id), ('move_type', '=', 'out_invoice'), ('state', '=', 'posted'),
                  ('payment_state', 'in', ('not_paid', 'partial')),
                  ('invoice_date_due', '<', today - timedelta(days=grace))]
        [(amount, count)] = self.env['account.move'].sudo()._read_group(
            domain, [], ['amount_residual_signed:sum', '__count'])
        amount = amount or 0.0
        metrics = {today: {'ar_overdue_amount': amount, 'ar_overdue_count': count}}
        minimum = check.threshold or 0.0
        if not count or amount < minimum:
            return [], metrics
        previous = self.env['eclipse.risk.metric'].sudo()._value(company, today - timedelta(days=7), 'ar_overdue_amount')
        growing = bool(previous and amount >= previous * 1.2)
        currency = company.currency_id.name
        return [{
            'check': check, 'key': 'overdue', 'severity': 3 if growing else 2, 'occurrences': count,
            'name': self.env._('%(n)s customer invoice(s) overdue by more than %(d)s days', n=count, d=grace),
            'problem': self.env._('%(n)s posted customer invoice(s) are more than %(d)s days past due, '
                                  'for %(amount).2f %(cur)s outstanding.', n=count, d=grace, amount=amount, cur=currency),
            'cause': self.env._('Overdue receivables grew %(pct)s %% in 7 days (from %(old).2f %(cur)s).',
                                pct=round((amount / previous - 1) * 100), old=previous, cur=currency)
            if growing else self.env._(
                'Invoices remain unpaid after their due date. Check whether reminders are sent and whether '
                'invoice emails are delivered.'),
            'recommendation': self.env._(
                'Review the aged receivable report, contact the largest debtors first and make sure invoice '
                'emails and reminders reach the customers.'),
        }], metrics

    def _detect_ops_late(self, company, check, now):
        grace = check.grace_days
        Picking = self.env['stock.picking'].sudo()
        domain = [('company_id', '=', company.id), ('picking_type_code', '=', 'outgoing'),
                  ('state', 'in', ('confirmed', 'waiting', 'assigned')),
                  ('scheduled_date', '<', now - timedelta(days=grace))]
        count = Picking.search_count(domain)
        metrics = {now.date(): {'late_deliveries': count}}
        if count < max(int(check.threshold or 1), 1):
            return [], metrics
        sample = Picking.search(domain, order='scheduled_date asc', limit=5)
        oldest_days = (now - sample[0].scheduled_date).days
        return [{
            'check': check, 'key': 'late', 'severity': 3 if count >= 10 else 2, 'occurrences': count,
            'name': self.env._('%s delivery order(s) late', count),
            'problem': self.env._('%(n)s outgoing deliveries are past their scheduled date by more than %(g)s day(s) '
                                  'and not done (oldest: %(days)s days). Examples: %(refs)s.',
                                  n=count, g=grace, days=oldest_days, refs=', '.join(sample.mapped('name'))),
            'cause': self.env._('Typical causes: products not available or not reserved, a blocked picking step, '
                                'or deliveries that were shipped but not validated in Odoo.'),
            'recommendation': self.env._('Open Inventory > Deliveries filtered on "Late", check availability, '
                                         'validate what was shipped and inform the affected customers.'),
            'res_model': 'stock.picking' if count == 1 else False,
            'res_id': sample.id if count == 1 else False,
        }], metrics

    def _detect_ops_negstock(self, company, check, now):
        Quant = self.env['stock.quant'].sudo()
        domain = [('company_id', '=', company.id), ('location_id.usage', '=', 'internal'), ('quantity', '<', 0)]
        count = Quant.search_count(domain)
        metrics = {now.date(): {'negative_quants': count}}
        if not count:
            return [], metrics
        sample = Quant.search(domain, order='quantity asc', limit=5)
        return [{
            'check': check, 'key': 'negative', 'severity': 2, 'occurrences': count,
            'name': self.env._('%s stock line(s) with negative quantity', count),
            'problem': self.env._('%(n)s product/location line(s) have a negative on-hand quantity. '
                                  'Examples: %(items)s.', n=count,
                                  items=', '.join('%s (%s)' % (q.product_id.display_name, q.quantity) for q in sample)),
            'cause': self.env._('Goods were delivered or consumed before their receipt was recorded, or moves were '
                                'validated from the wrong location. Availability and valuation become unreliable.'),
            'recommendation': self.env._('Review these products, correct them with an inventory adjustment and '
                                         'fix the receipt or delivery flow that caused it.'),
        }], metrics

    def _detect_cus_sales(self, company, check, now):
        Order = self.env['sale.order'].sudo()
        yesterday = now.date() - timedelta(days=1)

        def confirmed_on(day):
            start = datetime.combine(day, time.min)
            return Order.search_count([('company_id', '=', company.id), ('state', '=', 'sale'),
                                       ('date_order', '>=', start), ('date_order', '<', start + timedelta(days=1))])

        value = confirmed_on(yesterday)
        metrics = {yesterday: {'sales_orders_confirmed': value}}
        baseline = sum(confirmed_on(yesterday - timedelta(weeks=k)) for k in range(1, 5)) / 4.0
        drop_pct = check.threshold or 40.0
        if baseline < SALES_MIN_BASELINE or value > baseline * (1 - drop_pct / 100.0):
            return [], metrics
        pct = round((1 - value / baseline) * 100)
        return [{
            'check': check, 'key': 'drop', 'severity': 3, 'occurrences': 1,
            'name': self.env._('Confirmed sales orders down %s %%', pct),
            'problem': self.env._('%(v)s sales orders confirmed on %(day)s against an average of %(avg).1f on the same '
                                  'weekday of the previous 4 weeks (-%(pct)s %%).',
                                  v=value, day=fields.Date.to_string(yesterday), avg=baseline, pct=pct),
            'cause': self.env._('Either demand dropped or orders are not reaching Odoo (website, payment provider, '
                                'integrations or emails failing). Look at the related signals before deciding.'),
            'recommendation': self.env._('Check the order sources first (website, payment, connectors); only if they '
                                         'work, treat it as a commercial drop and alert sales.'),
        }], metrics

    def _detect_fin_payment(self, company, check, now):
        Tx = self.env['payment.transaction'].sudo()
        base = [('company_id', '=', company.id), ('state', '=', 'error')]
        last = Tx.search(base + [('last_state_change', '>=', now - timedelta(hours=24))])
        prev = Tx.search_count(base + [('last_state_change', '>=', now - timedelta(hours=48)),
                                       ('last_state_change', '<', now - timedelta(hours=24))])
        metrics = {now.date(): {'payment_errors_24h': len(last)}}
        if not last:
            return [], metrics
        providers = ', '.join(sorted(set(last.mapped('provider_id.name'))))
        reason = sanitize(last[0].state_message, 160)
        return [{
            'check': check, 'key': 'errors', 'severity': 4 if len(last) >= 10 else 3,
            'occurrences': len(last), 'occ_24h': len(last), 'occ_prev_24h': prev,
            'name': self.env._('%s online payment(s) failed in the last 24 h', len(last)),
            'problem': self.env._('%(n)s payment transaction(s) ended in error in the last 24 h (provider: %(p)s).',
                                  n=len(last), p=providers or '?'),
            'cause': reason or self.env._('The payment provider refused or failed the transactions: credentials, '
                                          'expired keys, 3-D Secure or a provider incident.'),
            'recommendation': self.env._('Check the payment provider configuration and the transaction messages; '
                                         'customers who could not pay may need to be contacted.'),
        }], metrics

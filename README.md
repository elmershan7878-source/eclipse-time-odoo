# Operational Risk Monitor (Odoo module `eclipse_risk_monitor`)

Author: Eclipse Time · Licence: LGPL-3 · Price on Odoo Apps: free · Version 1.0.1 · Languages: English, Spanish

Odoo Apps: https://apps.odoo.com/apps/modules/18.0/eclipse_risk_monitor (also 17.0 and 19.0)

Detects operational problems inside an Odoo database every hour and turns them into
prioritised signals (P0-P3) with probable cause, recommended next step, related signals,
an activity for the responsible user, a daily digest and automatic verification once the
problem disappears. No external service, no automatic data transmission.

| Branch / folder | Odoo series | Tests |
|---|---|---|
| `17.0/eclipse_risk_monitor` | 17.0 | 18 tests, 0 failures (failing-cron test skipped: Odoo 17 does not count cron failures) |
| `18.0/eclipse_risk_monitor` | 18.0 | 18 tests, 0 failures (with Sales, Inventory, Invoicing, Payment; also with Spain Veri*Factu + SII; also minimal install) |
| `19.0/eclipse_risk_monitor` | 19.0 | 18 tests, 0 failures |

`18.0` is the source of truth. `17.0` and `19.0` are generated from it with the port script
kept in the Eclipse Time master folder (28 - Alianza Odoo/modulo-odoo/herramientas/port.py).

## Install
1. Copy the `eclipse_risk_monitor` folder of your Odoo series into the addons path (or use "Deploy on Odoo.sh" from Odoo Apps).
2. Update the Apps list and install *Operational Risk Monitor*. The administrator gets the Manager role.
3. Settings > Risk Monitor: choose the responsible user and press *Scan now*. Checks then run every hour.

## Translations
`i18n/es.po` (Spanish, 250 terms). Signals and the daily digest are written in the language of each company.

## Changelog
- 1.0.1 (2026-11-24): Spanish translation; scheduled scans and digests use the company language; installation section in the description.
- 1.0.0 (2026-11-23): first release.

## Checks
TECH_CRON, TECH_MAIL, FIN_EINVOICE, FIN_AR, FIN_PAYMENT, OPS_LATE, OPS_NEGSTOCK, CUS_SALES.
See `static/description/index.html` for what each one reports and its requirements.

## Run the tests (Docker)
```
docker run -d --name db -e POSTGRES_USER=odoo -e POSTGRES_PASSWORD=change-me -e POSTGRES_DB=postgres postgres:16
docker run --rm --link db:db -e HOST=db -e USER=odoo -e PASSWORD=change-me \
  -v "$PWD/18.0:/mnt/extra-addons" odoo:18.0 odoo -d t18 \
  -i eclipse_risk_monitor,sale_management,stock,account,payment \
  --test-enable --test-tags /eclipse_risk_monitor --stop-after-init
```

## Publishing on apps.odoo.com
One Git repository, one branch per series (`17.0`, `18.0`, `19.0`), module folder at the
root of each branch. Register the repository at https://apps.odoo.com/apps/upload as
`ssh://git@github.com/<owner>/<repo>#18.0` (and the other branches). For a private
GitHub repository, give read access to the `online-odoo` user.

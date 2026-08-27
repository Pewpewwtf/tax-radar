# Tax Radar 1.9 — legal setup before public launch

Already configured:
- Operator: Колосов Роман Михайлович
- NPD / self-employed individual
- INN: 772072450119
- Contact: inbox@sdelatvychet.ru
- /terms
- /privacy
- /consent
- separate Terms and personal-data checkboxes
- server-side version validation
- consent audit event

## Operator address

Configured in the application:
Зелёный проспект, д. 32

You may override it in Timeweb with:
OPERATOR_ADDRESS=<new address>

## IMPORTANT: persistent consent evidence

The app writes consent events to:

CONSENT_AUDIT_PATH=data/consent_audit.jsonl

Container-local files can disappear on redeploy/recreation. Before public
traffic, either:
1. mount persistent storage and set CONSENT_AUDIT_PATH to that mount; or
2. migrate audit events to PostgreSQL.

Recommended additional environment variable:

CONSENT_AUDIT_SECRET=<long random secret>

It is used to pseudonymize IP and user-agent before writing the audit record.
Do not commit this secret to GitHub.

## Also before public traffic

- File the operator notification with Roskomnadzor if required for the chosen
  processing model.
- Review the final policy/consent with a Russian privacy lawyer, especially the
  legal classification of merchant data that may indirectly reveal medical
  services.
- Do not add foreign analytics, crash reporting, or CDN services that receive
  bank-statement data without reassessing localization/transborder processing.

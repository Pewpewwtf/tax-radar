# Tax Radar UNIVERSAL 1.7

## PDF pipeline

### Verified on real user files
- Альфа-Банк
- Т-Банк
- Сбер

### Bank-specific beta parsers built from public statement-format evidence
- ВТБ
- Райффайзенбанк

### Automatic bank recognition + strict universal deduction parser
Major Russian retail banks including:
Газпромбанк, Россельхозбанк, МКБ, Банк ДОМ.РФ, Совкомбанк, Банк Санкт-Петербург,
Ак Барс, ОТП, Озон Банк, Уралсиб, МТС Банк, ЮниКредит, Яндекс Банк, УБРиР,
Русский Стандарт, Кредит Европа, Локо-Банк and others.

The universal parser is intentionally conservative. It extracts only transaction
blocks with tax-deduction signals when the amount association is structurally
credible. It does not invent transactions from ambiguous PDF layouts.

CSV/XLSX remains the safest fallback for any bank.

Paid funnel remains:
statement → two aggregate amounts → 499 ₽ report → details after payment.

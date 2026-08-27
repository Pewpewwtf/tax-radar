from __future__ import annotations

import csv
import hashlib
import io
import re
import os
import time
import uuid
import json
import base64
import urllib.request
import urllib.error
from collections import defaultdict
from datetime import datetime
from html import escape
from typing import Any

from fastapi import FastAPI, File, UploadFile, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, Response, RedirectResponse
from pydantic import BaseModel

app = FastAPI(title="Tax Radar", version="1.7.0")


REPORT_PRICE_RUB = 499
ANALYSIS_TTL_SECONDS = 2 * 60 * 60
ANALYSES: dict[str, dict[str, Any]] = {}

YOOKASSA_SHOP_ID = os.getenv("YOOKASSA_SHOP_ID", "").strip()
YOOKASSA_SECRET_KEY = os.getenv("YOOKASSA_SECRET_KEY", "").strip()
PUBLIC_BASE_URL = os.getenv("PUBLIC_BASE_URL", "").strip().rstrip("/")
PAYMENT_TEST_MODE = os.getenv("PAYMENT_TEST_MODE", "").strip().lower() in {"1", "true", "yes", "on"}


def cleanup_analyses() -> None:
    now = time.time()
    expired = [k for k, v in ANALYSES.items() if now - v.get("created_at", now) > ANALYSIS_TTL_SECONDS]
    for k in expired:
        ANALYSES.pop(k, None)


def get_analysis_or_404(analysis_id: str) -> dict[str, Any]:
    cleanup_analyses()
    item = ANALYSES.get(analysis_id)
    if not item:
        raise HTTPException(404, "Результат анализа не найден или срок хранения истёк. Загрузите выписку ещё раз.")
    return item


def yookassa_request(method: str, path: str, payload: dict[str, Any] | None = None, idempotence_key: str | None = None) -> dict[str, Any]:
    if not (YOOKASSA_SHOP_ID and YOOKASSA_SECRET_KEY):
        raise HTTPException(503, "Оплата пока не подключена: добавьте YOOKASSA_SHOP_ID и YOOKASSA_SECRET_KEY в Timeweb.")
    url = "https://api.yookassa.ru/v3" + path
    token = base64.b64encode(f"{YOOKASSA_SHOP_ID}:{YOOKASSA_SECRET_KEY}".encode("utf-8")).decode("ascii")
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8") if payload is not None else None
    headers = {
        "Authorization": f"Basic {token}",
        "Content-Type": "application/json",
    }
    if idempotence_key:
        headers["Idempotence-Key"] = idempotence_key
    req = urllib.request.Request(url, data=body, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=20) as r:
            return json.loads(r.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        detail = e.read().decode("utf-8", errors="replace")
        raise HTTPException(502, f"ЮKassa вернула ошибку: {detail[:500]}")
    except Exception as e:
        raise HTTPException(502, f"Не удалось связаться с ЮKassa: {e}")


def public_base_url(request: Request) -> str:
    if PUBLIC_BASE_URL:
        return PUBLIC_BASE_URL
    return str(request.base_url).rstrip("/")


# Recognition signatures for major Russian retail banks.
# Detection != guaranteed bank-specific parsing: unsupported layouts fall through
# to the strict universal deduction-oriented parser.
BANK_SIGNATURES = [
    ("sber", "Сбер", ["ПАО СБЕРБАНК", "СБЕРБАНК", "SBERBANK.RU", "СБЕР БАНК"]),
    ("vtb", "ВТБ", ["БАНК ВТБ", "ВТБ (ПАО)", "VTB.RU", "ПАО ВТБ"]),
    ("gazprombank", "Газпромбанк", ["ГАЗПРОМБАНК", "БАНК ГПБ", "GAZPROMBANK"]),
    ("alfa", "Альфа-Банк", ["АЛЬФА-БАНК", "АЛЬФА БАНК", "ALFABANK", "ALFA-BANK"]),
    ("tbank", "Т-Банк", ["АО «ТБАНК»", 'АО "ТБАНК"', "Т-БАНК", "TBANK.RU", "ТИНЬКОФФ БАНК", "TINKOFF BANK"]),
    ("rshb", "Россельхозбанк", ["РОССЕЛЬХОЗБАНК", "RSHB.RU"]),
    ("mkb", "МКБ", ["МОСКОВСКИЙ КРЕДИТНЫЙ БАНК", "МКБ", "MKB.RU"]),
    ("domrf", "Банк ДОМ.РФ", ["БАНК ДОМ.РФ", "DOM.RF BANK", "ДОМ.РФ БАНК"]),
    ("sovcombank", "Совкомбанк", ["СОВКОМБАНК", "ХАЛВА", "SOVCOMBANK"]),
    ("raiffeisen", "Райффайзенбанк", ["РАЙФФАЙЗЕНБАНК", "RAIFFEISENBANK", "RAIFFEISEN BANK"]),
    ("bmbank", "БМ-Банк", ["БМ-БАНК", "БМ БАНК"]),
    ("novikom", "Новикомбанк", ["НОВИКОМБАНК"]),
    ("bankrossiya", "Банк Россия", ['БАНК "РОССИЯ"', "АБ РОССИЯ"]),
    ("bspb", "Банк Санкт-Петербург", ["БАНК САНКТ-ПЕТЕРБУРГ", "BSPB.RU"]),
    ("akbars", "Ак Барс Банк", ["АК БАРС", "AK BARS"]),
    ("rencap", "РенКап Банк", ["РЕНКАП БАНК", "РЕНЕССАНС КРЕДИТ", "RENCAP"]),
    ("otp", "ОТП Банк", ["ОТП БАНК", "OTP BANK"]),
    ("ozon", "Озон Банк", ["ОЗОН БАНК", "OZON BANK"]),
    ("uralsib", "Уралсиб", ["УРАЛСИБ", "URALSIB"]),
    ("mts", "МТС Банк", ["МТС БАНК", "MTS BANK"]),
    ("unicredit", "ЮниКредит Банк", ["ЮНИКРЕДИТ БАНК", "UNICREDIT BANK"]),
    ("tkb", "ТКБ Банк", ["ТКБ БАНК", "TRANSKAPITALBANK"]),
    ("yandex", "Яндекс Банк", ["ЯНДЕКС БАНК", "YANDEX BANK"]),
    ("atb", "Азиатско-Тихоокеанский Банк", ["АЗИАТСКО-ТИХООКЕАНСКИЙ БАНК", "АТБ БАНК"]),
    ("expobank", "Экспобанк", ["ЭКСПОБАНК", "EXPOBANK"]),
    ("tochka", "Точка", ["БАНК ТОЧКА", "ТОЧКА БАНК"]),
    ("ubrir", "УБРиР", ["УБРИР", "УРАЛЬСКИЙ БАНК РЕКОНСТРУКЦИИ И РАЗВИТИЯ"]),
    ("russianstandard", "Русский Стандарт", ["РУССКИЙ СТАНДАРТ", "RUSSIAN STANDARD"]),
    ("credit_europe", "Кредит Европа Банк", ["КРЕДИТ ЕВРОПА БАНК", "CREDIT EUROPE BANK"]),
    ("loko", "Локо-Банк", ["ЛОКО-БАНК", "LOKO BANK"]),
]


def recognize_bank(text: str) -> tuple[str, str]:
    upper = text.upper()
    for key, display, markers in BANK_SIGNATURES:
        if any(marker.upper() in upper for marker in markers):
            return key, display
    return "unknown", "Неизвестный банк"


CATEGORY_META = {
    "medicine": {"name": "Медицина", "emoji": "🏥", "confidence": "high", "note": "Похоже на оплату медицинских услуг. Для вычета потребуется подтверждение от медицинской организации."},
    "pharmacy": {"name": "Аптеки / лекарства", "emoji": "💊", "confidence": "verify", "note": "Аптечная покупка сама по себе не гарантирует вычет: обычно нужны назначение врача и подтверждающие документы."},
    "fitness": {"name": "Спорт / фитнес", "emoji": "🏋️", "confidence": "verify", "note": "Нужно проверить, дает ли организация право на спортивный вычет за соответствующий год."},
    "education": {"name": "Обучение", "emoji": "🎓", "confidence": "verify", "note": "Нужно подтвердить, что платеж относится к обучению и организация/ИП соответствует условиям вычета."},
    "insurance": {"name": "Страхование", "emoji": "🛡️", "confidence": "verify", "note": "Нужно определить тип договора. ОСАГО/каско не являются обычным социальным вычетом."},
    "donation": {"name": "Благотворительность", "emoji": "❤️", "confidence": "verify", "note": "Нужно проверить получателя и документы. Такие расходы не включаются в базовый расчет автоматически."},
}

RULES = [
    ("medicine", [r"MCC(?:8011|8062|8099|8021|8031|8041|8042|8049)\b", r"MEDSI(?:\b|_)", r"DERAJS", r"DERAYS", r"ДЭРАЙС", r"\bMEDSKAN\w*\b", r"\bKLINIKA\b", r"КЛИНИК", r"MEDICINSK", r"МЕДИЦ", r"STOMAT", r"СТОМАТ", r"\bDENT(?:AL)?\b", r"КОСМЕТОЛ", r"\bBESTCLIN"]),
    ("pharmacy", [r"MCC5912\b", r"\bAPTEKA\b", r"АПТЕК", r"АПТЕЧ", r"\bGORZDRAV\b", r"\bMSKAPT"]),
    ("fitness", [r"MCC7997\b", r"\bSTROYTELO\b", r"\bFITNESS\b", r"\bFITNES\b", r"ФИТНЕС", r"\bEMS\b"]),
    ("education", [r"MCC(?:8220|8241|8244|8249|8351)\b", r"\bSKILLBOX\b", r"\bNETOLOGY\b", r"GEEKBRAINS", r"\bUNIVERS", r"УНИВЕРСИТ", r"\bSCHOOL\b", r"ШКОЛ", r"\bCOURSE\b", r"КУРС"]),
    ("insurance", [r"ИНГОССТРАХ", r"\bINGOS", r"\bINSURANCE\b", r"СТРАХОВ", r"MCC6300\b"]),
    ("donation", [r"БЛАГОТВОР", r"\bCHARITY\b", r"MCC8398\b"]),
]

TX_RE = re.compile(r"(?ms)^(\d{2}\.\d{2}\.\d{4})\s+([A-ZА-Я0-9_]+)\s+(.*?)(?=^\d{2}\.\d{2}\.\d{4}\s+[A-ZА-Я0-9_]+\s+|\Z)")
RUR_AMOUNT_RE = re.compile(r"(?<!\d)([-+]?\d[\d ]*[,.]\d{2})\s+RUR")


def money_to_float(s: str) -> float:
    return float(s.replace("\xa0", "").replace(" ", "").replace(",", "."))


def normalize(s: str) -> str:
    return re.sub(r"\s+", " ", s or "").strip()


def normalize_date(v: Any) -> str:
    if isinstance(v, datetime):
        return v.strftime("%d.%m.%Y")
    s = str(v or "").strip()
    for fmt in ("%d.%m.%Y", "%Y-%m-%d", "%d/%m/%Y", "%d.%m.%y"):
        try:
            return datetime.strptime(s[:10], fmt).strftime("%d.%m.%Y")
        except Exception:
            pass
    return s[:10]


def extract_merchant(desc: str) -> str:
    m = re.search(r"место совершения операции:\s*(.*?)\s+MCC\d{4}\b", desc, re.I)
    if m:
        place = m.group(1).strip()
        parts = [p.strip() for p in place.split("\\") if p.strip()]
        if parts:
            merchant = re.sub(r"^\d{5,}\s*", "", parts[-1]).strip()
            return merchant[:100]
    m = re.search(r"\bв\s+(.+?)\s+через\s+Систему быстрых платежей", desc, re.I)
    if m:
        return m.group(1).strip()[:100]
    if "Оплата страхового продукта" in desc:
        return "Страховой продукт"
    return "Не удалось определить"


TBANK_PAIR_RE = re.compile(
    r"(?m)^(\d{2}\.\d{2}\.\d{4})\n(\d{2}:\d{2})\n"
    r"(\d{2}\.\d{2}\.\d{4})\n(\d{2}:\d{2})\n"
)


def extract_tbank_merchant(description: str) -> str:
    s = normalize(description)

    m = re.match(r"Оплата в\s+(.+)", s, re.I)
    if m:
        merchant = m.group(1).strip()
        location_patterns = [
            r"\s+Gorod\s+Moskva\s+RUS$",
            r"\s+MOSKVA\s+RUS$",
            r"\s+Moskva\s+RUS$",
            r"\s+MOSCOW\s+RUS$",
            r"\s+Moscow\s+RU$",
            r"\s+Moskva\s+RU$",
            r"\s+G\s+MOSKVA\s+RU$",
            r"\s+Москва\s+Россия$",
            r"\s+[A-Za-zА-Яа-яЁё.\-]+\s+(?:RUS|RU|BLR)$",
        ]
        for pattern in location_patterns:
            merchant = re.sub(pattern, "", merchant, flags=re.I).strip()
        return merchant[:100] or "Оплата картой"

    m = re.match(r"Оплата услуг\s+(.+)", s, re.I)
    if m:
        return m.group(1).strip()[:100]

    return s[:100] or "Операция"


def parse_tbank_pdf_text(text: str) -> list[dict[str, Any]]:
    pairs = list(TBANK_PAIR_RE.finditer(text))
    if not pairs:
        raise ValueError("Не удалось найти операции в выписке Т-Банка.")

    txs: list[dict[str, Any]] = []

    for i, pair in enumerate(pairs):
        end = pairs[i + 1].start() if i + 1 < len(pairs) else len(text)
        block = text[pair.end():end]

        cut_positions = []
        for marker in (
            'АО «ТБанк»',
            'АО "ТБанк"',
            "АКЦИОНЕРНОЕ ОБЩЕСТВО «ТБАНК»",
            "Дата и время\nоперации",
            "Пополнения:",
            "Расходы:",
            "С уважением,",
        ):
            pos = block.find(marker)
            if pos >= 0:
                cut_positions.append(pos)
        if cut_positions:
            block = block[:min(cut_positions)]

        m = re.match(
            r"(?s)^\s*"
            r"([+-]?\d[\d ]*[.,]\d{2})\s+(\S+)\s+"
            r"([+-]?\d[\d ]*[.,]\d{2})\s+₽\s+"
            r"(.*?)\s*$",
            block,
        )
        if not m:
            continue

        operation_amount, operation_currency, card_amount, description = m.groups()

        card_match = re.search(r"(?:^|\s)(—|\d{4})\s*$", description)
        card_last4 = ""
        if card_match:
            card_last4 = card_match.group(1)
            description = description[:card_match.start()].strip()

        desc = normalize(description)

        txs.append({
            "date": pair.group(1),
            "code": f"TBANK_{i + 1:05d}",
            "description": desc,
            "merchant": extract_tbank_merchant(desc),
            "amount": money_to_float(card_amount),
            "bank": "Т-Банк",
            "posting_date": pair.group(3),
            "card_last4": card_last4,
            "operation_currency": operation_currency,
            "operation_amount": money_to_float(operation_amount),
        })

    if not txs:
        raise ValueError("Не удалось распознать операции в выписке Т-Банка.")
    return txs



SBER_PAIR_RE = re.compile(
    r"(?m)^(\d{2}\.\d{2}\.\d{4})\n"
    r"(\d{2}\.\d{2}\.\d{4})\n"
    r"(\d{2}:\d{2})\n"
    r"(\d{6})\n"
)

SBER_AMOUNT_LINE_RE = re.compile(
    r"^\s*(\+?\d[\d\u00a0 ]*,\d{2})(?:\s+.*)?$"
)


def extract_sber_merchant(description: str) -> str:
    s = normalize(description)

    # Strip the standard card suffix.
    s = re.sub(r"\s*Операция по карте\s+\*+\d{4}\s*$", "", s, flags=re.I).strip()

    # Strip standard SBP boilerplate.
    s = re.sub(
        r"\s*Покупка по СБП в ТСТ (?:другого банка|Сбербанка)\.*\s*$",
        "",
        s,
        flags=re.I,
    ).strip()

    # Strip common location prefixes from merchant descriptors.
    s = re.sub(
        r"^(?:MOSCOW|MOSKVA|SANKT-PETERBU|Krasnogorsk)\s+",
        "",
        s,
        flags=re.I,
    ).strip()

    # QR suffixes are noise for merchant presentation, but keeping merchant stem is useful.
    s = re.sub(r"_(?:P|E)_QR\.?$", "", s, flags=re.I).strip()

    return s[:100] or "Операция"


def parse_sber_pdf_text(text: str) -> list[dict[str, Any]]:
    """
    Parse Sber's 'Выписка по счёту кредитной карты'.

    Stable block structure:
      operation date
      posting date
      time
      6-digit authorization code
      bank category
      merchant/description
      amount in RUB [and balance]
      optional foreign-currency amount
      balance
    """
    pairs = list(SBER_PAIR_RE.finditer(text))
    if not pairs:
        raise ValueError("Не удалось найти операции в выписке Сбера.")

    txs: list[dict[str, Any]] = []

    for i, pair in enumerate(pairs):
        end = pairs[i + 1].start() if i + 1 < len(pairs) else len(text)
        block = text[pair.end():end]

        # Remove repeated page headers/footers and document footer.
        for marker in (
            "Продолжение на следующей странице",
            "Дата формирования документа",
            "Выписка по счёту кредитной карты Страница",
        ):
            pos = block.find(marker)
            if pos >= 0:
                block = block[:pos]

        lines = [ln.strip() for ln in block.splitlines() if ln.strip()]
        if len(lines) < 2:
            continue

        bank_category = lines[0]
        amount_idx = None
        raw_amount = None

        # First amount-looking line after category is the transaction RUB amount.
        # The second number on the same line, if present, is the running balance.
        for j, line in enumerate(lines[1:], start=1):
            m = SBER_AMOUNT_LINE_RE.match(line)
            if m:
                raw_amount = m.group(1)
                amount_idx = j
                break

        if amount_idx is None or raw_amount is None:
            continue

        description = normalize(" ".join(lines[1:amount_idx]))
        amount_abs = money_to_float(raw_amount.replace("+", ""))

        category_lower = bank_category.lower()
        desc_lower = description.lower()

        is_credit = (
            raw_amount.startswith("+")
            or "возврат" in category_lower
            or "перевод на карту" in category_lower
            or "пополнение" in category_lower
            or "зачислен" in category_lower
            or "возврат" in desc_lower
        )
        amount = amount_abs if is_credit else -amount_abs

        card_match = re.search(r"\*+(\d{4})", description)
        card_last4 = card_match.group(1) if card_match else ""

        txs.append({
            "date": pair.group(1),
            "code": f"SBER_{pair.group(4)}_{i + 1:04d}",
            # Include Sber's category in searchable description but keep merchant separately.
            "description": f"{bank_category} {description}",
            "merchant": extract_sber_merchant(description),
            "amount": amount,
            "bank": "Сбер",
            "posting_date": pair.group(2),
            "auth_code": pair.group(4),
            "card_last4": card_last4,
            "bank_category": bank_category,
        })

    if not txs:
        raise ValueError("Не удалось распознать операции в выписке Сбера.")
    return txs



VTB_ROW_START_RE = re.compile(
    r"(?m)^(\d{2}\.\d{2}\.\d{4})(?:\s+|\n)(\d{2}:\d{2}(?::\d{2})?)"
)

RAIFF_ROW_RE = re.compile(
    r"(?m)^\s*(\d+)\s+(\d{2}\.\d{2}\.\d{2,4})\s+(\d{2}\.\d{2}\.\d{2,4})\s+"
)

GENERIC_DATE_RE = re.compile(
    r"(?m)^(?:\d+\s+)?(\d{2}[./-]\d{2}[./-]\d{2,4})(?:\s+(\d{2}:\d{2}(?::\d{2})?))?"
)
GENERIC_MONEY_RE = re.compile(
    r"(?<!\d)([+-]?(?:\d{1,3}(?:[ \u00a0.,]\d{3})+|\d+)(?:[.,-]\d{2}))\s*(₽|RUB|RUR)?",
    re.I,
)

DEBIT_WORDS = (
    "ОПЛАТА", "ПОКУПКА", "СПИСАН", "ПЕРЕВОД НА", "ПЕРЕВОД ПО", "СНЯТИ",
    "ВЫДАЧА НАЛИЧ", "КОМИСС", "ПЛАТА", "PAYMENT", "PURCHASE", "DEBIT",
)
CREDIT_WORDS = (
    "ПОПОЛН", "ЗАЧИСЛ", "ВОЗВРАТ", "ПЕРЕВОД С НОМЕРА", "ПЕРЕВОД ОТ",
    "ВНЕСЕНИЕ НАЛИЧ", "CREDIT",
)


def _parse_money_generic(raw: str) -> float:
    s = raw.replace("\u00a0", " ").strip()
    # Raiffeisen old-style "2,000-00" -> 2000.00
    if re.match(r"^-?\d{1,3}(?:,\d{3})*-\d{2}$", s):
        neg = s.startswith("-")
        s2 = s.lstrip("-").replace(",", "").replace("-", ".")
        val = float(s2)
        return -val if neg else val

    # Russian spaces thousands + comma decimal.
    if "," in s and "." not in s:
        s = s.replace(" ", "").replace(",", ".")
    else:
        # If both separators occur, assume commas/spaces are grouping.
        s = s.replace(" ", "").replace(",", "")
    return float(s)


def extract_vtb_merchant(desc: str) -> str:
    s = normalize(desc)
    # Common VTB descriptions put the useful counterparty/merchant at the end.
    for marker in ("ОПИСАНИЕ ОПЕРАЦИИ", "ОПИСАНИЕ:"):
        s = s.replace(marker, " ")
    s = re.sub(r"\b(?:RUS|RUB|RUВ)\b", " ", s, flags=re.I)
    s = normalize(s)
    return s[-100:] if len(s) > 100 else (s or "Операция ВТБ")


def parse_vtb_pdf_text(text: str) -> list[dict[str, Any]]:
    """
    Supports the common VTB retail statement family described with:
    operation datetime, bank processing date, operation amount, card/account
    income/expense and description.
    """
    starts = list(VTB_ROW_START_RE.finditer(text))
    if not starts:
        raise ValueError("Не удалось найти операции в выписке ВТБ.")

    txs = []
    for i, m in enumerate(starts):
        end = starts[i + 1].start() if i + 1 < len(starts) else len(text)
        block = normalize(text[m.end():end])
        if not block:
            continue

        # Remove processing dates/times before looking for monetary values.
        # Otherwise "08.06.2023" can be misread as 8.06.
        money_block = re.sub(r"\b\d{2}\.\d{2}\.\d{4}\b", " ", block)
        money_block = re.sub(r"\b\d{2}:\d{2}(?::\d{2})?\b", " ", money_block)

        amounts = []
        for mm in GENERIC_MONEY_RE.finditer(money_block):
            try:
                amounts.append((_parse_money_generic(mm.group(1)), mm.group(0), mm.start()))
            except Exception:
                pass
        if not amounts:
            continue

        upper = block.upper()
        # Prefer explicit "расход" value if textual labels survived extraction.
        expense_match = re.search(r"(?:РАСХОД|ДЕБЕТ)\D{0,20}(\d[\d \u00a0.,]*[.,]\d{2})", block, re.I)
        income_match = re.search(r"(?:ПРИХОД|КРЕДИТ)\D{0,20}(\d[\d \u00a0.,]*[.,]\d{2})", block, re.I)

        if expense_match:
            amount = -abs(_parse_money_generic(expense_match.group(1)))
        elif income_match and not any(w in upper for w in DEBIT_WORDS):
            amount = abs(_parse_money_generic(income_match.group(1)))
        else:
            # Signed operation amount if present.
            signed = [a for a in amounts if a[0] < 0]
            if signed:
                amount = signed[0][0]
            else:
                # Purchase/payment descriptions are debits; transfers-in/refunds credits.
                op_amount = abs(amounts[0][0])
                if any(w in upper for w in CREDIT_WORDS) and not any(w in upper for w in DEBIT_WORDS):
                    amount = op_amount
                else:
                    amount = -op_amount

        # Remove numbers to produce a cleaner merchant-like description.
        desc = re.sub(r"\d[\d \u00a0.,-]*\s*(?:RUB|RUR|RUВ|₽)?", " ", block, flags=re.I)
        desc = normalize(desc)

        txs.append({
            "date": m.group(1),
            "code": f"VTB_{i+1:05d}",
            "description": desc,
            "merchant": extract_vtb_merchant(desc),
            "amount": round(amount, 2),
            "bank": "ВТБ",
            "parser_confidence": "bank_specific_beta",
        })
    if not txs:
        raise ValueError("Не удалось распознать операции в выписке ВТБ.")
    return txs


def extract_raiff_merchant(desc: str) -> str:
    s = normalize(desc)
    # Card lines commonly: CARD **9997 11AUG RUR 443 MERCHANT CITY
    m = re.search(r"\bCARD\b.*?\b(?:RUR|RUB)\b\s+[\d.]+\s+(.+)$", s, re.I)
    if m:
        merchant = m.group(1)
        merchant = re.sub(r"\s+(?:MOSKVA|MOSCOW|SANKT-PETERBU|RUS|RU)$", "", merchant, flags=re.I)
        return merchant[:100].strip()
    # SBP payment lines: "Оплата покупки 329.00 RUB МПП - Платные дороги."
    m = re.search(r"Оплата покупки(?: по карте)?[.\s]+(?:[\d.,]+\s+RUB\s+)?(.+)", s, re.I)
    if m:
        return m.group(1)[:100].strip()
    return s[-100:] if len(s) > 100 else (s or "Операция Райффайзен")


def parse_raiffeisen_pdf_text(text: str) -> list[dict[str, Any]]:
    rows = list(RAIFF_ROW_RE.finditer(text))
    if not rows:
        raise ValueError("Не удалось найти операции в выписке Райффайзенбанка.")
    txs = []
    for i, m in enumerate(rows):
        end = rows[i+1].start() if i+1 < len(rows) else len(text)
        block = normalize(text[m.end():end])
        upper = block.upper()

        # Raiff account statements usually carry an amount like 2,000-00.
        candidates = []
        for mm in re.finditer(r"(?<!\d)(\d{1,3}(?:,\d{3})*-\d{2}|\d[\d \u00a0]*[.,]\d{2})(?!\d)", block):
            try:
                val = _parse_money_generic(mm.group(1))
                candidates.append((val, mm.start(), mm.group(1)))
            except Exception:
                pass
        if not candidates:
            continue

        # Skip long account/correspondent numbers by requiring reasonable transaction magnitude token.
        amount_abs = abs(candidates[0][0])
        if amount_abs == 0:
            continue

        if any(w in upper for w in CREDIT_WORDS) and not any(w in upper for w in DEBIT_WORDS):
            amount = amount_abs
        else:
            amount = -amount_abs

        desc = block
        txs.append({
            "date": normalize_date(m.group(2)),
            "code": f"RAIFF_{m.group(1)}",
            "description": desc,
            "merchant": extract_raiff_merchant(desc),
            "amount": round(amount, 2),
            "bank": "Райффайзенбанк",
            "parser_confidence": "bank_specific_beta",
        })
    if not txs:
        raise ValueError("Не удалось распознать операции в выписке Райффайзенбанка.")
    return txs


def _tax_signal_in_text(s: str) -> bool:
    up = s.upper()
    for _cat, pats in RULES:
        if any(re.search(p, up, re.I) for p in pats):
            return True
    return False


def parse_generic_tax_pdf_text(text: str, bank_display: str = "Банк") -> list[dict[str, Any]]:
    """
    Strict fallback for digital PDFs from unsupported banks.

    It does NOT try to reconstruct the whole ledger. It only emits transaction
    blocks that contain a tax-deduction signal AND where an amount can be
    associated with the block with reasonable confidence.
    """
    starts = list(GENERIC_DATE_RE.finditer(text))
    if not starts:
        raise ValueError("В PDF не удалось найти таблицу операций.")

    out = []
    seen = set()
    for i, m in enumerate(starts):
        end = starts[i+1].start() if i+1 < len(starts) else min(len(text), m.end()+1200)
        block_raw = text[m.start():end]
        block = normalize(block_raw)
        if not _tax_signal_in_text(block):
            continue

        upper = block.upper()
        # Avoid document headers that happen to mention medical/insurance words.
        if len(block) > 900:
            continue

        money_scan_block = re.sub(r"\b\d{2}[./-]\d{2}[./-]\d{2,4}\b", " ", block)
        money_scan_block = re.sub(r"\b\d{2}:\d{2}(?::\d{2})?\b", " ", money_scan_block)

        money = []
        for mm in GENERIC_MONEY_RE.finditer(money_scan_block):
            raw = mm.group(1)
            try:
                val = _parse_money_generic(raw)
            except Exception:
                continue
            # Reject obvious years/account fragments and zeroes.
            if val == 0 or abs(val) > 10_000_000:
                continue
            score = 0
            if mm.group(2):
                score += 3
            if raw.startswith(("+", "-")):
                score += 2
            # Transaction amount usually sits near purchase/merchant text.
            dist = min(
                [abs(mm.start()-p) for p in [upper.find("ОПЛАТ"), upper.find("ПОКУП"), upper.find("MED"), upper.find("АПТЕК"), upper.find("FIT") ] if p >= 0]
                or [999]
            )
            if dist < 250:
                score += 2
            money.append((score, mm.start(), val, raw))

        if not money:
            continue
        money.sort(key=lambda x: (-x[0], x[1]))
        best = money[0]
        # Require some structural evidence; otherwise do not invent a number.
        if best[0] < 2:
            continue

        val = abs(best[2])
        if any(w in upper for w in CREDIT_WORDS) and not any(w in upper for w in DEBIT_WORDS):
            amount = val
        else:
            amount = -val

        fingerprint = (m.group(1), round(amount, 2), block[:160])
        if fingerprint in seen:
            continue
        seen.add(fingerprint)

        # Use the tax-relevant part of the block as merchant-ish label.
        merchant = block[:100]
        for token in ("MEDSI", "MEDSKAN", "АПТЕК", "FITNESS", "ФИТНЕС", "СТРАХ", "INSURANCE", "КЛИНИК", "STOMAT"):
            pos = upper.find(token)
            if pos >= 0:
                merchant = block[max(0, pos-30):pos+70]
                break

        out.append({
            "date": normalize_date(m.group(1).replace("/", ".").replace("-", ".")),
            "code": f"GENERIC_{i+1:05d}",
            "description": block,
            "merchant": normalize(merchant),
            "amount": round(amount, 2),
            "bank": bank_display,
            "parser_confidence": "strict_generic",
        })

    # Returning no candidates is valid: statement may simply have no deductible spend.
    return out


def parse_alfa_pdf_text(text: str) -> list[dict[str, Any]]:
    txs = []
    for m in TX_RE.finditer(text):
        date, code, body = m.groups()
        amounts = RUR_AMOUNT_RE.findall(body)
        if not amounts:
            continue
        amount = money_to_float(amounts[-1])
        desc = normalize(body)
        txs.append({
            "date": date,
            "code": code,
            "description": desc,
            "merchant": extract_merchant(desc),
            "amount": amount,
            "bank": "Альфа-Банк",
        })
    if not txs:
        raise ValueError("Не удалось распознать операции в выписке Альфа-Банка.")
    return txs


def extract_pdf_text(data: bytes) -> str:
    from pypdf import PdfReader
    reader = PdfReader(io.BytesIO(data))
    return "\n".join((p.extract_text() or "") for p in reader.pages)


def detect_pdf_bank_from_text(text: str) -> str:
    upper = text.upper()
    key, _display = recognize_bank(text)

    # Exact known layouts first.
    if key == "sber" and (
        "ВЫПИСКА ПО СЧЁТУ КРЕДИТНОЙ КАРТЫ" in upper
        or "РАСШИФРОВКА ОПЕРАЦИЙ" in upper
        or SBER_PAIR_RE.search(text)
    ):
        return "sber"

    if key == "tbank" and (
        "СПРАВКА О ДВИЖЕНИИ СРЕДСТВ" in upper
        or TBANK_PAIR_RE.search(text)
    ):
        return "tbank"

    if key == "alfa" or ("АЛЬФА" in upper and RUR_AMOUNT_RE.search(text)):
        return "alfa"

    if key == "vtb":
        return "vtb"

    if key == "raiffeisen":
        return "raiffeisen"

    if key != "unknown":
        return f"known:{key}"

    return "unknown"



def parse_alfa_pdf(data: bytes) -> list[dict[str, Any]]:
    return parse_alfa_pdf_text(extract_pdf_text(data))


def parse_tbank_pdf(data: bytes) -> list[dict[str, Any]]:
    return parse_tbank_pdf_text(extract_pdf_text(data))


def parse_sber_pdf(data: bytes) -> list[dict[str, Any]]:
    return parse_sber_pdf_text(extract_pdf_text(data))

def _guess_column(headers: list[str], variants: list[str]) -> str | None:
    low = {h.lower().strip(): h for h in headers}
    for v in variants:
        for lk, original in low.items():
            if v in lk:
                return original
    return None


def parse_csv(data: bytes) -> list[dict[str, Any]]:
    text = None
    for enc in ("utf-8-sig", "utf-8", "cp1251"):
        try:
            text = data.decode(enc)
            break
        except UnicodeDecodeError:
            pass
    if text is None:
        raise ValueError("Не удалось определить кодировку CSV.")
    try:
        delimiter = csv.Sniffer().sniff(text[:5000], delimiters=",;\t").delimiter
    except Exception:
        delimiter = ";"
    reader = csv.DictReader(io.StringIO(text), delimiter=delimiter)
    headers = reader.fieldnames or []
    date_col = _guess_column(headers, ["дата", "date"])
    desc_col = _guess_column(headers, ["опис", "назнач", "merchant", "операц", "description"])
    amount_col = _guess_column(headers, ["сумм", "amount"])
    if not (date_col and desc_col and amount_col):
        raise ValueError("В CSV нужны колонки с датой, описанием операции и суммой.")
    out = []
    for i, row in enumerate(reader):
        raw_amt = str(row.get(amount_col, "")).replace("RUR", "").replace("₽", "").strip()
        try:
            amount = money_to_float(raw_amt)
        except Exception:
            continue
        desc = normalize(str(row.get(desc_col, "")))
        out.append({"date": normalize_date(row.get(date_col, "")), "code": f"CSV_{i+1}", "description": desc, "merchant": desc[:100] or "Операция", "amount": amount})
    return out


def parse_xlsx(data: bytes) -> list[dict[str, Any]]:
    from openpyxl import load_workbook
    wb = load_workbook(io.BytesIO(data), data_only=True, read_only=True)
    ws = wb.active
    rows = list(ws.iter_rows(values_only=True))
    if not rows:
        raise ValueError("Пустой XLSX.")
    headers = [normalize(str(x or "")) for x in rows[0]]
    date_col = _guess_column(headers, ["дата", "date"])
    desc_col = _guess_column(headers, ["опис", "назнач", "merchant", "операц", "description"])
    amount_col = _guess_column(headers, ["сумм", "amount"])
    if not (date_col and desc_col and amount_col):
        raise ValueError("В XLSX нужны колонки с датой, описанием операции и суммой.")
    idx = {h: n for n, h in enumerate(headers)}
    out = []
    for i, row in enumerate(rows[1:], start=2):
        if not row:
            continue
        amount_val = row[idx[amount_col]] if idx[amount_col] < len(row) else None
        try:
            amount = float(amount_val) if isinstance(amount_val, (int, float)) else money_to_float(str(amount_val or ""))
        except Exception:
            continue
        date_val = row[idx[date_col]] if idx[date_col] < len(row) else ""
        desc_val = row[idx[desc_col]] if idx[desc_col] < len(row) else ""
        desc = normalize(str(desc_val or ""))
        out.append({"date": normalize_date(date_val), "code": f"XLSX_{i}", "description": desc, "merchant": desc[:100] or "Операция", "amount": amount})
    return out


def classify(tx: dict[str, Any]) -> dict[str, Any] | None:
    if tx["amount"] >= 0:
        return None
    s = tx["description"].upper()
    for category, patterns in RULES:
        if any(re.search(p, s, re.I) for p in patterns):
            meta = CATEGORY_META[category]
            fingerprint = hashlib.sha1(f'{tx["date"]}|{tx["code"]}|{tx["amount"]}|{tx["description"]}'.encode("utf-8")).hexdigest()[:14]
            return {
                "id": fingerprint,
                "date": tx["date"],
                "year": tx["date"][-4:] if len(tx["date"]) >= 4 else "",
                "merchant": tx["merchant"],
                "amount": round(abs(tx["amount"]), 2),
                "category": category,
                "category_name": meta["name"],
                "emoji": meta["emoji"],
                "confidence": meta["confidence"],
                "status": "Похоже подходит" if meta["confidence"] == "high" else "Нужно подтвердить",
                "note": meta["note"],
                "selected": True,
            }
    return None


def calculate_refund(candidates: list[dict[str, Any]], selected_ids: set[str] | None = None) -> dict[str, Any]:
    # Консервативная модель MVP: 13%, общий социальный лимит 150 000 ₽ на каждый год.
    by_year = defaultdict(float)
    selected = []
    for c in candidates:
        use = c["selected"] if selected_ids is None else c["id"] in selected_ids
        if use:
            selected.append(c)
            by_year[c["year"]] += c["amount"]
    capped = {y: min(v, 150_000.0) for y, v in by_year.items()}
    base = sum(capped.values())
    return {
        "selected_amount": round(sum(c["amount"] for c in selected), 2),
        "tax_base_after_conservative_cap": round(base, 2),
        "refund_from": round(base * 0.13, 2),
        "by_year": [{"year": y, "selected_amount": round(by_year[y], 2), "tax_base": round(capped[y], 2), "refund_from": round(capped[y] * 0.13, 2)} for y in sorted(by_year)],
    }


def analyze_transactions(txs: list[dict[str, Any]]) -> dict[str, Any]:
    candidates = [c for tx in txs if (c := classify(tx)) is not None]
    base = calculate_refund(candidates)
    all_potential = calculate_refund(candidates, {c["id"] for c in candidates})
    group = defaultdict(lambda: {"count": 0, "amount": 0.0})
    for c in candidates:
        group[c["category"]]["count"] += 1
        group[c["category"]]["amount"] += c["amount"]
    groups = []
    for k, v in group.items():
        meta = CATEGORY_META[k]
        groups.append({"category": k, "name": meta["name"], "emoji": meta["emoji"], "count": v["count"], "amount": round(v["amount"], 2), "confidence": meta["confidence"]})
    groups.sort(key=lambda x: x["amount"], reverse=True)
    return {"transactions_scanned": len(txs), "candidates_count": len(candidates), "candidates_amount": round(sum(c["amount"] for c in candidates), 2), "base": base, "potential_if_all_confirmed": all_potential, "groups": groups, "candidates": sorted(candidates, key=lambda x: (x["year"], x["date"], x["amount"]))}


@app.get("/", response_class=HTMLResponse)
def index():
    return HTMLResponse(INDEX_HTML)


@app.get("/health")
def health():
    return {"ok": True, "version": "1.7.0"}


@app.post("/api/analyze")
async def analyze(file: UploadFile = File(...)):
    data = await file.read()
    if not data:
        raise HTTPException(400, "Файл пустой.")
    if len(data) > 30 * 1024 * 1024:
        raise HTTPException(413, "Файл больше 30 МБ. Загрузите более компактную выписку.")
    name = (file.filename or "").lower()
    try:
        if name.endswith(".pdf"):
            pdf_text = extract_pdf_text(data)
            bank = detect_pdf_bank_from_text(pdf_text)
            bank_key, bank_display = recognize_bank(pdf_text)
            if bank == "sber":
                txs, parser = parse_sber_pdf_text(pdf_text), "Сбер PDF"
            elif bank == "tbank":
                txs, parser = parse_tbank_pdf_text(pdf_text), "Т-Банк PDF"
            elif bank == "alfa":
                txs, parser = parse_alfa_pdf_text(pdf_text), "Альфа-Банк PDF"
            elif bank == "vtb":
                try:
                    txs = parse_vtb_pdf_text(pdf_text)
                    parser = "ВТБ PDF"
                except ValueError:
                    txs = parse_generic_tax_pdf_text(pdf_text, bank_display)
                    parser = "ВТБ PDF · универсальный разбор"
            elif bank == "raiffeisen":
                try:
                    txs = parse_raiffeisen_pdf_text(pdf_text)
                    parser = "Райффайзен PDF"
                except ValueError:
                    txs = parse_generic_tax_pdf_text(pdf_text, bank_display)
                    parser = "Райффайзен PDF · универсальный разбор"
            elif bank.startswith("known:"):
                txs = parse_generic_tax_pdf_text(pdf_text, bank_display)
                parser = f"{bank_display} PDF · универсальный разбор"
            else:
                txs = parse_generic_tax_pdf_text(pdf_text, "Неизвестный банк")
                parser = "PDF · универсальный разбор"
        elif name.endswith(".csv"):
            txs, parser = parse_csv(data), "CSV"
        elif name.endswith(".xlsx") or name.endswith(".xlsm"):
            txs, parser = parse_xlsx(data), "XLSX"
        else:
            raise ValueError("Поддерживаются PDF, CSV и XLSX.")

        result = analyze_transactions(txs)
        result["filename"], result["parser"] = file.filename, parser

        analysis_id = uuid.uuid4().hex
        cleanup_analyses()
        ANALYSES[analysis_id] = {
            "created_at": time.time(),
            "result": result,
            "paid": False,
            "payment_id": None,
        }

        # ВАЖНО: до оплаты браузер получает только агрегаты, без категорий и транзакций.
        return JSONResponse({
            "analysis_id": analysis_id,
            "expenses_found": result["candidates_amount"],
            "refund_from": result["potential_if_all_confirmed"]["refund_from"],
            "price": REPORT_PRICE_RUB,
        })
    except ValueError as e:
        raise HTTPException(422, str(e))
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(500, f"Ошибка обработки: {e}")


class PaymentPayload(BaseModel):
    analysis_id: str


@app.post("/api/create-payment")
def create_payment(payload: PaymentPayload, request: Request):
    item = get_analysis_or_404(payload.analysis_id)
    if item.get("paid"):
        return {"status": "succeeded", "confirmation_url": f"{public_base_url(request)}/?analysis={payload.analysis_id}&paid=1"}

    if PAYMENT_TEST_MODE:
        payment_id = "test_" + uuid.uuid4().hex
        item["payment_id"] = payment_id
        return {
            "status": "pending",
            "test_mode": True,
            "confirmation_url": f"{public_base_url(request)}/api/test-pay?analysis_id={payload.analysis_id}&payment_id={payment_id}",
        }

    return_url = f"{public_base_url(request)}/?analysis={payload.analysis_id}&payment=return"
    payment = yookassa_request(
        "POST",
        "/payments",
        {
            "amount": {"value": f"{REPORT_PRICE_RUB:.2f}", "currency": "RUB"},
            "capture": True,
            "confirmation": {"type": "redirect", "return_url": return_url},
            "description": "Tax Radar — персональный налоговый отчёт",
            "metadata": {"analysis_id": payload.analysis_id},
        },
        idempotence_key=str(uuid.uuid4()),
    )
    item["payment_id"] = payment.get("id")
    confirmation_url = (payment.get("confirmation") or {}).get("confirmation_url")
    if not confirmation_url:
        raise HTTPException(502, "ЮKassa не вернула ссылку на оплату.")
    return {"status": payment.get("status", "pending"), "confirmation_url": confirmation_url}


@app.get("/api/test-pay")
def test_pay(analysis_id: str, payment_id: str):
    if not PAYMENT_TEST_MODE:
        raise HTTPException(404, "Тестовая оплата отключена.")
    item = get_analysis_or_404(analysis_id)
    if item.get("payment_id") != payment_id:
        raise HTTPException(400, "Некорректный тестовый платёж.")
    item["paid"] = True
    return RedirectResponse(url=f"/?analysis={analysis_id}&paid=1", status_code=302)


@app.get("/api/payment-status")
def payment_status(analysis_id: str):
    item = get_analysis_or_404(analysis_id)
    if item.get("paid"):
        return {"status": "succeeded", "paid": True}

    payment_id = item.get("payment_id")
    if not payment_id:
        return {"status": "not_created", "paid": False}

    if PAYMENT_TEST_MODE and str(payment_id).startswith("test_"):
        return {"status": "pending", "paid": False}

    payment = yookassa_request("GET", f"/payments/{payment_id}")
    succeeded = payment.get("status") == "succeeded" and bool(payment.get("paid"))
    if succeeded:
        item["paid"] = True
    return {"status": payment.get("status"), "paid": succeeded}


@app.get("/api/report/{analysis_id}")
def paid_report(analysis_id: str):
    item = get_analysis_or_404(analysis_id)
    if not item.get("paid"):
        raise HTTPException(402, "Сначала оплатите полный отчёт.")
    return JSONResponse(item["result"])


class RecalcPayload(BaseModel):
    candidates: list[dict[str, Any]]
    selected_ids: list[str]


@app.post("/api/recalculate")
def recalculate(payload: RecalcPayload):
    return calculate_refund(payload.candidates, set(payload.selected_ids))


class PacketPayload(BaseModel):
    filename: str = "Выписка"
    candidates: list[dict[str, Any]]
    selected_ids: list[str]


@app.post("/api/packet")
def packet(payload: PacketPayload):
    chosen = set(payload.selected_ids)
    selected = [c for c in payload.candidates if c["id"] in chosen]
    calc = calculate_refund(payload.candidates, chosen)

    easy_cats = {"medicine", "fitness", "education"}
    question_cats = {"insurance"}
    extra_cats = {"pharmacy", "donation"}

    easy = [c for c in selected if c["category"] in easy_cats]
    questions = [c for c in selected if c["category"] in question_cats]
    extras = [c for c in selected if c["category"] in extra_cats]

    def calc_subset(items):
        return calculate_refund(items, {c["id"] for c in items}) if items else {"refund_from": 0, "selected_amount": 0}

    easy_calc = calc_subset(easy)
    question_calc = calc_subset(questions)

    def unique_merchants(items):
        out = []
        for c in items:
            m = (c.get("merchant") or "").strip()
            if m and m != "Не удалось определить" and m not in out:
                out.append(m)
        return out

    def short_list(items, limit=8):
        names = unique_merchants(items)
        shown = names[:limit]
        tail = f" + ещё {len(names)-limit}" if len(names) > limit else ""
        return ", ".join(escape(x) for x in shown) + tail if names else "организации из найденных операций"

    rows = "".join(
        f"<tr><td>{escape(c['date'])}</td><td>{escape(c['category_name'])}</td>"
        f"<td>{escape(c['merchant'])}</td><td>{c['amount']:,.2f} ₽</td></tr>"
        for c in selected
    )

    fast_ids = {c['id'] for c in easy + questions}
    fast_calc = calculate_refund(payload.candidates, fast_ids) if fast_ids else {"refund_from": 0}
    extra_increment = max(0, calc['refund_from'] - fast_calc['refund_from'])
    action_count = int(bool(easy)) + int(bool(questions))

    action_html = []
    if easy:
        action_html.append(
            f"<div class='action'><div class='n'>1</div><div><b>Получить справки одним пакетом — от {easy_calc['refund_from']:,.0f} ₽</b>"
            f"<p>Обратиться в {len(unique_merchants(easy))} организаций: {short_list(easy)}. "
            f"Tax Radar уже собрал оплаты и разбил их по организациям.</p></div></div>"
        )
    if questions:
        n = 2 if easy else 1
        action_html.append(
            f"<div class='action'><div class='n'>{n}</div><div><b>Ответить на {len(unique_merchants(questions))} коротких вопроса — до {question_calc['refund_from']:,.0f} ₽</b>"
            f"<p>Уточнить тип договора: {short_list(questions)}. Если это неподходящий тип страховки — просто исключить оплату.</p></div></div>"
        )

    extra_html = ""
    if extras:
        extra_html = f"<div class='extra'><b>Можно попробовать вернуть ещё до {extra_increment:,.0f} ₽</b><p>Лекарства и другие расходы требуют больше документов. Их можно оставить на потом.</p></div>"

    html = f'''<!doctype html>
<html lang="ru"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>Tax Radar — персональный пакет</title>
<style>
:root{{--text:#111827;--muted:#667085;--line:#e5e9f0;--green:#07864c;--greenSoft:#eaf8f1;--brand:#3157e7;--bg:#f5f7fb}}
*{{box-sizing:border-box}}body{{margin:0;background:var(--bg);color:var(--text);font-family:Inter,Arial,sans-serif;line-height:1.45}}.page{{max-width:920px;margin:36px auto;padding:0 18px 60px}}.top{{display:flex;align-items:center;justify-content:space-between;margin-bottom:18px}}.brand{{font-size:20px;font-weight:900}}.brand i{{display:inline-grid;place-items:center;width:34px;height:34px;border-radius:11px;background:linear-gradient(145deg,#3157e7,#6c4df6);color:white;font-style:normal;margin-right:8px}}.source{{font-size:11px;color:var(--muted)}}.hero{{background:linear-gradient(135deg,#121a2f,#22265a);color:#fff;border-radius:24px;padding:28px;box-shadow:0 18px 50px rgba(28,39,60,.12)}}.hero small{{color:#bbc5d9}}.big{{font-size:44px;line-height:1;font-weight:950;letter-spacing:-.045em;margin:6px 0 10px}}.fast{{color:#b8f0d0;font-weight:800;font-size:13px}}h2{{font-size:21px;letter-spacing:-.025em;margin:26px 0 12px}}.action{{display:grid;grid-template-columns:40px 1fr;gap:12px;border:1px solid var(--line);border-radius:16px;padding:15px;margin:9px 0;background:#fff}}.n{{width:36px;height:36px;border-radius:11px;background:#eef2ff;color:#4452ce;display:grid;place-items:center;font-weight:900}}.action b{{font-size:14px}}.action p,.extra p{{color:var(--muted);font-size:12px;margin:4px 0 0}}.extra{{background:#fffaf0;border:1px solid #f0e0bb;border-radius:16px;padding:15px;margin:14px 0}}.extra b{{color:#896100}}.ops{{background:#fff;border:1px solid var(--line);border-radius:16px;overflow:hidden}}table{{width:100%;border-collapse:collapse}}td,th{{padding:10px 9px;border-bottom:1px solid #edf0f4;text-align:left;font-size:11px}}th{{font-size:9px;text-transform:uppercase;letter-spacing:.06em;color:#7c8799}}.note{{background:#f8fafc;border:1px solid var(--line);padding:13px 14px;border-radius:13px;color:#6b7280;font-size:10px;line-height:1.5;margin-top:18px}}@media(max-width:600px){{.page{{margin-top:16px}}.big{{font-size:36px}}.hero{{padding:22px}}th:nth-child(1),td:nth-child(1){{display:none}}}}
</style></head><body><div class="page">
<div class="top"><div class="brand"><i>₽</i>Tax Radar</div><div class="source">Источник: {escape(payload.filename)}</div></div>
<div class="hero"><small>Ваш потенциальный возврат</small><div class="big">от {calc['refund_from']:,.0f} ₽</div><div class="fast">До {fast_calc['refund_from']:,.0f} ₽ — за {action_count or 1} простых действия</div></div>
<h2>Быстрый путь</h2>{''.join(action_html) or '<p>Сначала выберите подходящие операции.</p>'}{extra_html}
<h2>Все включённые операции</h2><div class="ops"><table><thead><tr><th>Дата</th><th>Категория</th><th>Организация</th><th>Сумма</th></tr></thead><tbody>{rows}</tbody></table></div>
<div class="note"><b>Важно:</b> расчёт использует базовые 13% и консервативный общий социальный лимит 150 000 ₽ на год. Это персональный навигатор и рабочий пакет, а не гарантия возврата.</div>
</div></body></html>'''
    return Response(html, media_type="text/html; charset=utf-8", headers={"Content-Disposition": 'attachment; filename="tax_radar_packet.html"'})


INDEX_HTML = r'''<!doctype html>
<html lang="ru">
<head>
<meta charset="utf-8"/>
<meta name="viewport" content="width=device-width,initial-scale=1"/>
<meta name="color-scheme" content="light"/>
<title>Tax Radar — найдём ваш налоговый вычет</title>
<style>
:root{
  --bg:#f6f6f3;
  --surface:#ffffff;
  --text:#11110f;
  --muted:#74746d;
  --line:#e5e5df;
  --line2:#d8d8d1;
  --accent:#b7ff2a;
  --accentInk:#182000;
  --soft:#f0f0eb;
  --green:#287a48;
  --greenSoft:#edf7ef;
  --amber:#806615;
  --amberSoft:#f7f3df;
  --red:#bd3a32;
}
*{box-sizing:border-box}
html{scroll-behavior:smooth}
body{
  margin:0;background:var(--bg);color:var(--text);
  font-family:Inter,ui-sans-serif,system-ui,-apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif;
  -webkit-font-smoothing:antialiased;
}
button,input,select{font:inherit}
button{touch-action:manipulation}
.wrap{max-width:1080px;margin:0 auto;padding:24px 20px 72px}
.header{display:flex;align-items:center;justify-content:space-between;padding:0 0 20px;border-bottom:1px solid var(--line)}
.brand{display:flex;align-items:center;gap:9px;font-weight:820;letter-spacing:-.025em;font-size:18px}
.brandMark{width:30px;height:30px;border-radius:8px;display:grid;place-items:center;background:#111;color:#fff;font-size:14px;font-weight:850}
.beta{font-size:9px;font-weight:800;color:var(--muted);border:1px solid var(--line);background:#fff;padding:4px 7px;border-radius:999px;text-transform:uppercase;letter-spacing:.07em}
.secure{display:flex;align-items:center;gap:7px;color:var(--muted);font-size:11px}
.secureDot{width:7px;height:7px;border-radius:50%;background:#29bd69}

.hero{
  display:grid;grid-template-columns:1.1fr .9fr;gap:56px;align-items:end;
  padding:68px 0 48px;border-bottom:1px solid var(--line);
}
.heroCopy,.preview{position:relative}
.eyebrow{display:flex;align-items:center;gap:8px;color:var(--muted);font-size:10px;font-weight:800;text-transform:uppercase;letter-spacing:.11em;margin-bottom:17px}
.eyebrowDot{width:6px;height:6px;border-radius:50%;background:var(--accent);box-shadow:0 0 0 4px rgba(183,255,42,.15)}
h1{font-size:64px;line-height:.96;letter-spacing:-.06em;margin:0;max-width:720px;font-weight:820}
.heroText{font-size:17px;line-height:1.55;color:var(--muted);max-width:620px;margin:22px 0 27px}
.heroActions{display:flex;gap:13px;align-items:center;flex-wrap:wrap}
.btn{
  appearance:none;border:0;border-radius:10px;padding:12px 15px;font-weight:780;cursor:pointer;
  transition:transform .16s ease,opacity .16s ease;display:inline-flex;align-items:center;justify-content:center;gap:7px;text-decoration:none;
  background:#111;color:#fff;
}
.btn:hover{transform:translateY(-1px)}
.btnPrimary{background:var(--accent);color:var(--accentInk)}
.btnBrand{background:#111;color:#fff}
.btnGhost{background:#fff;color:#222;border:1px solid var(--line)}
.btn:disabled{opacity:.42;cursor:not-allowed;transform:none}
.heroHint{font-size:11px;color:var(--muted)}

.preview{background:#111;color:#fff;border-radius:18px;padding:23px;box-shadow:0 18px 42px rgba(0,0,0,.08)}
.previewTop{display:flex;justify-content:space-between;align-items:center;margin-bottom:28px;color:#989894;font-size:10px;text-transform:uppercase;letter-spacing:.07em}
.previewPill{border:1px solid #2d2d2b;padding:5px 7px;border-radius:999px;color:#b8b8b4}
.previewLabel{font-size:11px;color:#9c9c97}
.previewMoney{font-size:47px;font-weight:820;letter-spacing:-.05em;margin:5px 0 9px}
.previewFast{display:flex;align-items:center;gap:8px;color:#deded8;font-size:12px;font-weight:700;margin-bottom:22px}
.previewFast i{width:20px;height:20px;display:grid;place-items:center;border-radius:50%;font-style:normal;background:var(--accent);color:#151a00;font-size:11px}
.previewSteps{display:grid;grid-template-columns:1fr 1fr;gap:8px}
.previewStep{background:#191917;border:1px solid #282825;border-radius:11px;padding:12px;font-size:11px;color:#bcbcb6;line-height:1.4}
.previewStep b{display:block;color:#fff;font-size:12px;margin-bottom:4px}

.uploaderCard{margin-top:26px;background:#fff;border:1px solid var(--line);border-radius:18px;padding:26px}
.uploaderHead{display:flex;justify-content:space-between;gap:20px;align-items:flex-start;margin-bottom:15px}
.uploaderHead h2{font-size:21px;letter-spacing:-.03em;margin:0 0 5px}
.uploaderHead p{margin:0;color:var(--muted);font-size:11px;line-height:1.5;max-width:650px}
.formatPills{display:flex;gap:5px;flex-wrap:wrap;justify-content:flex-end}
.formatPill{border:1px solid var(--line);background:#fafaf7;border-radius:999px;padding:5px 7px;color:var(--muted);font-size:9px;font-weight:800}
.upload{
  border:1px solid var(--line);border-radius:14px;background:#fafaf7;padding:18px;
  cursor:pointer;transition:.16s ease;display:flex;align-items:center;justify-content:space-between;gap:16px;text-align:left
}
.upload:hover,.upload.drag{border-color:#b9b9b0;background:#f7f7f2}
.upload.selected{border-color:#b9b9b0;background:#fff}
.uploadLeft{display:flex;align-items:center;gap:13px;min-width:0}
.uploadIcon{width:42px;height:42px;flex:0 0 42px;border:1px solid var(--line);background:#fff;border-radius:11px;display:grid;place-items:center;font-size:18px}
.uploadMeta{min-width:0}
.uploadTitle{font-weight:780;font-size:13px;white-space:nowrap;overflow:hidden;text-overflow:ellipsis;max-width:560px}
.uploadSub{color:var(--muted);font-size:10px;margin-top:3px}
.uploadPick{flex:0 0 auto;background:#111;color:#fff;border-radius:9px;padding:9px 11px;font-size:10px;font-weight:800}
.upload.selected .uploadPick{background:#f1f1ed;color:#333}

input[type=file]{display:none}
.uploadBottom{display:flex;align-items:center;gap:11px;margin-top:13px}
.parser{font-size:10px;color:var(--muted)}
.loader{display:none;margin-top:14px}
.loaderTop{display:flex;justify-content:space-between;gap:10px;align-items:center;color:var(--muted);font-size:10px;margin-bottom:7px}
.loaderTop b{color:#222;font-size:11px}
.track{height:3px;background:#ecece7;border-radius:99px;overflow:hidden}
.bar{height:100%;width:28%;background:#111;border-radius:99px;animation:scan 1.05s infinite alternate ease-in-out}
@keyframes scan{from{transform:translateX(-110%)}to{transform:translateX(360%)}}
#error{display:none;margin-top:12px;background:#fff1ef;border:1px solid #f0d5d1;color:var(--red);padding:10px 11px;border-radius:9px;font-size:11px}

.summary{display:none;margin-top:24px}
.summaryCard{background:#fff;border:1px solid var(--line);border-radius:18px;overflow:hidden}
.summaryTop{padding:30px;border-bottom:1px solid var(--line)}
.summaryBadge{display:inline-flex;align-items:center;gap:7px;color:var(--muted);font-size:9px;font-weight:800;text-transform:uppercase;letter-spacing:.1em}
.summaryBadge i{width:18px;height:18px;border-radius:50%;display:grid;place-items:center;background:var(--accent);color:#172000;font-style:normal}
.summaryTitle{font-size:22px;letter-spacing:-.035em;margin:18px 0 4px}
.summarySub{font-size:11px;color:var(--muted)}
.summaryNumbers{display:grid;grid-template-columns:1fr 1fr;gap:0;margin-top:26px;border:1px solid var(--line);border-radius:13px;overflow:hidden}
.summaryNumber{padding:20px}
.summaryNumber+ .summaryNumber{border-left:1px solid var(--line)}
.summaryNumber span{display:block;color:var(--muted);font-size:10px;margin-bottom:5px}
.summaryNumber b{font-size:34px;letter-spacing:-.045em}
.paywall{padding:24px 30px;display:grid;grid-template-columns:1fr auto;gap:24px;align-items:center;background:#111;color:#fff}
.paywallLabel{color:#a6a6a0;font-size:9px;text-transform:uppercase;letter-spacing:.09em;font-weight:800}
.paywallTitle{font-size:20px;font-weight:800;letter-spacing:-.03em;margin-top:5px}
.paywallText{color:#a9a9a3;font-size:10px;line-height:1.5;margin-top:6px;max-width:630px}
.paywallPrice{display:flex;align-items:center;gap:13px}
.price{font-size:28px;font-weight:820;letter-spacing:-.04em;white-space:nowrap}
.payBtn{background:var(--accent);color:var(--accentInk);border:0;border-radius:10px;padding:12px 16px;font-weight:820;cursor:pointer;white-space:nowrap}
.paySecure{font-size:9px;color:#8e8e89;margin-top:7px;text-align:right}
.paymentStatus{display:none;margin-top:12px;padding:10px 11px;border-radius:9px;background:#f6f6f1;color:var(--muted);font-size:10px}
.unlockBadge{display:inline-flex;align-items:center;gap:7px;color:var(--green);font-size:10px;font-weight:800;margin-bottom:12px}
#results{display:none;margin-top:24px}
.resultShell{background:#fff;border:1px solid var(--line);border-radius:16px;overflow:hidden}
.resultTop{padding:30px;border-bottom:1px solid var(--line)}
.resultBadge{display:flex;align-items:center;gap:6px;color:var(--muted);font-size:9px;font-weight:800;text-transform:uppercase;letter-spacing:.1em;margin-bottom:19px}
.resultBadge i{width:16px;height:16px;border-radius:50%;display:grid;place-items:center;background:var(--accent);color:#172000;font-style:normal;font-size:9px}
.resultGrid{display:grid;grid-template-columns:1.18fr .82fr;gap:28px;align-items:end}
.resultLabel{font-size:11px;color:var(--muted);margin-bottom:5px}
.refund{font-size:68px;line-height:.95;letter-spacing:-.06em;font-weight:830}
.refund .from{font-size:20px;color:var(--muted);font-weight:650;letter-spacing:-.02em;margin-right:8px}
.resultMeta{margin-top:10px;color:var(--muted);font-size:10px;line-height:1.45}
.fastBox{border-left:1px solid var(--line);padding-left:26px}
.fastLabel{font-size:9px;color:var(--muted);font-weight:800;text-transform:uppercase;letter-spacing:.09em}
.fastMoney{font-size:31px;font-weight:820;letter-spacing:-.04em;margin-top:5px}
.fastText{color:var(--muted);font-size:10px;line-height:1.45;margin-top:6px}

.resultBody{padding:28px 30px 30px}
.sectionTitleRow{display:flex;justify-content:space-between;gap:18px;align-items:end}
.sectionTitleRow h2{font-size:21px;letter-spacing:-.03em;margin:0 0 4px}
.sectionTitleRow p{margin:0;color:var(--muted);font-size:11px}
.tiny{font-size:9px;color:var(--muted)}
.groups{display:flex;gap:6px;flex-wrap:wrap;margin:16px 0 5px}
.chip{border:1px solid var(--line);background:#fafaf7;border-radius:999px;padding:6px 8px;color:#55554f;font-size:9px;font-weight:700}

.actions{margin-top:14px}
.actionCard{display:grid;grid-template-columns:34px 1fr auto;gap:14px;align-items:start;padding:19px 0;border-top:1px solid var(--line)}
.actionNo{width:32px;height:32px;border-radius:9px;display:grid;place-items:center;background:var(--soft);color:#333;font-size:11px;font-weight:800}
.actionName{font-size:14px;font-weight:790;letter-spacing:-.02em}
.actionDesc{color:var(--muted);font-size:10px;line-height:1.5;margin-top:5px;max-width:660px}
.actionMoney{font-size:12px;font-weight:800;white-space:nowrap}
.actionBtn{margin-top:9px;border:0;background:transparent;padding:0;color:#111;font-size:10px;font-weight:800;cursor:pointer;text-decoration:underline;text-underline-offset:3px}
.batch,.questionBox{display:none;margin-top:12px;border-top:1px solid var(--line)}
.batchItem{padding:12px 0;border-bottom:1px solid var(--line);font-size:10px;color:var(--muted);line-height:1.45}
.batchItem b{display:block;color:#222;font-size:11px;margin-bottom:3px}
.copy{margin-top:6px;color:#111;font-weight:750;cursor:pointer}
.qrow{padding:11px 0;border-bottom:1px solid var(--line);display:grid;grid-template-columns:1fr minmax(220px,320px);gap:12px;align-items:center}
.qrow b{font-size:11px}
.qrow select{width:100%;border:1px solid var(--line);border-radius:8px;background:#fff;padding:8px 9px;color:#333;font-size:10px}

.extra{display:none;margin-top:18px;background:#111;color:#fff;border-radius:13px;padding:17px}
.extraHead{display:flex;justify-content:space-between;gap:16px;align-items:center}
.extraMoney{font-size:22px;font-weight:810;letter-spacing:-.035em}
.extraText{color:#aaa;font-size:10px;line-height:1.45;margin-top:4px}
.extraBtn{border:1px solid #353532;background:#191917;color:#eee;border-radius:8px;padding:8px 10px;font-size:9px;font-weight:750;cursor:pointer}
.extraBody{display:none;margin-top:12px;border-top:1px solid #2d2d2a}
.extraBody.open{display:block}
.extraBody .batchItem{border-color:#2b2b28;color:#aaa}
.extraBody .batchItem b{color:#fff}

.packetRow{display:flex;align-items:center;justify-content:space-between;gap:18px;padding:22px 0 0;margin-top:18px;border-top:1px solid var(--line)}
.packetRow b{font-size:13px}
.packetRow p{margin:4px 0 0;color:var(--muted);font-size:10px}
.divider{height:1px;background:var(--line);margin:24px 0 0}

.detailsCard{margin-top:0}
.detailsSummary{list-style:none;cursor:pointer;padding:18px 0;color:#333}
.detailsSummary::-webkit-details-marker{display:none}
.detailsSummary b{font-size:12px}
.detailsSummary span{font-size:10px;color:var(--muted)}
.detailsInner{padding:0 0 8px}
.tableActions{display:flex;align-items:center;justify-content:space-between;gap:14px;margin-bottom:8px}
.tableActions .left{font-size:9px;color:var(--muted)}
table{width:100%;border-collapse:collapse}
th,td{padding:9px 7px;border-bottom:1px solid var(--line);text-align:left;font-size:10px}
th{color:var(--muted);font-size:8px;text-transform:uppercase;letter-spacing:.06em;font-weight:750}
.status{display:inline-flex;padding:4px 6px;border-radius:999px;font-size:8px;font-weight:800;white-space:nowrap}
.high{background:var(--greenSoft);color:var(--green)}
.verify{background:var(--amberSoft);color:var(--amber)}
.amt{font-weight:760;white-space:nowrap}
.merchant{font-weight:650}
.yearhead td{background:#fafaf7;font-weight:800}
.disclaimer{margin-top:18px;padding-top:17px;border-top:1px solid var(--line);color:var(--muted);font-size:9px;line-height:1.5}
.footer{margin-top:10px;color:#a1a19a;font-size:8px}
.toast{display:none;position:fixed;right:20px;bottom:20px;background:#111;color:#fff;padding:10px 12px;border-radius:9px;font-size:10px;box-shadow:0 12px 30px rgba(0,0,0,.14)}

@media(max-width:820px){
  .wrap{padding:16px 15px 48px}
  .secure{font-size:0}.secureDot{margin:0}
  .hero{grid-template-columns:1fr;gap:28px;padding:45px 0 32px}
  h1{font-size:47px}
  .heroText{font-size:15px}
  .previewMoney{font-size:41px}
  .resultGrid{grid-template-columns:1fr}
  .fastBox{border-left:0;border-top:1px solid var(--line);padding:18px 0 0}
  .refund{font-size:51px}
  .resultTop,.resultBody{padding:22px 18px}
  .actionCard{grid-template-columns:32px 1fr}
  .actionMoney{grid-column:2}
  .qrow{grid-template-columns:1fr}
  .uploaderHead{flex-direction:column}
  .formatPills{justify-content:flex-start}
  .upload{align-items:flex-start}
  .uploadPick{display:none}
  .summaryNumbers{grid-template-columns:1fr}
  .summaryNumber+ .summaryNumber{border-left:0;border-top:1px solid var(--line)}
  .paywall{grid-template-columns:1fr}
  .paywallPrice{justify-content:space-between}
  .paySecure{text-align:left}
  .packetRow{align-items:flex-start;flex-direction:column}
  .sectionTitleRow{align-items:flex-start;flex-direction:column}
}
</style>
</head>
<body>
<div class="wrap">
  <header class="header">
    <div class="brand"><span class="brandMark">₽</span>Tax Radar <span class="beta">UNIVERSAL 1.7</span></div>
    <div class="secure"><span class="secureDot"></span>Файл не сохраняется после анализа</div>
  </header>

  <section class="hero">
    <div class="heroCopy">
      <div class="eyebrow"><span class="eyebrowDot"></span>Персональный поиск налоговых вычетов</div>
      <h1>Найдём ваш налоговый вычет за 5 минут.</h1>
      <p class="heroText">Загрузите банковскую выписку. Мы найдём потенциальные расходы для вычета, посчитаем консервативную сумму по 13% и сведём всё к нескольким понятным действиям.</p>
      <div class="heroActions">
        <label for="file" class="btn btnPrimary">Загрузить выписку <span>→</span></label>
        <span class="heroHint">PDF • CSV • XLSX</span>
      </div>
    </div>
    <div class="preview">
      <div class="previewTop"><span>Результат</span><span class="previewPill">UNIVERSAL 1.7</span></div>
      <div class="previewLabel">Можно вернуть</div>
      <div class="previewMoney">от 20 208 ₽</div>
      <div class="previewFast"><i>✓</i>15 078 ₽ — за 2 простых действия</div>
      <div class="previewSteps">
        <div class="previewStep"><b>1. Получить справки</b>Мы сами сгруппируем организации и подготовим запросы.</div>
        <div class="previewStep"><b>2. Ответить на вопросы</b>Уточнить только те операции, где без вас нельзя.</div>
      </div>
    </div>
  </section>

  <section class="uploaderCard">
    <div class="uploaderHead">
      <div><h2>Проверить выписку</h2><p>Сейчас лучше всего поддерживается PDF Альфа-Банка. Для таблиц нужны дата, описание операции и сумма.</p></div>
      <div class="formatPills"><span class="formatPill">PDF</span><span class="formatPill">CSV</span><span class="formatPill">XLSX</span></div>
    </div>
    <label class="upload" id="drop">
      <input id="file" type="file" accept=".pdf,.csv,.xlsx,.xlsm"/>
      <div class="uploadLeft">
        <div class="uploadIcon">↥</div>
        <div class="uploadMeta">
          <div class="uploadTitle" id="fname">Выберите банковскую выписку</div>
          <div class="uploadSub" id="fileSub">PDF любого банка · также CSV / XLSX · до 30 МБ</div>
        </div>
      </div>
      <span class="uploadPick" id="uploadPick">Выбрать файл</span>
    </label>
    <div class="uploadBottom">
      <button class="btn btnBrand" id="analyze" disabled>Найти вычеты</button>
      <span class="parser" id="parserHint">Файл передаётся по HTTPS и не сохраняется приложением</span>
    </div>
    <div class="loader" id="loader">
      <div class="loaderTop"><b>Анализируем операции</b><span>ищем медицину, фитнес, обучение, страхование…</span></div>
      <div class="track"><div class="bar"></div></div>
    </div>
    <div id="error"></div>
  </section>

  <section class="summary" id="summary">
    <div class="summaryCard">
      <div class="summaryTop">
        <div class="summaryBadge"><i>✓</i>Анализ готов</div>
        <div class="summaryTitle">Мы нашли деньги, которые потенциально можно вернуть</div>
        <div class="summarySub">До оплаты показываем только итог анализа — детали остаются закрыты.</div>
        <div class="summaryNumbers">
          <div class="summaryNumber"><span>Нашли подходящих расходов</span><b id="summaryExpenses">—</b></div>
          <div class="summaryNumber"><span>Потенциальный возврат</span><b id="summaryRefund">—</b></div>
        </div>
        <div class="paymentStatus" id="paymentStatus"></div>
      </div>
      <div class="paywall">
        <div>
          <div class="paywallLabel">Полный персональный отчёт</div>
          <div class="paywallTitle">Что именно нашли и как вернуть деньги</div>
          <div class="paywallText">Откроем операции и категории, сгруппируем организации, подготовим готовые запросы и покажем короткий план действий.</div>
        </div>
        <div>
          <div class="paywallPrice"><div class="price">499 ₽</div><button class="payBtn" id="payBtn">Получить отчёт</button></div>
          <div class="paySecure">Разовая оплата · ЮKassa</div>
        </div>
      </div>
    </div>
  </section>

  <section id="results">
    <div class="resultShell">
      <div class="resultTop">
        <div class="unlockBadge">✓ Полный отчёт оплачен и открыт</div>
        <div class="resultBadge"><i>✓</i>Ваш результат</div>
        <div class="resultGrid">
          <div>
            <div class="resultLabel">Потенциальный возврат</div>
            <div class="refund" id="refund"><span class="from">от</span>—</div>
            <div class="resultMeta" id="resultMeta"></div>
          </div>
          <div class="fastBox">
            <div class="fastLabel">Быстрый путь</div>
            <div class="fastMoney" id="fastRefund">—</div>
            <div class="fastText" id="fastText"></div>
          </div>
        </div>
      </div>

      <div class="resultBody">
        <div class="sectionTitleRow">
          <div><h2>Что нужно сделать</h2><p id="actionLead">Мы соберём операции в несколько понятных шагов.</p></div>
          <div class="tiny" id="foundLabel"></div>
        </div>
        <div class="groups" id="groups"></div>
        <div class="actions" id="actions"></div>

        <div class="extra" id="extra">
          <div class="extraHead">
            <div><div class="extraMoney" id="extraMoney"></div><div class="extraText" id="extraText"></div></div>
            <button class="extraBtn" id="extraToggle">Разобраться</button>
          </div>
          <div class="extraBody" id="extraBody"></div>
        </div>

        <div class="packetRow">
          <div><b>Готовый персональный пакет</b><p>Список выбранных расходов + суммы + короткий чек-лист для получения вычета.</p></div>
          <button class="btn" id="packet">Скачать пакет</button>
        </div>

        <div class="divider"></div>

        <details class="detailsCard">
          <summary class="detailsSummary"><div><b>Все найденные операции</b><br><span>Нужны только для проверки и ручной корректировки результата.</span></div></summary>
          <div class="detailsInner">
            <div class="tableActions"><div class="left">Жёлтые операции включены по умолчанию. Снимите галочку, если операция не подходит.</div><button class="btn btnGhost" id="selectAll">Снять все</button></div>
            <div style="overflow:auto">
              <table><thead><tr><th>Включить</th><th>Дата</th><th>Категория</th><th>Организация</th><th>Сумма</th><th>Статус</th></tr></thead><tbody id="tbody"></tbody></table>
            </div>
          </div>
        </details>

        <div class="disclaimer">Расчёт является ориентиром: Tax Radar использует базовые 13% и консервативный общий социальный лимит 150 000 ₽ на год. Фактическое право на вычет и сумма зависят от подтверждающих документов и уплаченного НДФЛ.</div>
        <div class="footer" id="footer"></div>
      </div>
    </div>
  </section>
</div>
<div class="toast" id="toast"></div>
<script>
let chosenFile=null,result=null,analysisId=null;
const file=document.getElementById('file'),drop=document.getElementById('drop'),analyze=document.getElementById('analyze');
file.onchange=()=>setFile(file.files[0]);
['dragover','dragenter'].forEach(e=>drop.addEventListener(e,x=>{x.preventDefault();drop.classList.add('drag')}));
['dragleave','drop'].forEach(e=>drop.addEventListener(e,x=>{x.preventDefault();drop.classList.remove('drag')}));
drop.addEventListener('drop',e=>{if(e.dataTransfer.files[0])setFile(e.dataTransfer.files[0])});
function setFile(f){
  if(!f)return;
  chosenFile=f;
  drop.classList.add('selected');
  document.getElementById('fname').textContent=f.name;
  document.getElementById('fileSub').textContent=(f.size/1024/1024).toFixed(1)+' МБ · готово к анализу';
  document.getElementById('uploadPick').textContent='Заменить';
  analyze.disabled=false;
  document.getElementById('parserHint').textContent='После анализа исходный файл не сохраняется';
}
const rub=n=>new Intl.NumberFormat('ru-RU',{maximumFractionDigits:0}).format(n)+' ₽';
function escapeHtml(s){return String(s).replace(/[&<>"']/g,m=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#039;'}[m]))}
function ids(){return [...document.querySelectorAll('input[data-id]:checked')].map(x=>x.dataset.id)}
function selected(){const set=new Set(ids());return result.candidates.filter(c=>set.has(c.id))}
function uniq(items){const a=[];items.forEach(c=>{const m=(c.merchant||'').trim();if(m&&m!=='Не удалось определить'&&!a.includes(m))a.push(m)});return a}
function subsetRefund(items){const by={};items.forEach(c=>(by[c.year]??=0,by[c.year]+=c.amount));return Object.values(by).reduce((s,v)=>s+Math.min(v,150000)*.13,0)}
function toast(s){const e=document.getElementById('toast');e.textContent=s;e.style.display='block';setTimeout(()=>e.style.display='none',1800)}
function copyText(s){navigator.clipboard?.writeText(s).then(()=>toast('Готовый запрос скопирован')).catch(()=>toast('Скопируйте текст вручную'))}

analyze.onclick=async()=>{
  document.getElementById('error').style.display='none';
  document.getElementById('loader').style.display='block';
  analyze.disabled=true;
  const fd=new FormData();fd.append('file',chosenFile);
  try{
    const r=await fetch('/api/analyze',{method:'POST',body:fd});
    const data=await r.json();
    if(!r.ok)throw new Error(data.detail||'Ошибка анализа');
    analysisId=data.analysis_id;
    localStorage.setItem('taxRadarAnalysisId',analysisId);
    showSummary(data);
  }catch(e){
    const er=document.getElementById('error');er.textContent=e.message;er.style.display='block'
  }finally{
    document.getElementById('loader').style.display='none';analyze.disabled=false
  }
};


function showSummary(data){
  document.getElementById('results').style.display='none';
  document.getElementById('summary').style.display='block';
  document.getElementById('summaryExpenses').textContent=rub(data.expenses_found);
  document.getElementById('summaryRefund').textContent='от '+rub(data.refund_from);
  document.getElementById('summary').scrollIntoView({behavior:'smooth',block:'start'});
}

async function buyReport(){
  if(!analysisId)return;
  const btn=document.getElementById('payBtn');
  const status=document.getElementById('paymentStatus');
  btn.disabled=true;btn.textContent='Открываем оплату…';
  status.style.display='none';
  try{
    const r=await fetch('/api/create-payment',{
      method:'POST',
      headers:{'Content-Type':'application/json'},
      body:JSON.stringify({analysis_id:analysisId})
    });
    const d=await r.json();
    if(!r.ok)throw new Error(d.detail||'Не удалось создать оплату');
    if(d.status==='succeeded'){await unlockReport();return}
    window.location.href=d.confirmation_url;
  }catch(e){
    status.textContent=e.message;
    status.style.display='block';
    btn.disabled=false;btn.textContent='Получить отчёт';
  }
}
document.getElementById('payBtn').onclick=buyReport;

async function unlockReport(){
  if(!analysisId)return;
  const status=document.getElementById('paymentStatus');
  status.style.display='block';status.textContent='Проверяем оплату…';
  try{
    const sr=await fetch('/api/payment-status?analysis_id='+encodeURIComponent(analysisId));
    const sd=await sr.json();
    if(!sr.ok)throw new Error(sd.detail||'Не удалось проверить оплату');
    if(!sd.paid){
      status.textContent=sd.status==='pending'?'Платёж ещё обрабатывается. Обновим статус через несколько секунд.':'Оплата не подтверждена.';
      if(sd.status==='pending')setTimeout(unlockReport,2500);
      return;
    }
    const rr=await fetch('/api/report/'+encodeURIComponent(analysisId));
    const report=await rr.json();
    if(!rr.ok)throw new Error(report.detail||'Не удалось открыть отчёт');
    result=report;
    document.getElementById('summary').style.display='none';
    render();
    const url=new URL(window.location.href);
    url.searchParams.delete('payment');url.searchParams.delete('paid');
    url.searchParams.set('analysis',analysisId);
    history.replaceState({},'',url);
  }catch(e){
    status.textContent=e.message;
  }
}

async function resumeAfterPayment(){
  const params=new URLSearchParams(window.location.search);
  const fromUrl=params.get('analysis');
  analysisId=fromUrl||localStorage.getItem('taxRadarAnalysisId');
  if(!analysisId)return;
  if(params.get('payment')==='return'||params.get('paid')==='1'){
    document.getElementById('summary').style.display='block';
    document.getElementById('summaryExpenses').textContent='—';
    document.getElementById('summaryRefund').textContent='—';
    await unlockReport();
  }
}
window.addEventListener('DOMContentLoaded',resumeAfterPayment);

function buildBatch(items){
  const by={};items.forEach(c=>{const k=c.category+'||'+c.merchant;(by[k]??=[]).push(c)});let html='';
  Object.values(by).forEach(arr=>{const c=arr[0],sum=arr.reduce((s,x)=>s+x.amount,0);let req=c.category==='medicine'?'Прошу предоставить справку об оплате медицинских услуг для оформления налогового вычета.':c.category==='fitness'?'Прошу предоставить документы/сведения об оплате услуг для оформления спортивного налогового вычета.':'Прошу предоставить подтверждение оплаты услуг для оформления налогового вычета.';const txt=`Здравствуйте! ${req} Найденные оплаты: ${arr.map(x=>x.date+' — '+rub(x.amount)).join('; ')}.`;html+=`<div class="batchItem"><b>${escapeHtml(c.merchant)} · ${rub(sum)}</b>${arr.length} оплат · ${c.category_name}<div class="copy" onclick='copyText(${JSON.stringify(txt)})'>Скопировать готовый запрос →</div></div>`});return html
}

function renderActions(){
  const s=selected(),easy=s.filter(c=>['medicine','fitness','education'].includes(c.category)),questions=s.filter(c=>c.category==='insurance'),extras=s.filter(c=>['pharmacy','donation'].includes(c.category));
  const fast=[...easy,...questions],total=subsetRefund(s),fastR=subsetRefund(fast),extra=Math.max(0,total-fastR);let count=0,html='';
  if(easy.length){count++;const merchants=uniq(easy),easyR=subsetRefund(easy);html+=`<div class="actionCard"><div class="actionNo">${count}</div><div><div class="actionName">Получить справки одним пакетом</div><div class="actionDesc">${merchants.length} организаций, ${easy.length} найденных оплат. Tax Radar уже сгруппировал, кому и что запросить.</div><button class="actionBtn" onclick="toggleBatch()">Подготовить все запросы</button><div class="batch" id="batch">${buildBatch(easy)}</div></div><div class="actionMoney">от ${rub(easyR)}</div></div>`}
  if(questions.length){count++;const merchants=uniq(questions),qR=subsetRefund(questions);let qhtml='';merchants.forEach(m=>{const related=questions.filter(c=>c.merchant===m);qhtml+=`<div class="qrow"><b>${escapeHtml(m)}</b><select onchange='insuranceAnswer(this,${JSON.stringify(related.map(x=>x.id))})'><option value="">Что это за страховка?</option><option value="keep">Жизнь / ДМС / подходящий договор</option><option value="drop">ОСАГО / каско / другое</option></select></div>`});html+=`<div class="actionCard"><div class="actionNo">${count}</div><div><div class="actionName">Ответить на ${merchants.length} коротких вопрос${merchants.length===1?'':'а'}</div><div class="actionDesc">Не нужно разбираться в договорах заранее — просто укажите тип найденной страховки.</div><button class="actionBtn" onclick="toggleQuestions()">Ответить</button><div class="questionBox" id="questionBox">${qhtml}</div></div><div class="actionMoney">до ${rub(qR)}</div></div>`}
  document.getElementById('actions').innerHTML=html||'<div class="actionCard"><div class="actionNo">✓</div><div><div class="actionName">Основной план уже готов</div><div class="actionDesc">Осталось проверить дополнительные расходы или скачать пакет.</div></div></div>';
  document.getElementById('refund').innerHTML='<span class="from">от</span>'+rub(total);document.getElementById('fastRefund').textContent='от '+rub(fastR);document.getElementById('fastText').textContent=count?`${fastR?Math.round(fastR/Math.max(total,1)*100):0}% найденного возврата — за ${count} ${count===1?'действие':'действия'}.`:'Сначала проверьте найденные операции.';document.getElementById('actionLead').textContent=count?`Вместо ${s.length} отдельных операций — всего ${count} ${count===1?'действие':'действия'}.`:'Сложные расходы вынесены отдельно.';
  const ex=document.getElementById('extra');if(extras.length){ex.style.display='block';document.getElementById('extraMoney').textContent='Ещё до '+rub(extra);const cats=[...new Set(extras.map(c=>c.category_name))];document.getElementById('extraText').textContent=`${cats.join(', ')} — ${extras.length} операций. Здесь нужно больше ручных подтверждений, поэтому они не мешают основному сценарию.`;document.getElementById('extraBody').innerHTML=extras.map(c=>`<div class="batchItem"><b>${c.emoji} ${escapeHtml(c.merchant)} · ${rub(c.amount)}</b>${c.date} · ${escapeHtml(c.note)}</div>`).join('')}else ex.style.display='none';
}
function toggleBatch(){const e=document.getElementById('batch');if(e)e.style.display=e.style.display==='block'?'none':'block'}
function toggleQuestions(){const e=document.getElementById('questionBox');if(e)e.style.display=e.style.display==='block'?'none':'block'}
function insuranceAnswer(sel,relatedIds){if(sel.value==='drop'){document.querySelectorAll('input[data-id]').forEach(x=>{if(relatedIds.includes(x.dataset.id))x.checked=false});recalc()}else if(sel.value==='keep'){document.querySelectorAll('input[data-id]').forEach(x=>{if(relatedIds.includes(x.dataset.id))x.checked=true});recalc()}}
document.getElementById('extraToggle').onclick=()=>{const b=document.getElementById('extraBody');b.classList.toggle('open');document.getElementById('extraToggle').textContent=b.classList.contains('open')?'Скрыть':'Разобраться'};

function render(){
  document.getElementById('results').style.display='block';
  document.getElementById('resultMeta').textContent=`Найдено ${result.candidates_count} потенциальных операций на ${rub(result.candidates_amount)} из ${result.transactions_scanned.toLocaleString('ru-RU')} банковских операций.`;
  document.getElementById('groups').innerHTML=result.groups.map(g=>`<span class="chip">${g.emoji} ${g.name}: ${g.count} · ${rub(g.amount)}</span>`).join('');
  document.getElementById('foundLabel').textContent=`${result.candidates_count} операций · ${rub(result.candidates_amount)}`;
  const byYear={};result.candidates.forEach(c=>(byYear[c.year]??=[]).push(c));let html='';
  Object.keys(byYear).sort().forEach(y=>{html+=`<tr class="yearhead"><td colspan="6">${y} год</td></tr>`;byYear[y].forEach(c=>{html+=`<tr><td><input type="checkbox" data-id="${c.id}" ${c.selected?'checked':''}></td><td>${c.date}</td><td>${c.emoji} ${c.category_name}</td><td class="merchant" title="${escapeHtml(c.note)}">${escapeHtml(c.merchant)}</td><td class="amt">${rub(c.amount)}</td><td><span class="status ${c.confidence==='high'?'high':'verify'}">${c.status}</span></td></tr>`})});
  document.getElementById('tbody').innerHTML=html;document.querySelectorAll('input[data-id]').forEach(x=>x.onchange=recalc);renderActions();document.getElementById('footer').textContent=`Файл: ${result.filename} · распознаватель: ${result.parser}`;document.getElementById('results').scrollIntoView({behavior:'smooth',block:'start'})
}
async function recalc(){const r=await fetch('/api/recalculate',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({candidates:result.candidates,selected_ids:ids()})});await r.json();renderActions()}
document.getElementById('selectAll').onclick=()=>{const boxes=[...document.querySelectorAll('input[data-id]')],all=boxes.every(x=>x.checked);boxes.forEach(x=>x.checked=!all);document.getElementById('selectAll').textContent=!all?'Снять все':'Отметить все';recalc()};
document.getElementById('packet').onclick=async()=>{const r=await fetch('/api/packet',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({filename:result.filename,candidates:result.candidates,selected_ids:ids()})});if(!r.ok)return alert('Не удалось сформировать пакет');const blob=await r.blob(),u=URL.createObjectURL(blob),a=document.createElement('a');a.href=u;a.download='tax_radar_packet.html';a.click();URL.revokeObjectURL(u)};
</script>
</body>
</html>'''


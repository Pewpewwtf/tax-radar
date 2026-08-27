from __future__ import annotations

import csv
import hashlib
import io
import re
from collections import defaultdict
from datetime import datetime
from html import escape
from typing import Any

from fastapi import FastAPI, File, UploadFile, HTTPException
from fastapi.responses import HTMLResponse, JSONResponse, Response
from pydantic import BaseModel

app = FastAPI(title="Tax Radar", version="1.3.0")

CATEGORY_META = {
    "medicine": {"name": "Медицина", "emoji": "🏥", "confidence": "high", "note": "Похоже на оплату медицинских услуг. Для вычета потребуется подтверждение от медицинской организации."},
    "pharmacy": {"name": "Аптеки / лекарства", "emoji": "💊", "confidence": "verify", "note": "Аптечная покупка сама по себе не гарантирует вычет: обычно нужны назначение врача и подтверждающие документы."},
    "fitness": {"name": "Спорт / фитнес", "emoji": "🏋️", "confidence": "verify", "note": "Нужно проверить, дает ли организация право на спортивный вычет за соответствующий год."},
    "education": {"name": "Обучение", "emoji": "🎓", "confidence": "verify", "note": "Нужно подтвердить, что платеж относится к обучению и организация/ИП соответствует условиям вычета."},
    "insurance": {"name": "Страхование", "emoji": "🛡️", "confidence": "verify", "note": "Нужно определить тип договора. ОСАГО/каско не являются обычным социальным вычетом."},
    "donation": {"name": "Благотворительность", "emoji": "❤️", "confidence": "verify", "note": "Нужно проверить получателя и документы. Такие расходы не включаются в базовый расчет автоматически."},
}

RULES = [
    ("medicine", [r"MCC(?:8011|8062|8099|8021|8031|8041|8042|8049)\b", r"\bMEDSI\b", r"\bMEDSKAN\w*\b", r"\bKLINIKA\b", r"КЛИНИК", r"MEDICINSK", r"МЕДИЦ", r"STOMAT", r"СТОМАТ", r"\bDENT(?:AL)?\b", r"КОСМЕТОЛ", r"\bBESTCLIN"]),
    ("pharmacy", [r"MCC5912\b", r"\bAPTEKA\b", r"АПТЕК", r"\bGORZDRAV\b", r"\bMSKAPT"]),
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


def parse_alfa_pdf(data: bytes) -> list[dict[str, Any]]:
    from pypdf import PdfReader
    reader = PdfReader(io.BytesIO(data))
    text = "\n".join((p.extract_text() or "") for p in reader.pages)
    txs = []
    for m in TX_RE.finditer(text):
        date, code, body = m.groups()
        amounts = RUR_AMOUNT_RE.findall(body)
        if not amounts:
            continue
        amount = money_to_float(amounts[-1])
        desc = normalize(body)
        txs.append({"date": date, "code": code, "description": desc, "merchant": extract_merchant(desc), "amount": amount})
    if not txs:
        raise ValueError("Не удалось распознать операции в PDF. Сейчас лучше всего поддерживается выписка Альфа-Банка.")
    return txs


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
    return {"ok": True, "version": "1.0.0"}


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
            txs, parser = parse_alfa_pdf(data), "Альфа-Банк PDF"
        elif name.endswith(".csv"):
            txs, parser = parse_csv(data), "CSV"
        elif name.endswith(".xlsx") or name.endswith(".xlsm"):
            txs, parser = parse_xlsx(data), "XLSX"
        else:
            raise ValueError("Поддерживаются PDF, CSV и XLSX.")
        result = analyze_transactions(txs)
        result["filename"], result["parser"] = file.filename, parser
        return JSONResponse(result)
    except ValueError as e:
        raise HTTPException(422, str(e))
    except Exception as e:
        raise HTTPException(500, f"Ошибка обработки: {e}")


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

.uploaderCard{margin-top:22px;background:#fff;border:1px solid var(--line);border-radius:15px;padding:22px}
.uploaderHead{display:flex;justify-content:space-between;gap:20px;align-items:flex-start;margin-bottom:15px}
.uploaderHead h2{font-size:21px;letter-spacing:-.03em;margin:0 0 5px}
.uploaderHead p{margin:0;color:var(--muted);font-size:11px;line-height:1.5;max-width:650px}
.formatPills{display:flex;gap:5px;flex-wrap:wrap;justify-content:flex-end}
.formatPill{border:1px solid var(--line);background:#fafaf7;border-radius:999px;padding:5px 7px;color:var(--muted);font-size:9px;font-weight:800}
.upload{border:1px dashed #c9c9c1;border-radius:12px;background:#fafaf7;padding:28px 20px;text-align:center;cursor:pointer;transition:.16s ease}
.upload.drag{border-color:#111;background:#f6f6ef}
.uploadIcon{width:34px;height:34px;margin:0 auto 9px;border:1px solid var(--line);background:#fff;border-radius:9px;display:grid;place-items:center;font-size:18px}
.uploadTitle{font-weight:780;font-size:14px}
.uploadSub{color:var(--muted);font-size:10px;margin-top:4px}
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
  .packetRow{align-items:flex-start;flex-direction:column}
  .sectionTitleRow{align-items:flex-start;flex-direction:column}
}
</style>
</head>
<body>
<div class="wrap">
  <header class="header">
    <div class="brand"><span class="brandMark">₽</span>Tax Radar <span class="beta">CLOUD 1.3</span></div>
    <div class="secure"><span class="secureDot"></span>Файл обрабатывается локально</div>
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
      <div class="previewTop"><span>Результат</span><span class="previewPill">CLOUD 1.3</span></div>
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
      <div class="uploadIcon">⇧</div>
      <div class="uploadTitle" id="fname">Перетащите выписку сюда</div>
      <div class="uploadSub">или нажмите, чтобы выбрать файл</div>
    </label>
    <div class="uploadBottom">
      <button class="btn btnBrand" id="analyze" disabled>Найти вычеты</button>
      <span class="parser" id="parserHint">Файл никуда не загружается в облако</span>
    </div>
    <div class="loader" id="loader">
      <div class="loaderTop"><b>Анализируем операции</b><span>ищем медицину, фитнес, обучение, страхование…</span></div>
      <div class="track"><div class="bar"></div></div>
    </div>
    <div id="error"></div>
  </section>

  <section id="results">
    <div class="resultShell">
      <div class="resultTop">
        <div class="resultBadge"><i>✓</i>Анализ готов</div>
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
let chosenFile=null,result=null;
const file=document.getElementById('file'),drop=document.getElementById('drop'),analyze=document.getElementById('analyze');
file.onchange=()=>setFile(file.files[0]);
['dragover','dragenter'].forEach(e=>drop.addEventListener(e,x=>{x.preventDefault();drop.classList.add('drag')}));
['dragleave','drop'].forEach(e=>drop.addEventListener(e,x=>{x.preventDefault();drop.classList.remove('drag')}));
drop.addEventListener('drop',e=>{if(e.dataTransfer.files[0])setFile(e.dataTransfer.files[0])});
function setFile(f){if(!f)return;chosenFile=f;document.getElementById('fname').textContent='✓ '+f.name;analyze.disabled=false;document.getElementById('parserHint').textContent=f.name.toLowerCase().endsWith('.pdf')?'PDF будет разобран локальным backend-сервисом':'Таблица будет разобрана локальным backend-сервисом'}
const rub=n=>new Intl.NumberFormat('ru-RU',{maximumFractionDigits:0}).format(n)+' ₽';
function escapeHtml(s){return String(s).replace(/[&<>"']/g,m=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#039;'}[m]))}
function ids(){return [...document.querySelectorAll('input[data-id]:checked')].map(x=>x.dataset.id)}
function selected(){const set=new Set(ids());return result.candidates.filter(c=>set.has(c.id))}
function uniq(items){const a=[];items.forEach(c=>{const m=(c.merchant||'').trim();if(m&&m!=='Не удалось определить'&&!a.includes(m))a.push(m)});return a}
function subsetRefund(items){const by={};items.forEach(c=>(by[c.year]??=0,by[c.year]+=c.amount));return Object.values(by).reduce((s,v)=>s+Math.min(v,150000)*.13,0)}
function toast(s){const e=document.getElementById('toast');e.textContent=s;e.style.display='block';setTimeout(()=>e.style.display='none',1800)}
function copyText(s){navigator.clipboard?.writeText(s).then(()=>toast('Готовый запрос скопирован')).catch(()=>toast('Скопируйте текст вручную'))}

analyze.onclick=async()=>{
  document.getElementById('error').style.display='none';document.getElementById('loader').style.display='block';analyze.disabled=true;
  const fd=new FormData();fd.append('file',chosenFile);
  try{const r=await fetch('/api/analyze',{method:'POST',body:fd});const data=await r.json();if(!r.ok)throw new Error(data.detail||'Ошибка анализа');result=data;render()}
  catch(e){const er=document.getElementById('error');er.textContent=e.message;er.style.display='block'}
  finally{document.getElementById('loader').style.display='none';analyze.disabled=false}
};

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


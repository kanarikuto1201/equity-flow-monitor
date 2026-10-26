"""転換社債の発行を日次で捕捉し、発行時のヘッジ売りを推計する（設計書の2章）。

- 転換社債の多くは私募（144A）で、目論見書は出ない。代わりに会社が「価格決定」の8-Kを出す
- EDGARの全文検索で、その日の8-Kから転換社債の価格決定の発表を探す
- Claudeが発表文を読み、発行額・転換価格・同時の自社株買いなどを取り出す
- 社債を買った投資家（転換社債アービトラージ）は、その会社の株を空売りしてリスクを抑える。
  発行額 × ヘッジ比率 を、発行日の供給として数える（ヘッジ比率は初期値。案件ごとの補正は今後）
- 同時に行う自社株買い（空売りの受け皿になることが多い）は、自社株買いのXBRLデータ側で数える
- 転換時に出る株は、XBRLの「社債の株式転換」から数える（flows.py）
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass, asdict
from datetime import date
from urllib.parse import urlencode

from bs4 import BeautifulSoup

from ..core import llm
from .edgar import EdgarClient

HEDGE_RATIO = 0.45  # 発行額に対するヘッジ売りの比率（初期値）
# Claudeに渡す前の絞り込み：冒頭に「転換社債の価格決定」の言い回しがある文書だけ（費用を抑えるため）
_PRICING_HINT = re.compile(
    # 金額の小数点（$1.3 billion、3.25%）を含むので、文の区切りではなく文字数で範囲を決める
    r"\bpric(?:es|ed|ing)\b.{0,250}?(?:convertible|exchangeable).{0,60}?notes"
    r"|(?:convertible|exchangeable).{0,60}?notes.{0,250}?\bpric(?:es|ed|ing)\b",
    re.I)
SEARCH = "https://efts.sec.gov/LATEST/search-index"

_PROMPT = """You read an SEC filing (usually an 8-K or its press release exhibit) about convertible notes.
Reply with one JSON object only, no other text. Use null when not stated.

Fields:
- "announcement_type": "pricing" if it announces the pricing of an offering of convertible or exchangeable notes,
  "proposed" if it only announces a proposed or launched offering, "closing" if it reports closing/issuance, otherwise "other"
- "issuer": company name
- "ticker": the issuer's stock ticker
- "principal_usd": aggregate principal amount priced, excluding any option to purchase additional notes
- "option_usd": principal amount of the initial purchasers' option to purchase additional notes
- "conversion_price_usd": initial conversion price per share
- "reference_price_usd": last reported sale price of the stock used for the conversion premium
- "concurrent_repurchase_usd": amount of shares the issuer will repurchase concurrently, if stated
- "capped_call": true if capped call transactions are entered
- "exchangeable_into_other_company": true if the notes convert into shares of a different company
- "confidence": "high", "medium" or "low"

Filing text:
"""


@dataclass
class ConvertIssue:
    filed: str
    accession: str
    cik: str
    issuer: str
    status: str
    ticker: str | None = None
    principal_usd: float | None = None
    option_usd: float | None = None
    conversion_price_usd: float | None = None
    reference_price_usd: float | None = None
    concurrent_repurchase_usd: float | None = None
    capped_call: bool | None = None
    hedge_sale_usd: float | None = None
    confidence: str | None = None
    url: str | None = None


def _num(v):
    if v is None:
        return None
    try:
        return float(str(v).replace("$", "").replace(",", ""))
    except ValueError:
        return None


def search_hits(client: EdgarClient, day: date) -> list[dict]:
    """その日の8-Kのうち、転換社債の価格決定に触れる書類を全文検索で探す。"""
    hits, start = [], 0
    while True:
        params = {"q": '"convertible" "notes" "pricing"', "dateRange": "custom", "forms": "8-K",
                  "startdt": day.isoformat(), "enddt": day.isoformat(), "from": start}
        data = json.loads(client.get(f"{SEARCH}?{urlencode(params)}"))
        page = data.get("hits", {}).get("hits", [])
        hits.extend(page)
        total = data.get("hits", {}).get("total", {}).get("value", 0)
        start += len(page)
        if not page or start >= total or start >= 500:
            return hits


def _pick_documents(hits: list[dict]) -> dict[str, dict]:
    """書類番号ごとに1つの文書を選ぶ（発表文の別紙 EX-99 を優先）。"""
    chosen: dict[str, dict] = {}
    for h in hits:
        src = h.get("_source", {})
        adsh = src.get("adsh")
        doc_id = h.get("_id", "")
        filename = doc_id.split(":", 1)[-1] if ":" in doc_id else ""
        if not adsh or not filename or not src.get("ciks"):
            continue
        is_exhibit = bool(re.search(r"ex-?99", filename, re.I))
        prev = chosen.get(adsh)
        if prev is None or (is_exhibit and not prev["is_exhibit"]):
            cik = str(int(src["ciks"][0]))
            name = (src.get("display_names") or [""])[0].split("  (")[0]
            url = f"https://www.sec.gov/Archives/edgar/data/{cik}/{adsh.replace('-', '')}/{filename}"
            chosen[adsh] = {"adsh": adsh, "cik": cik, "issuer": name, "url": url, "is_exhibit": is_exhibit,
                            "filed": src.get("file_date", "").replace("-", "")}
    return chosen


def collect(client: EdgarClient, day: date, log) -> tuple[list[dict], dict]:
    stats = {"convert_docs": 0, "convert_pricings": 0, "convert_errors": 0}
    if not llm.available():
        return [], stats
    try:
        docs = _pick_documents(search_hits(client, day))
    except Exception as exc:
        stats["convert_errors"] += 1
        log(f"転換社債の全文検索に失敗 ({type(exc).__name__}: {exc})")
        return [], stats

    rows = []
    for d in docs.values():
        stats["convert_docs"] += 1
        rec = ConvertIssue(filed=d["filed"] or day.strftime("%Y%m%d"), accession=d["adsh"], cik=d["cik"],
                           issuer=d["issuer"], status="", url=d["url"])
        try:
            html = client.get(d["url"], max_bytes=300_000).decode("utf-8", errors="ignore")
            text = re.sub(r"\s+", " ", BeautifulSoup(html, "lxml").get_text(" "))[:12000]
            # 8-K本体は冒頭が定型の表紙なので、本文全体で探す
            if not _PRICING_HINT.search(text):
                continue
            data = llm.ask_json(_PROMPT + text)
        except Exception as exc:
            stats["convert_errors"] += 1
            log(f"転換社債の読み取りに失敗: {d['url']} ({type(exc).__name__}: {exc})")
            continue
        kind = data.get("announcement_type")
        if kind != "pricing" or data.get("exchangeable_into_other_company"):
            rec.status = f"skipped_{kind or 'unknown'}"
            rows.append(asdict(rec))
            continue
        rec.ticker = data.get("ticker")
        rec.principal_usd = _num(data.get("principal_usd"))
        rec.option_usd = _num(data.get("option_usd"))
        rec.conversion_price_usd = _num(data.get("conversion_price_usd"))
        rec.reference_price_usd = _num(data.get("reference_price_usd"))
        rec.concurrent_repurchase_usd = _num(data.get("concurrent_repurchase_usd"))
        rec.capped_call = data.get("capped_call")
        rec.confidence = data.get("confidence")
        if rec.principal_usd:
            rec.hedge_sale_usd = round(rec.principal_usd * HEDGE_RATIO, 2)
            rec.status = "ok"
            stats["convert_pricings"] += 1
        else:
            rec.status = "ok_missing_amount"
        rows.append(asdict(rec))
    return rows, stats

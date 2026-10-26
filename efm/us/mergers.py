"""現金M&A（設計書の2章・4章）。

上場企業が現金で買収されると、その株式は市場から消える（需要）。
- きっかけ：上場廃止の届出（取引所が出す Form 25-NSE、会社が出す Form 25）
- 買収による廃止か：対象会社に合併関連の書類（DEFM14A、SC TO-T など）が過去にあるか
- 金額：完了時の8-KをClaudeで読み、1株当たりの現金対価を取り出し、発行済株数を掛ける
- 株式対価の部分は需給に影響しない（上場企業同士なら相殺）ので数えない
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass, asdict
from datetime import date, timedelta

from bs4 import BeautifulSoup

from ..core import llm
from .edgar import EdgarClient, IndexEntry, EdgarNotFound

DELISTING_FORMS = {"25-NSE", "25"}
MERGER_FORMS = {"DEFM14A", "DEFM14C", "PREM14A", "PREM14C", "SC TO-T", "SC TO-T/A", "SC 14D9", "SC 14D9/A", "425", "SC 13E3"}
EQUITY_WORDS = re.compile(r"common stock|common shares|ordinary shares|american depositary|class [a-z] shares", re.I)

_PROMPT = """You read a U.S. SEC Form 8-K filed by a company around the date its stock was delisted.
Decide whether it reports the completion of a merger or acquisition of this company, and extract the consideration per share.
Reply with one JSON object only, no other text. Use null when not stated.

Fields:
- "is_acquisition_completed": true if the company was acquired (merger or tender offer completed)
- "acquirer": name of the buyer
- "cash_per_share_usd": cash paid per share of common stock (include cash in mixed cash-and-stock deals; null if all stock)
- "stock_consideration": short description of any stock consideration per share, or null
- "confidence": "high", "medium" or "low"

8-K text:
"""


@dataclass
class CashMerger:
    filed: str
    accession: str
    target_cik: str
    target: str
    status: str
    acquirer: str | None = None
    cash_per_share_usd: float | None = None
    stock_consideration: str | None = None
    shares_outstanding: float | None = None
    cash_usd: float | None = None
    confidence: str | None = None
    source_url: str | None = None


def subject_company(header: str) -> tuple[str | None, str | None]:
    """提出書類のヘッダーから、上場廃止になる会社（SUBJECT COMPANY、なければ FILER）を取り出す。"""
    for block_name in ("SUBJECT COMPANY:", "FILER:"):
        i = header.find(block_name)
        if i == -1:
            continue
        block = header[i : i + 1500]
        name = re.search(r"COMPANY CONFORMED NAME:\s*(.+)", block)
        cik = re.search(r"CENTRAL INDEX KEY:\s*(\d+)", block)
        if cik:
            return str(int(cik.group(1))), (name.group(1).strip() if name else None)
    return None, None


def _recent_filings(client: EdgarClient, cik: str) -> list[dict]:
    data = json.loads(client.get(f"https://data.sec.gov/submissions/CIK{int(cik):010d}.json"))
    r = data.get("filings", {}).get("recent", {})
    keys = ("form", "filingDate", "accessionNumber", "primaryDocument")
    return [dict(zip(keys, vals)) for vals in zip(*(r.get(k, []) for k in keys))]


def _shares_outstanding(client: EdgarClient, cik: str) -> float | None:
    url = f"https://data.sec.gov/api/xbrl/companyconcept/CIK{int(cik):010d}/dei/EntityCommonStockSharesOutstanding.json"
    try:
        facts = json.loads(client.get(url)).get("units", {}).get("shares", [])
    except EdgarNotFound:
        return None
    if not facts:
        return None
    # 最新の時点の値。種類株が複数ある場合は同じ日付の値を合計する
    latest = max(f["end"] for f in facts)
    by_filing = {}
    for f in facts:
        if f["end"] == latest:
            by_filing.setdefault(f["accn"], []).append(f["val"])
    return float(max(sum(v) for v in by_filing.values()))


def read_delisting(client: EdgarClient, e: IndexEntry, day: date, log) -> CashMerger | None:
    raw = client.get(e.url, max_bytes=60_000).decode("latin-1", errors="ignore")
    cik, name = subject_company(raw)
    if not cik:
        return None
    # 社債やワラントだけの上場廃止は対象外
    body = BeautifulSoup(raw, "lxml").get_text(" ")
    if not EQUITY_WORDS.search(body):
        return None
    rec = CashMerger(filed=e.date_filed, accession=e.accession, target_cik=cik, target=name or e.company, status="")

    filings = _recent_filings(client, cik)
    since = (day - timedelta(days=540)).isoformat()
    if not any(f["form"] in MERGER_FORMS and f["filingDate"] >= since for f in filings):
        rec.status = "not_merger"
        return rec

    window = ((day - timedelta(days=7)).isoformat(), (day + timedelta(days=3)).isoformat())
    eightks = [f for f in filings if f["form"] == "8-K" and window[0] <= f["filingDate"] <= window[1]]
    if not eightks:
        rec.status = "merger_no_8k_yet"
        return rec
    if not llm.available():
        rec.status = "skipped_no_api_key"
        return rec

    f = sorted(eightks, key=lambda x: x["filingDate"])[-1]
    acc = f["accessionNumber"].replace("-", "")
    rec.source_url = f"https://www.sec.gov/Archives/edgar/data/{int(cik)}/{acc}/{f['primaryDocument']}"
    html = client.get(rec.source_url, max_bytes=400_000).decode("utf-8", errors="ignore")
    text = re.sub(r"\s+", " ", BeautifulSoup(html, "lxml").get_text(" "))[:14000]
    data = llm.ask_json(_PROMPT + text)

    if not data.get("is_acquisition_completed"):
        rec.status = "not_completion"
        return rec
    rec.acquirer = data.get("acquirer")
    rec.stock_consideration = data.get("stock_consideration")
    rec.confidence = data.get("confidence")
    try:
        rec.cash_per_share_usd = float(str(data.get("cash_per_share_usd")).replace("$", "").replace(",", "")) if data.get("cash_per_share_usd") is not None else None
    except ValueError:
        rec.cash_per_share_usd = None
    rec.shares_outstanding = _shares_outstanding(client, cik)
    if rec.cash_per_share_usd and rec.shares_outstanding:
        rec.cash_usd = round(rec.cash_per_share_usd * rec.shares_outstanding, 2)
        rec.status = "ok"
    else:
        rec.status = "ok_stock_only" if rec.cash_per_share_usd is None else "ok_missing_shares"
    return rec


def collect(client: EdgarClient, entries: list[IndexEntry], day: date, log) -> tuple[list[dict], dict]:
    rows: list[dict] = []
    stats = {"delistings": 0, "cash_mergers": 0, "merger_errors": 0}
    seen: set[str] = set()
    for e in entries:
        if e.form not in DELISTING_FORMS:
            continue
        stats["delistings"] += 1
        try:
            rec = read_delisting(client, e, day, log)
        except Exception as exc:  # 1件の失敗で全体を止めない
            stats["merger_errors"] += 1
            log(f"上場廃止届の処理に失敗: {e.url} ({type(exc).__name__}: {exc})")
            continue
        if rec is None or rec.target_cik in seen:
            continue
        seen.add(rec.target_cik)  # 同じ会社の複数の届出（株式の種類ごとなど）は1回だけ数える
        if rec.status == "ok":
            stats["cash_mergers"] += 1
        rows.append(asdict(rec))
    return rows, stats

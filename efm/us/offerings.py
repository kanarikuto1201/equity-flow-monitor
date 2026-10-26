"""IPO・公募増資・売出し（424B4 / 424B5 / 424B7）と、上場予定（S-1 / F-1）を読み取る。

- 目論見書の先頭（表紙と「The Offering」の要約）だけを読み、Claude APIで価格・株数を抽出する
- 株式の募集らしくない書類（社債など）はClaudeに渡す前に除外して費用を抑える
- ATMは「枠」の設定なので、需給には入れず予定として記録する
- 仕組債が大半の424B2と、転売登録の424B3は使わない（設計書の3章）
"""
from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass, asdict

from bs4 import BeautifulSoup

from .edgar import EdgarClient, IndexEntry, EdgarNotFound

PROSPECTUS_FORMS = {"424B4", "424B5", "424B7"}
REGISTRATION_FORMS = {"S-1", "F-1"}  # 新規の上場予定（訂正の S-1/A などは除く）

_EQUITY_HINT = re.compile(
    r"(shares of (?:our |its )?(?:class [a-z] )?(?:common stock|ordinary shares)"
    r"|ordinary shares|american depositary shares|\bADSs\b)",
    re.I,
)

_PROMPT = """You read the beginning of a U.S. SEC prospectus and extract facts about the offering.
Reply with one JSON object only, no other text. Use null when the document does not state a value.

Fields:
- "offering_type": one of "ipo", "follow_on", "atm", "registered_direct", "rights", "resale", "debt", "spac_ipo", "other".
  "follow_on" = an underwritten or bought offering of shares by a company that is already listed.
  "atm" = an at-the-market program (sales agreement, shares sold over time).
  "resale" = selling stockholders registering shares for resale without an underwritten offering.
- "ticker": exchange ticker of the security, or null
- "exchange": e.g. "Nasdaq", "NYSE", or null
- "security": short description such as "Class A common stock" or "ADSs"
- "price_per_share_usd": public offering price per share (or per ADS) in USD
- "primary_shares": number of new shares the company sells (excluding the underwriters' option)
- "secondary_shares": number of existing shares sold by selling stockholders (excluding the option)
- "option_shares": number of shares in the underwriters' over-allotment option
- "atm_program_usd": maximum aggregate amount of an ATM program, for "atm" only
- "lockup_days": lock-up period in days for directors, officers and existing holders, if stated
- "is_spac": true if the issuer is a blank-check / special purpose acquisition company
- "confidence": "high", "medium" or "low"

Prospectus text:
"""


@dataclass
class Offering:
    filed: str
    form: str
    accession: str
    cik: str
    company: str
    url: str
    offering_type: str | None = None
    ticker: str | None = None
    exchange: str | None = None
    security: str | None = None
    price_per_share_usd: float | None = None
    primary_shares: float | None = None
    secondary_shares: float | None = None
    option_shares: float | None = None
    atm_program_usd: float | None = None
    lockup_days: float | None = None
    is_spac: bool | None = None
    confidence: str | None = None
    primary_usd: float | None = None
    secondary_usd: float | None = None
    preliminary: bool = False
    status: str = "ok"


def prospectus_text(raw: bytes, limit: int = 14000) -> str:
    """提出書類の先頭から本文テキストを取り出し、表紙と「The Offering」付近を残す。"""
    html = raw.decode("utf-8", errors="ignore")
    # 提出書類（.txt）の最初の<DOCUMENT>が目論見書本体
    m = re.search(r"<DOCUMENT>(.*?)(?:</DOCUMENT>|$)", html, re.S | re.I)
    body = m.group(1) if m else html
    text = BeautifulSoup(body, "lxml").get_text(" ")
    text = re.sub(r"\s+", " ", text).strip()
    head = text[: limit // 2]
    idx = text.find("The Offering", limit // 4)
    if idx == -1:
        idx = text.find("THE OFFERING", limit // 4)
    tail = text[idx : idx + limit // 2] if idx != -1 else text[limit // 2 : limit]
    return head + "\n...\n" + tail


def _claude_extract(text: str) -> dict:
    import anthropic

    client = anthropic.Anthropic()
    model = os.environ.get("CLAUDE_MODEL", "claude-haiku-5-5")
    msg = client.messages.create(
        model=model,
        max_tokens=600,
        messages=[{"role": "user", "content": _PROMPT + text}],
    )
    out = "".join(block.text for block in msg.content if getattr(block, "type", "") == "text")
    m = re.search(r"\{.*\}", out, re.S)
    if not m:
        raise ValueError("Claudeの返答からJSONを読み取れませんでした")
    return json.loads(m.group(0))


def _to_float(v) -> float | None:
    if v is None:
        return None
    try:
        return float(str(v).replace(",", "").replace("$", ""))
    except ValueError:
        return None


def read_offering(client: EdgarClient, e: IndexEntry, use_claude: bool) -> Offering:
    off = Offering(filed=e.date_filed, form=e.form, accession=e.accession, cik=e.cik, company=e.company, url=e.url)
    raw = client.get(e.url, max_bytes=600_000)
    text = prospectus_text(raw)
    if not _EQUITY_HINT.search(text[:20000]):
        off.offering_type, off.status = "debt_or_other", "skipped_not_equity"
        return off
    # 価格が決まる前の仮目論見書は、確定版と二重に数えないよう金額を計上しない
    if re.search(r"subject to completion|preliminary prospectus", text[:6000], re.I):
        off.preliminary = True
        off.status = "skipped_preliminary"
        return off
    if not use_claude:
        off.status = "skipped_no_api_key"
        return off
    data = _claude_extract(text)
    for key in ("offering_type", "ticker", "exchange", "security", "confidence"):
        setattr(off, key, data.get(key))
    for key in ("price_per_share_usd", "primary_shares", "secondary_shares", "option_shares", "atm_program_usd", "lockup_days"):
        setattr(off, key, _to_float(data.get(key)))
    off.is_spac = bool(data.get("is_spac")) or off.offering_type == "spac_ipo"
    if off.price_per_share_usd:
        if off.primary_shares:
            off.primary_usd = round(off.price_per_share_usd * off.primary_shares, 2)
        if off.secondary_shares:
            off.secondary_usd = round(off.price_per_share_usd * off.secondary_shares, 2)
    return off


def collect(client: EdgarClient, entries: list[IndexEntry], log) -> tuple[list[dict], list[dict], dict]:
    use_claude = bool(os.environ.get("ANTHROPIC_API_KEY"))
    if not use_claude:
        log("ANTHROPIC_API_KEY が未設定のため、目論見書の金額読み取りを省略します")
    offerings: list[dict] = []
    pipeline: list[dict] = []
    stats = {"prospectuses": 0, "prospectus_errors": 0, "claude_calls": 0, "registrations": 0}

    for e in entries:
        if e.form in REGISTRATION_FORMS:
            stats["registrations"] += 1
            pipeline.append({"filed": e.date_filed, "form": e.form, "cik": e.cik, "company": e.company, "url": e.url})
        elif e.form in PROSPECTUS_FORMS:
            stats["prospectuses"] += 1
            try:
                off = read_offering(client, e, use_claude)
                if off.status == "ok":
                    stats["claude_calls"] += 1
                offerings.append(asdict(off))
            except (EdgarNotFound, RuntimeError, ValueError, json.JSONDecodeError) as exc:
                stats["prospectus_errors"] += 1
                log(f"目論見書の読み取りに失敗: {e.url} ({exc})")
                offerings.append(asdict(Offering(e.date_filed, e.form, e.accession, e.cik, e.company, e.url, status=f"error: {exc}"[:200])))
            except Exception as exc:  # Claude APIの一時的な障害などで全体を止めない
                stats["prospectus_errors"] += 1
                log(f"目論見書の処理で想定外のエラー: {e.url} ({type(exc).__name__}: {exc})")
                offerings.append(asdict(Offering(e.date_filed, e.form, e.accession, e.cik, e.company, e.url, status=f"error: {type(exc).__name__}")))
    return offerings, pipeline, stats

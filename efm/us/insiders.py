"""内部者の売買（Form 4）と売却予定（Form 144）を読み取る。

計上ルール（設計書の4章）:
- Form 4は取引コードS（市場売却）とP（市場買付）だけを使う
- 税金のための差し引き（F）、オプション行使（M）、贈与（G）などは市場売買ではないので除く
- Form 144は「予告」なので需給には入れず、別の系列として集計する
- 訂正書類（4/A、144/A）は二重計上を避けるため使わない
"""
from __future__ import annotations

import xml.etree.ElementTree as ET
from dataclasses import dataclass, asdict

from .edgar import EdgarClient, IndexEntry, extract_xml, EdgarNotFound


def _local(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


def _find(elem: ET.Element, *path: str) -> ET.Element | None:
    """名前空間を無視して子要素をたどる。"""
    cur = elem
    for name in path:
        nxt = None
        for child in cur:
            if _local(child.tag) == name:
                nxt = child
                break
        if nxt is None:
            return None
        cur = nxt
    return cur


def _text(elem: ET.Element | None, *path: str) -> str:
    if elem is None:
        return ""
    node = _find(elem, *path) if path else elem
    if node is None:
        return ""
    # Form 4では <transactionShares><value>100</value></transactionShares> の形が多い
    val = _find(node, "value")
    raw = (val.text if val is not None else node.text) or ""
    return raw.strip()


def _num(s: str) -> float | None:
    try:
        return float(s.replace(",", "").replace("$", ""))
    except (ValueError, AttributeError):
        return None


@dataclass
class InsiderTrade:
    filed: str
    accession: str
    issuer_cik: str
    ticker: str
    issuer: str
    owner: str
    is_officer: bool
    is_director: bool
    is_ten_pct: bool
    trade_date: str
    code: str  # S or P
    shares: float
    price: float
    value_usd: float
    plan_10b5_1: bool


def parse_form4(xml: bytes, entry: IndexEntry) -> list[InsiderTrade]:
    root = ET.fromstring(xml)
    issuer = _find(root, "issuer")
    issuer_cik = _text(issuer, "issuerCik")
    ticker = _text(issuer, "issuerTradingSymbol").upper()
    issuer_name = _text(issuer, "issuerName")

    owners, officer, director, ten_pct = [], False, False, False
    for child in root:
        if _local(child.tag) != "reportingOwner":
            continue
        owners.append(_text(child, "reportingOwnerId", "rptOwnerName"))
        rel = _find(child, "reportingOwnerRelationship")
        if rel is not None:
            officer |= _text(rel, "isOfficer") in ("1", "true")
            director |= _text(rel, "isDirector") in ("1", "true")
            ten_pct |= _text(rel, "isTenPercentOwner") in ("1", "true")
    plan = _text(root, "aff10b5One") in ("1", "true")

    trades: list[InsiderTrade] = []
    table = _find(root, "nonDerivativeTable")
    if table is None:
        return trades
    for tx in table:
        if _local(tx.tag) != "nonDerivativeTransaction":
            continue
        code = _text(tx, "transactionCoding", "transactionCode").upper()
        if code not in ("S", "P"):
            continue
        shares = _num(_text(tx, "transactionAmounts", "transactionShares"))
        price = _num(_text(tx, "transactionAmounts", "transactionPricePerShare"))
        if not shares or not price:
            continue
        trades.append(
            InsiderTrade(
                filed=entry.date_filed,
                accession=entry.accession,
                issuer_cik=issuer_cik,
                ticker=ticker,
                issuer=issuer_name,
                owner="; ".join(o for o in owners if o),
                is_officer=officer,
                is_director=director,
                is_ten_pct=ten_pct,
                trade_date=_text(tx, "transactionDate"),
                code=code,
                shares=shares,
                price=price,
                value_usd=round(shares * price, 2),
                plan_10b5_1=plan,
            )
        )
    return trades


@dataclass
class Form144Notice:
    filed: str
    accession: str
    issuer_cik: str
    issuer: str
    seller: str
    relationship: str
    shares: float
    market_value_usd: float
    approx_sale_date: str


def _iter_named(root: ET.Element, name: str):
    for el in root.iter():
        if _local(el.tag) == name:
            yield el


def parse_form144(xml: bytes, entry: IndexEntry) -> list[Form144Notice]:
    root = ET.fromstring(xml)
    info = next(_iter_named(root, "issuerInfo"), None)
    issuer_cik = _text(info, "issuerCik") if info is not None else entry.cik
    issuer = _text(info, "issuerName") if info is not None else entry.company
    seller = _text(info, "nameOfPersonForWhoseAccountTheSecuritiesAreToBeSold") if info is not None else ""
    relationship = ""
    if info is not None:
        rel = next(_iter_named(info, "relationshipToIssuer"), None)
        relationship = (rel.text or "").strip() if rel is not None else ""

    notices = []
    for sec in _iter_named(root, "securitiesInformation"):
        value = _num(_text(sec, "aggregateMarketValue"))
        shares = _num(_text(sec, "noOfUnitsSold"))
        if not value:
            continue
        notices.append(
            Form144Notice(
                filed=entry.date_filed,
                accession=entry.accession,
                issuer_cik=issuer_cik,
                issuer=issuer,
                seller=seller,
                relationship=relationship,
                shares=shares or 0.0,
                market_value_usd=value,
                approx_sale_date=_text(sec, "approxSaleDate"),
            )
        )
    return notices


def collect(client: EdgarClient, entries: list[IndexEntry], log) -> tuple[list[dict], list[dict], dict]:
    """その日のForm 4とForm 144をすべて読み、取引の一覧と処理状況を返す。"""
    trades: list[dict] = []
    notices: list[dict] = []
    stats = {"form4_filings": 0, "form4_errors": 0, "form144_filings": 0, "form144_errors": 0}

    for e in entries:
        if e.form == "4":
            stats["form4_filings"] += 1
            try:
                xml = extract_xml(client.get(e.url))
                if xml:
                    trades.extend(asdict(t) for t in parse_form4(xml, e))
            except (ET.ParseError, EdgarNotFound, RuntimeError) as exc:
                stats["form4_errors"] += 1
                log(f"Form 4の読み取りに失敗: {e.url} ({exc})")
        elif e.form == "144":
            stats["form144_filings"] += 1
            try:
                xml = extract_xml(client.get(e.url))
                if xml:
                    notices.extend(asdict(n) for n in parse_form144(xml, e))
            except (ET.ParseError, EdgarNotFound, RuntimeError) as exc:
                stats["form144_errors"] += 1
                log(f"Form 144の読み取りに失敗: {e.url} ({exc})")
    return trades, notices, stats

"""XBRL（財務諸表の数値データ）から、企業ごとの四半期の資金の流れを取り出し、日次に配分する。

設計書の4章「推計ロジック」の共通部分。対象は次の4つ:

| 名前            | 需給での向き | 使う項目（キャッシュフロー計算書など）                                   |
| buyback         | 需要        | 自社株買いの支出                                                          |
| sbc             | 供給        | 株式報酬費用 − 源泉徴収のために会社が現金で払った分（社員に渡った株の推計） |
| stock_issuance  | 供給        | 新株発行の収入 − ストックオプション行使の収入（ATM・私募などを含む）       |
| conversion      | 供給        | 社債から株に転換された元本                                                 |

考え方:
- キャッシュフロー計算書は「期首からの累計」で報告されるため、累計の差から四半期の値を出す
- 報告済みの四半期は実績を、まだ報告されていない四半期は直近4四半期の平均を使う
- 四半期の金額を営業日に配分する。自社株買いだけは、決算前の自粛期間の重みを下げる
- 対象企業は、年次集計（frames API）で年間の金額が一定以上の企業。週に1回選び直し、
  毎日の処理では、その日に10-Q / 10-K を出した企業だけを更新する
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass
from datetime import date, timedelta

import numpy as np
import pandas as pd

from .edgar import EdgarClient, EdgarNotFound

BLACKOUT_BEFORE_DAYS = 14
BLACKOUT_WEIGHT = 0.25  # 自粛期間中の自社株買いのペース（通常日を1とした比率）
PROFILE_MAX_AGE_DAYS = 7


@dataclass(frozen=True)
class Flow:
    name: str
    label: str
    tags: tuple[str, ...]  # 主となる項目（複数あれば、新しい四半期まで報告されている方）
    minus_tags: tuple[str, ...] = ()  # 差し引く項目
    min_annual_usd: float = 5_000_000
    blackout: bool = False


FLOWS = {
    f.name: f
    for f in (
        Flow("buyback", "自社株買い", ("PaymentsForRepurchaseOfCommonStock", "PaymentsForRepurchaseOfEquity"), blackout=True),
        Flow("sbc", "社員への株式報酬", ("ShareBasedCompensation", "AllocatedShareBasedCompensationExpense"),
             minus_tags=("PaymentsRelatedToTaxWithholdingForShareBasedCompensation",)),
        Flow("stock_issuance", "新株発行（ATM・私募など）", ("ProceedsFromIssuanceOfCommonStock",),
             minus_tags=("ProceedsFromStockOptionsExercised",)),
        Flow("conversion", "社債の株式転換", ("DebtConversionOriginalDebtAmount1",), min_annual_usd=1_000_000),
    )
}


def _cik10(cik) -> str:
    return f"{int(cik):010d}"


def quarterly_from_facts(facts: list[dict]) -> list[dict]:
    """累計値（期首から）の並びを、四半期ごとの値に直す。

    facts: companyconcept APIの units.USD の要素（start, end, val, filed, form）
    戻り値: [{start, end, usd, filed}]  start/end は四半期の期間
    """
    rows: dict = {}
    for f in facts:
        if not f.get("start") or f.get("form") not in ("10-Q", "10-K", "10-Q/A", "10-K/A", "20-F", "40-F"):
            continue
        key = (f["start"], f["end"])
        prev = rows.get(key)
        if prev is None:
            rows[key] = {"start": f["start"], "end": f["end"], "val": f["val"], "filed": f["filed"]}
        else:
            # 同じ期間の値が複数の書類にある場合は、最初に報告された日付と最新の値を使う
            prev["val"] = f["val"] if f["filed"] >= prev["filed"] else prev["val"]
            prev["filed"] = min(prev["filed"], f["filed"])

    by_start: dict[str, list[dict]] = {}
    for r in rows.values():
        by_start.setdefault(r["start"], []).append(r)

    quarters: dict[str, dict] = {}
    for start, items in by_start.items():
        items.sort(key=lambda r: r["end"])
        prev_end, prev_val = None, 0.0
        for r in items:
            span = (date.fromisoformat(r["end"]) - date.fromisoformat(start)).days
            q_start = start if prev_end is None else (date.fromisoformat(prev_end) + timedelta(days=1)).isoformat()
            q_days = (date.fromisoformat(r["end"]) - date.fromisoformat(q_start)).days
            if 75 <= q_days <= 100 and span <= 380:
                usd = r["val"] - prev_val
                if usd >= 0 and r["end"] not in quarters:
                    quarters[r["end"]] = {"start": q_start, "end": r["end"], "usd": float(usd), "filed": r["filed"]}
            prev_end, prev_val = r["end"], r["val"]
    return sorted(quarters.values(), key=lambda q: q["end"])


class FlowStore:
    """1つの流れ（例：自社株買い）の四半期データの保存場所。"""

    def __init__(self, store, flow: Flow):
        self.flow = flow
        self.path = store.base / "ref" / f"{flow.name}_quarters.csv.gz"
        self.meta_path = store.base / "ref" / f"{flow.name}_meta.json"

    def load(self) -> pd.DataFrame:
        return pd.read_csv(self.path, dtype={"cik": str}) if self.path.exists() else pd.DataFrame()

    def meta(self) -> dict:
        return json.loads(self.meta_path.read_text()) if self.meta_path.exists() else {}

    def is_stale(self) -> bool:
        refreshed = self.meta().get("refreshed")
        return not refreshed or (date.today() - date.fromisoformat(refreshed)).days >= PROFILE_MAX_AGE_DAYS


def candidate_ciks(client: EdgarClient, flow: Flow, years: list[int], log) -> dict[str, str]:
    out: dict[str, str] = {}
    for tag in flow.tags:
        for y in years:
            url = f"https://data.sec.gov/api/xbrl/frames/us-gaap/{tag}/USD/CY{y}.json"
            try:
                data = json.loads(client.get(url))
            except EdgarNotFound:
                continue
            for row in data.get("data", []):
                if row.get("val", 0) >= flow.min_annual_usd:
                    out[str(int(row["cik"]))] = row.get("entityName", "")
    log(f"{flow.label}の対象企業: {len(out):,} 社")
    return out


def _concept(client: EdgarClient, cik: str, tag: str) -> list[dict]:
    url = f"https://data.sec.gov/api/xbrl/companyconcept/CIK{_cik10(cik)}/us-gaap/{tag}.json"
    try:
        data = json.loads(client.get(url))
    except EdgarNotFound:
        return []
    return quarterly_from_facts(data.get("units", {}).get("USD", []))


def fetch_company(client: EdgarClient, flow: Flow, cik: str) -> list[dict]:
    best: list[dict] = []
    for tag in flow.tags:
        q = _concept(client, cik, tag)
        if q and (not best or q[-1]["end"] > best[-1]["end"]):
            best = q
    if not best or not flow.minus_tags:
        return best
    minus: dict[str, float] = {}
    for tag in flow.minus_tags:
        for q in _concept(client, cik, tag):
            minus[q["end"]] = minus.get(q["end"], 0.0) + q["usd"]
    for q in best:
        q["usd"] = max(0.0, q["usd"] - minus.get(q["end"], 0.0))
    return best


def refresh(client: EdgarClient, store, flow: Flow, log, only_ciks: set[str] | None = None) -> pd.DataFrame:
    """四半期データを更新して保存する。only_ciks を渡すと、その会社だけを更新する。"""
    fs = FlowStore(store, flow)
    fs.path.parent.mkdir(parents=True, exist_ok=True)
    old = fs.load()
    meta = fs.meta()

    if only_ciks is None:
        this_year = date.today().year
        names = candidate_ciks(client, flow, [this_year - 2, this_year - 1], log)
        targets = set(names)
        meta = {"refreshed": date.today().isoformat(), "names": names}
    else:
        names = meta.get("names", {})
        targets = only_ciks & set(names)
        if not targets:
            return old

    rows = []
    for i, cik in enumerate(sorted(targets)):
        try:
            for q in fetch_company(client, flow, cik):
                rows.append({"cik": cik, "company": names.get(cik, ""), **q})
        except Exception as exc:  # 1社の失敗で全体を止めない
            log(f"{flow.label}の取得に失敗: CIK {cik} ({type(exc).__name__}: {exc})")
        if (i + 1) % 500 == 0:
            log(f"{flow.label}: {i + 1:,} / {len(targets):,} 社を取得")

    new = pd.DataFrame(rows)
    if not old.empty and only_ciks is not None:
        new = pd.concat([old[~old["cik"].isin(targets)], new], ignore_index=True)
    new.to_csv(fs.path, index=False, compression="gzip")
    fs.meta_path.write_text(json.dumps(meta, ensure_ascii=False), encoding="utf-8")
    log(f"{flow.label}の四半期データ: {new['cik'].nunique() if not new.empty else 0:,} 社、{len(new):,} 四半期")
    return new


FUND_NAME = re.compile(r"\b(?:ETF|ETFS|FUND|FUNDS|PROSHARES|ISHARES|SPDR|INVESCO DB|GRAYSCALE|COMMODITY|BITCOIN|ETHER|ETHEREUM|"
                       r"FEDERAL HOME LOAN BANK|TRUST II|TRUST III)\b", re.I)


def refresh_listed(client: EdgarClient, store, log) -> None:
    """取引所に上場している会社のCIK一覧を保存する（非上場の発行体やファンドを集計から除くため）。"""
    data = json.loads(client.get("https://www.sec.gov/files/company_tickers_exchange.json"))
    fields = data.get("fields", [])
    rows = [dict(zip(fields, r)) for r in data.get("data", [])]
    listed = {str(int(r["cik"])): r.get("name", "") for r in rows
              if r.get("exchange") in ("Nasdaq", "NYSE", "NYSE American", "NYSE Arca", "CBOE", "Cboe BZX")}
    path = store.base / "ref" / "listed.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"refreshed": date.today().isoformat(), "listed": listed}, ensure_ascii=False), encoding="utf-8")
    log(f"上場会社の一覧: {len(listed):,} 社")


def listed_ciks(store) -> set[str] | None:
    path = store.base / "ref" / "listed.json"
    if not path.exists():
        return None
    return set(json.loads(path.read_text(encoding="utf-8"))["listed"])


def operating_companies(q: pd.DataFrame, store) -> pd.DataFrame:
    """上場している事業会社だけに絞る（ETF・ファンド・連邦住宅貸付銀行・非上場REITなどを除く）。"""
    if q.empty:
        return q
    listed = listed_ciks(store)
    keep = ~q["company"].fillna("").astype(str).str.contains(FUND_NAME)
    if listed is not None:
        keep &= q["cik"].astype(str).isin(listed)
    return q[keep]


def refresh_all(client: EdgarClient, store, log, reporters: set[str]) -> None:
    try:
        refresh_listed(client, store, log)
    except Exception as exc:
        log(f"上場会社の一覧の取得に失敗 ({type(exc).__name__}: {exc})")
    for flow in FLOWS.values():
        fs = FlowStore(store, flow)
        if fs.is_stale():
            log(f"{flow.label}：対象企業を選び直し、全社の四半期データを更新します")
            refresh(client, store, flow, log)
        elif reporters:
            refresh(client, store, flow, log, only_ciks=reporters)


def daily_estimate(quarters: pd.DataFrame, until: date, blackout: bool = False, since: date | None = None) -> pd.DataFrame:
    """企業ごとの四半期の値を日次に配分し、全社合計の日次系列を返す。

    columns: reported（報告済み四半期の配分）, estimated（未報告期間の推計）。index は YYYYMMDD
    """
    if quarters.empty:
        return pd.DataFrame({"reported": pd.Series(dtype=float), "estimated": pd.Series(dtype=float)})
    q = quarters.copy()
    q["start_d"] = pd.to_datetime(q["start"])
    q["end_d"] = pd.to_datetime(q["end"])
    q["filed_d"] = pd.to_datetime(q["filed"])
    q = q.sort_values(["cik", "end_d"])
    q["lag"] = (q["filed_d"] - q["end_d"]).dt.days.clip(lower=0)
    # 期首の自粛期間は、前四半期の決算発表の翌日まで
    q["prev_lag"] = q.groupby("cik")["lag"].shift(1).fillna(35)
    q["kind"] = "reported"

    # 未報告の期間：直近4四半期の平均を、91日ごとの四半期として until まで延ばす
    future = []
    for cik, g in q.groupby("cik", sort=False):
        pace = g["usd"].tail(4).mean()
        lag = float(g["lag"].tail(4).median())
        last_end, prev_lag = g["end_d"].iloc[-1], float(g["lag"].iloc[-1])
        while last_end < pd.Timestamp(until):
            s = last_end + pd.Timedelta(days=1)
            e = s + pd.Timedelta(days=90)
            future.append((cik, s, e, pace, prev_lag))
            last_end, prev_lag = e, lag
    if future:
        f = pd.DataFrame(future, columns=["cik", "start_d", "end_d", "usd", "prev_lag"])
        f["kind"] = "estimated"
        q = pd.concat([q, f], ignore_index=True)

    lo = q["start_d"].min()
    if since is not None:
        lo = max(lo, pd.Timestamp(since) - pd.Timedelta(days=120))
        q = q[q["end_d"] >= lo]
    days = pd.bdate_range(lo, max(q["end_d"].max(), pd.Timestamp(until)))
    out = {"reported": np.zeros(len(days)), "estimated": np.zeros(len(days))}
    pos = days.values

    for kind, start, end, usd, prev_lag in q[["kind", "start_d", "end_d", "usd", "prev_lag"]].itertuples(index=False):
        a = np.searchsorted(pos, np.datetime64(start))
        b = np.searchsorted(pos, np.datetime64(end), side="right")
        if b <= a or not usd:
            continue
        w = np.ones(b - a)
        if blackout:
            seg = pos[a:b]
            early = seg <= np.datetime64(start + pd.Timedelta(days=int(prev_lag)))
            late = seg >= np.datetime64(end - pd.Timedelta(days=BLACKOUT_BEFORE_DAYS))
            w[early | late] = BLACKOUT_WEIGHT
        out[kind][a:b] += usd * w / w.sum()

    df = pd.DataFrame(out, index=days)
    df = df[df.index <= pd.Timestamp(until)]
    if since is not None:
        df = df[df.index >= pd.Timestamp(since)]
    df.index = df.index.strftime("%Y%m%d")
    return df


def subtract_captured(quarters: pd.DataFrame, captured: pd.DataFrame) -> pd.DataFrame:
    """新株発行の収入から、目論見書で既に数えた分（IPO・公募など）を差し引く。

    captured: columns cik, day(YYYYMMDD), usd
    """
    if quarters.empty or captured is None or captured.empty:
        return quarters
    q = quarters.copy().reset_index(drop=True)
    c = captured.copy()
    c["cik"] = c["cik"].astype(str).str.lstrip("0")
    c["d"] = pd.to_datetime(c["day"].astype(str), format="%Y%m%d", errors="coerce")
    m = q.reset_index().merge(c, on="cik", suffixes=("", "_c"))
    m = m[(m["d"] >= pd.to_datetime(m["start"])) & (m["d"] <= pd.to_datetime(m["end"]))]
    sub = m.groupby("index")["usd_c"].sum()
    q.loc[sub.index, "usd"] = (q.loc[sub.index, "usd"] - sub).clip(lower=0)
    return q


def coverage(quarters: pd.DataFrame) -> dict:
    if quarters.empty:
        return {}
    return {"companies": int(quarters["cik"].nunique()), "latest_end": str(quarters["end"].max())}

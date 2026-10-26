"""自社株買いの推計（設計書の4章）。

考え方:
- 企業ごとの四半期の買付額（実績）を、SECのXBRLデータから復元する
  キャッシュフロー計算書は「期首からの累計」で報告されるため、累計の差から四半期の値を出す
- 報告済みの四半期は実績を、まだ報告されていない四半期は直近4四半期の平均（消化ペース）を使う
- 四半期の金額を、その期間の営業日に配分する。決算前の自粛期間（期末14日前〜決算発表の翌日）は
  買付が大きく減るとみて重みを下げる（10b5-1プランで自粛期間中も買う企業があるため0にはしない）
- 決算発表日は、10-Q / 10-K の提出日で代用する

対象企業は、XBRLの年次集計（frames API）で年間の買付額が一定以上の企業。週に1回選び直し、
毎日の処理では、その日に10-Q / 10-K を出した企業だけを更新する。
"""
from __future__ import annotations

import json
from datetime import date, datetime, timedelta

import pandas as pd

from .edgar import EdgarClient, EdgarNotFound

TAGS = ("PaymentsForRepurchaseOfCommonStock", "PaymentsForRepurchaseOfEquity")
MIN_ANNUAL_USD = 5_000_000  # 年間買付額がこれ未満の企業は対象外（全体への影響が小さいため）
BLACKOUT_BEFORE_DAYS = 14
BLACKOUT_WEIGHT = 0.25  # 自粛期間中の買付ペース（通常日を1とした比率）
PROFILE_MAX_AGE_DAYS = 7


def _cik10(cik) -> str:
    return f"{int(cik):010d}"


def candidate_ciks(client: EdgarClient, years: list[int], log) -> dict[str, str]:
    """年次集計から、年間の買付額が一定以上の企業を選ぶ。{cik: 会社名}"""
    out: dict[str, str] = {}
    for tag in TAGS:
        for y in years:
            url = f"https://data.sec.gov/api/xbrl/frames/us-gaap/{tag}/USD/CY{y}.json"
            try:
                data = json.loads(client.get(url))
            except EdgarNotFound:
                continue
            for row in data.get("data", []):
                if row.get("val", 0) >= MIN_ANNUAL_USD:
                    out[str(int(row["cik"]))] = row.get("entityName", "")
    log(f"自社株買いの対象企業: {len(out):,} 社")
    return out


def quarterly_from_facts(facts: list[dict]) -> list[dict]:
    """累計値（期首から）の並びを、四半期ごとの値に直す。

    facts: companyconcept APIの units.USD の要素（start, end, val, filed, form）
    戻り値: [{start, end, usd, filed}]  start/end は四半期の期間
    """
    rows = {}
    for f in facts:
        if not f.get("start") or f.get("form") not in ("10-Q", "10-K", "10-Q/A", "10-K/A", "20-F", "40-F"):
            continue
        key = (f["start"], f["end"])
        # 同じ期間の値が複数の書類にある場合は、最初に報告された日付と最新の値を使う
        prev = rows.get(key)
        if prev is None:
            rows[key] = {"start": f["start"], "end": f["end"], "val": f["val"], "filed": f["filed"]}
        else:
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
            if prev_end is None:
                q_start = start
            else:
                q_start = (date.fromisoformat(prev_end) + timedelta(days=1)).isoformat()
            q_days = (date.fromisoformat(r["end"]) - date.fromisoformat(q_start)).days
            if 75 <= q_days <= 100 and span <= 380:
                usd = r["val"] - prev_val
                # 3か月分の値が直接報告されている場合も、累計の差と同じ結果になる
                if usd >= 0 and r["end"] not in quarters:
                    quarters[r["end"]] = {"start": q_start, "end": r["end"], "usd": float(usd), "filed": r["filed"]}
            prev_end, prev_val = r["end"], r["val"]
    return sorted(quarters.values(), key=lambda q: q["end"])


def fetch_company(client: EdgarClient, cik: str) -> list[dict]:
    best: list[dict] = []
    for tag in TAGS:
        url = f"https://data.sec.gov/api/xbrl/companyconcept/CIK{_cik10(cik)}/us-gaap/{tag}.json"
        try:
            data = json.loads(client.get(url))
        except EdgarNotFound:
            continue
        q = quarterly_from_facts(data.get("units", {}).get("USD", []))
        # 2つのタグのうち、新しい四半期まで報告されている方を使う
        if q and (not best or q[-1]["end"] > best[-1]["end"]):
            best = q
    return best


def refresh_profiles(client: EdgarClient, store, log, only_ciks: set[str] | None = None) -> pd.DataFrame:
    """企業ごとの四半期実績（profiles）を更新して保存する。"""
    path = store.base / "ref" / "buyback_quarters.csv.gz"
    meta_path = store.base / "ref" / "buyback_meta.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    old = pd.read_csv(path, dtype={"cik": str}) if path.exists() else pd.DataFrame()
    meta = json.loads(meta_path.read_text()) if meta_path.exists() else {}

    if only_ciks is None:
        this_year = date.today().year
        names = candidate_ciks(client, [this_year - 2, this_year - 1], log)
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
            for q in fetch_company(client, cik):
                rows.append({"cik": cik, "company": names.get(cik, ""), **q})
        except Exception as exc:  # 1社の失敗で全体を止めない
            log(f"自社株買いの取得に失敗: CIK {cik} ({type(exc).__name__}: {exc})")
        if (i + 1) % 500 == 0:
            log(f"自社株買い: {i + 1:,} / {len(targets):,} 社を取得")

    new = pd.DataFrame(rows)
    if not old.empty and only_ciks is not None:
        old = old[~old["cik"].isin(targets)]
        new = pd.concat([old, new], ignore_index=True)
    new.to_csv(path, index=False, compression="gzip")
    meta_path.write_text(json.dumps(meta, ensure_ascii=False), encoding="utf-8")
    log(f"自社株買いの四半期データ: {new['cik'].nunique() if not new.empty else 0:,} 社、{len(new):,} 四半期")
    return new


def profiles_are_stale(store) -> bool:
    meta_path = store.base / "ref" / "buyback_meta.json"
    if not meta_path.exists():
        return True
    refreshed = json.loads(meta_path.read_text()).get("refreshed")
    return not refreshed or (date.today() - date.fromisoformat(refreshed)).days >= PROFILE_MAX_AGE_DAYS


def load_quarters(store) -> pd.DataFrame:
    path = store.base / "ref" / "buyback_quarters.csv.gz"
    return pd.read_csv(path, dtype={"cik": str}) if path.exists() else pd.DataFrame()


def _quarter_weights(q_start: date, q_end: date, prev_lag_days: int) -> pd.Series:
    """四半期内の営業日ごとの重み。自粛期間は BLACKOUT_WEIGHT、それ以外は1。

    自粛期間は2つある:
    - 期首から、前四半期の決算発表の翌日まで（前四半期末 + 決算までの日数 + 1日）
    - 期末の14日前から期末まで
    """
    days = pd.bdate_range(q_start, q_end)
    w = pd.Series(1.0, index=days)
    early_until = pd.Timestamp(q_start + timedelta(days=prev_lag_days))
    late_from = pd.Timestamp(q_end - timedelta(days=BLACKOUT_BEFORE_DAYS))
    w[(days <= early_until) | (days >= late_from)] = BLACKOUT_WEIGHT
    return w


def daily_estimate(quarters: pd.DataFrame, until: date) -> pd.DataFrame:
    """企業ごとの四半期の値を日次に配分し、全社合計の日次系列を返す。

    columns: buyback_reported（報告済み四半期の配分）, buyback_estimated（未報告期間の推計）
    """
    if quarters.empty:
        return pd.DataFrame(columns=["buyback_reported", "buyback_estimated"])
    reported, estimated = [], []
    for _, g in quarters.groupby("cik"):
        g = g.sort_values("end")
        lag = 35  # 期末から決算発表（10-Q提出）までの日数。最初は一般的な値から始める
        lags = []
        for q in g.itertuples():
            q_start, q_end = date.fromisoformat(q.start), date.fromisoformat(q.end)
            w = _quarter_weights(q_start, q_end, lag)
            if w.sum() > 0:
                reported.append(q.usd * w / w.sum())
            lag = max(0, (date.fromisoformat(q.filed) - q_end).days)
            lags.append(lag)

        # 未報告の期間：直近4四半期の平均を、同じ長さの四半期として先へ延ばす
        pace = float(g["usd"].tail(4).mean())
        typical_lag = int(pd.Series(lags).tail(4).median()) if lags else 35
        last_end = date.fromisoformat(g["end"].iloc[-1])
        lag = lags[-1] if lags else typical_lag
        while last_end < until:
            q_start = last_end + timedelta(days=1)
            q_end = q_start + timedelta(days=90)
            w = _quarter_weights(q_start, q_end, lag)
            if w.sum() > 0:
                estimated.append(pace * w / w.sum())
            last_end, lag = q_end, typical_lag

    rep = pd.concat(reported).groupby(level=0).sum() if reported else pd.Series(dtype=float)
    est = pd.concat(estimated).groupby(level=0).sum() if estimated else pd.Series(dtype=float)
    df = pd.DataFrame({"buyback_reported": rep, "buyback_estimated": est}).fillna(0.0)
    df = df[df.index <= pd.Timestamp(until)]
    df.index = df.index.strftime("%Y%m%d")
    return df


def coverage(quarters: pd.DataFrame) -> dict:
    """直近の四半期について、何社分が報告済みかなど（画面の注記用）。"""
    if quarters.empty:
        return {}
    last = quarters.sort_values("end").groupby("cik").tail(1)
    return {
        "companies": int(quarters["cik"].nunique()),
        "latest_end": str(last["end"].max()),
        "as_of": datetime.now().date().isoformat(),
    }

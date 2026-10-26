"""米国版：保存済みデータから集計し、公開用のダッシュボード（site/us/index.html）を作る。"""
from __future__ import annotations

import json
from datetime import date, datetime
from pathlib import Path

import pandas as pd

from ..core.store import Store, SITE as SITE_ROOT
from . import buybacks

STORE = Store("us")
SITE = SITE_ROOT / "us"
TEMPLATE = Path(__file__).resolve().parent / "template.html"

FOLLOW_TYPES = {"follow_on", "registered_direct", "rights"}


def _billions(x: float) -> float:
    return round(float(x) / 1e9, 3)


def _week_end(s: pd.Series) -> pd.Series:
    """日付を、その週の金曜日に寄せる。"""
    d = pd.to_datetime(s.astype(str), format="%Y%m%d", errors="coerce")
    return (d + pd.to_timedelta((4 - d.dt.weekday) % 7, unit="D")).dt.normalize()


def daily_components() -> pd.DataFrame:
    """日ごとの構成要素（ドル）。今は実装済みの項目だけ。"""
    parts = []

    trades = STORE.read_all("insider_trades")
    if not trades.empty:
        t = trades.assign(day=trades["filed"].astype(str))
        g = t.pivot_table(index="day", columns="code", values="value_usd", aggfunc="sum", fill_value=0)
        parts.append(pd.DataFrame({
            "insider_sell": g.get("S", 0.0),
            "insider_buy": g.get("P", 0.0),
        }))

    f144 = STORE.read_all("form144")
    if not f144.empty:
        parts.append(f144.assign(day=f144["filed"].astype(str)).groupby("day")["market_value_usd"].sum().rename("form144").to_frame())

    offs = STORE.read_all("offerings")
    if not offs.empty and "offering_type" in offs:
        o = offs[(offs["status"] == "ok") & (offs["is_spac"] != True)].copy()  # noqa: E712
        o["usd"] = o[["primary_usd", "secondary_usd"]].fillna(0).sum(axis=1)
        o["day"] = o["filed"].astype(str)
        ipo = o[o["offering_type"] == "ipo"].groupby("day")["usd"].sum().rename("ipo")
        fol = o[o["offering_type"].isin(FOLLOW_TYPES)].groupby("day")["usd"].sum().rename("follow_on")
        parts.extend([ipo.to_frame(), fol.to_frame()])

    deals = STORE.read_all("mergers")
    if not deals.empty and "cash_usd" in deals:
        ok = deals[deals["status"] == "ok"]
        parts.append(ok.assign(day=ok["filed"].astype(str)).groupby("day")["cash_usd"].sum().rename("cash_mergers").to_frame())

    if not parts:
        return pd.DataFrame()
    df = pd.concat(parts, axis=1).fillna(0.0)

    # 自社株買い：処理済みの期間に合わせて日次推計を重ねる
    status = STORE.load_status().get("days", {})
    ok_days = sorted(k for k, v in status.items() if v.get("state") == "ok")
    if ok_days:
        first, last = date.fromisoformat(ok_days[0]), date.fromisoformat(ok_days[-1])
        bb = buybacks.daily_estimate(buybacks.load_quarters(STORE), until=last)
        bb = bb[(bb.index >= first.strftime("%Y%m%d"))]
        df = df.join(bb, how="outer").fillna(0.0)

    for col in ("insider_sell", "insider_buy", "form144", "ipo", "follow_on", "cash_mergers", "buyback_reported", "buyback_estimated"):
        if col not in df:
            df[col] = 0.0
    df["buyback"] = df["buyback_reported"] + df["buyback_estimated"]
    # 浮動株ベース（部分集計）：実装済みの項目だけで計算。ATM・株式報酬などは未実装
    demand = df["buyback"] + df["cash_mergers"] + df["insider_buy"]
    supply = df["insider_sell"] + df["ipo"] + df["follow_on"]
    df["net_partial"] = demand - supply
    df.index.name = "day"
    return df.sort_index()


def weekly(df: pd.DataFrame) -> pd.DataFrame:
    if df.empty:
        return df
    w = df.copy()
    w["week"] = _week_end(pd.Series(w.index, index=w.index))
    w = w.groupby("week").sum(numeric_only=True)
    w["net_13w"] = w["net_partial"].rolling(13, min_periods=1).sum()
    return w


def _records(df: pd.DataFrame, cols: list[str]) -> list[dict]:
    out = df[cols].copy()
    out = out.astype(object).where(pd.notna(out), None)
    return out.to_dict(orient="records")


def build() -> None:
    status = STORE.load_status()
    daily = daily_components()
    wk = weekly(daily)

    payload: dict = {"generated": datetime.now().astimezone().isoformat(timespec="minutes")}

    if not wk.empty:
        payload["weekly"] = {
            "weeks": [d.strftime("%Y-%m-%d") for d in wk.index],
            "ipo": [-_billions(v) for v in wk["ipo"]],
            "follow_on": [-_billions(v) for v in wk["follow_on"]],
            "insider_sell": [-_billions(v) for v in wk["insider_sell"]],
            "insider_buy": [_billions(v) for v in wk["insider_buy"]],
            "buyback_reported": [_billions(v) for v in wk["buyback_reported"]],
            "buyback_estimated": [_billions(v) for v in wk["buyback_estimated"]],
            "cash_mergers": [_billions(v) for v in wk["cash_mergers"]],
            "net_13w": [_billions(v) for v in wk["net_13w"]],
            "form144": [_billions(v) for v in wk["form144"]],
        }
        last4 = wk.tail(4)
        payload["kpi"] = {
            "net_4w": _billions(last4["net_partial"].sum()),
            "net_13w": _billions(wk["net_partial"].tail(13).sum()),
            "insider_sell_4w": _billions(last4["insider_sell"].sum()),
            "form144_4w": _billions(last4["form144"].sum()),
            "offerings_4w": _billions(last4["ipo"].sum() + last4["follow_on"].sum()),
            "buyback_4w": _billions(last4["buyback"].sum()),
            "mergers_4w": _billions(last4["cash_mergers"].sum()),
            "weeks_of_data": int(len(wk)),
        }

    trades = STORE.read_all("insider_trades")
    if not trades.empty and payload.get("weekly"):
        cutoff = (pd.Timestamp(payload["weekly"]["weeks"][-1]) - pd.Timedelta(days=27)).strftime("%Y%m%d")
        recent = trades[(trades["code"] == "S") & (trades["filed"].astype(str) >= cutoff)]
        top = (recent.groupby(["ticker", "issuer"], dropna=False)["value_usd"].sum()
               .sort_values(ascending=False).head(10).reset_index())
        top["value_musd"] = (top["value_usd"] / 1e6).round(1)
        payload["top_sellers"] = _records(top, ["ticker", "issuer", "value_musd"])

    offs = STORE.read_all("offerings")
    if not offs.empty:
        o = offs[offs["status"] == "ok"].copy()
        o["usd"] = o[["primary_usd", "secondary_usd"]].fillna(0).sum(axis=1)
        o = o[o["offering_type"].isin({"ipo", "spac_ipo", "atm", *FOLLOW_TYPES})]
        o = o.sort_values("filed", ascending=False).head(40)
        o["musd"] = (o["usd"] / 1e6).round(1)
        o["atm_musd"] = (o["atm_program_usd"].fillna(0) / 1e6).round(1)
        payload["offerings"] = _records(o, ["filed", "form", "company", "ticker", "offering_type", "price_per_share_usd",
                                            "musd", "atm_musd", "lockup_days", "url"])

    deals = STORE.read_all("mergers")
    if not deals.empty and "cash_usd" in deals:
        d = deals[deals["status"].isin(["ok", "ok_stock_only", "ok_missing_shares"])].copy()
        d = d.sort_values("filed", ascending=False).head(30)
        d["cash_musd"] = (d["cash_usd"].fillna(0) / 1e6).round(1)
        payload["mergers"] = _records(d, ["filed", "target", "acquirer", "cash_per_share_usd", "stock_consideration", "cash_musd", "source_url"])

    payload["buyback_coverage"] = buybacks.coverage(buybacks.load_quarters(STORE))

    regs = STORE.read_all("registrations")
    if not regs.empty:
        payload["registrations"] = _records(regs.sort_values("filed", ascending=False).head(25),
                                            ["filed", "form", "company", "url"])

    days = status.get("days", {})
    ok = {k: v for k, v in days.items() if v.get("state") == "ok"}
    payload["status"] = {
        "last_day": max(ok) if ok else None,
        "first_day": min(ok) if ok else None,
        "days_ok": len(ok),
        "last_run": status.get("last_run"),
        "errors": sum(v.get("form4_errors", 0) + v.get("form144_errors", 0) + v.get("prospectus_errors", 0) for v in ok.values()),
        "claude_calls": sum(v.get("claude_calls", 0) for v in ok.values()),
        "recent": [{"day": k, **v} for k, v in sorted(days.items(), reverse=True)[:10]],
    }

    SITE.mkdir(parents=True, exist_ok=True)
    html = TEMPLATE.read_text(encoding="utf-8").replace(
        "/*__DATA__*/null", json.dumps(payload, ensure_ascii=False, default=str)
    )
    (SITE / "index.html").write_text(html, encoding="utf-8")
    (SITE / "data.json").write_text(json.dumps(payload, ensure_ascii=False, default=str), encoding="utf-8")
    if not daily.empty:
        daily.to_csv(SITE / "daily.csv")

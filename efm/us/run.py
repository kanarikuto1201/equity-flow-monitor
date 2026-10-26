"""毎日の処理の入口。

使い方:
  python -m efm.us.run                     # 未処理の日をまとめて処理（通常の毎日の実行）
  python -m efm.us.run --start 2026-09-01 --end 2026-09-30   # 過去分の取り込み
  python -m efm.us.run --build-only        # ダッシュボードだけ作り直す

米国の取引日（東部時間）単位で処理する。EDGARの日次インデックスは東部時間の夜に
確定するため、まだ確定していない当日分は次回の実行に回す。
"""
from __future__ import annotations

import argparse
import sys
import time
import traceback
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

from . import converts, flows, insiders, mergers, offerings
from ..core.store import Store

STORE = Store("us")
from .edgar import EdgarClient, EdgarNotFound

ET_ZONE = ZoneInfo("America/New_York")


LOG_LINES: list[str] = []


def log(msg: str) -> None:
    line = f"[{datetime.now(ET_ZONE):%H:%M:%S} ET] {msg}"
    LOG_LINES.append(line)
    print(line, flush=True)


def error(msg: str) -> None:
    """GitHubの画面に注釈として表示されるエラー。"""
    log(msg)
    print(f"::error::{msg}", flush=True)


def save_log() -> None:
    folder = STORE.base / "logs"
    folder.mkdir(parents=True, exist_ok=True)
    (folder / "last_run.log").write_text("\n".join(LOG_LINES) + "\n", encoding="utf-8")


def business_days(start: date, end: date):
    d = start
    while d <= end:
        if d.weekday() < 5:
            yield d
        d += timedelta(days=1)


def latest_complete_day(now: datetime | None = None) -> date:
    """インデックスが確定している最後の日。東部時間23時より前なら前日まで。"""
    now = now or datetime.now(ET_ZONE)
    day = now.date() if now.hour >= 23 else now.date() - timedelta(days=1)
    return day


def plan_days(status: dict, start: date | None, end: date | None, max_days: int, force: bool = False) -> list[date]:
    last_ok = latest_complete_day()
    end = min(end or last_ok, last_ok)
    if start is None:
        done = [date.fromisoformat(k) for k in status["days"]]
        start = (max(done) + timedelta(days=1)) if done else end - timedelta(days=14)
    todo = [d for d in business_days(start, end) if force or d.isoformat() not in status["days"]]
    return todo[:max_days]


COLLECTORS = ("insiders", "offerings", "mergers", "converts")


def process_day(client: EdgarClient, day: date, collectors=COLLECTORS) -> tuple[dict, set[str]]:
    """1日分を処理する。戻り値：処理状況と、その日に10-Q / 10-K を出した会社のCIK。"""
    t0 = time.monotonic()
    entries = client.daily_index(day)
    log(f"{day}: 提出書類 {len(entries):,} 件")
    stats: dict = {"filings": len(entries)}

    if "insiders" in collectors:
        trades, notices, s = insiders.collect(client, entries, log)
        STORE.write_day("insider_trades", day, trades)
        STORE.write_day("form144", day, notices)
        stats.update(s)
    if "offerings" in collectors:
        offs, pipeline, s = offerings.collect(client, entries, log)
        STORE.write_day("offerings", day, offs)
        STORE.write_day("registrations", day, pipeline)
        stats.update(s)
    if "mergers" in collectors:
        deals, s = mergers.collect(client, entries, day, log)
        STORE.write_day("mergers", day, deals)
        stats.update(s)
    if "converts" in collectors:
        issues, s = converts.collect(client, day, log)
        STORE.write_day("converts", day, issues)
        stats.update(s)

    reporters = {str(int(e.cik)) for e in entries if e.form in ("10-Q", "10-K", "10-Q/A", "10-K/A")}
    stats["seconds"] = round(time.monotonic() - t0)
    log(f"{day}: 完了 {stats}")
    return stats, reporters


def retry_offering_errors(client: EdgarClient) -> None:
    """保存済みの目論見書のうち、読み取りに失敗したものだけを読み直して上書きする。"""
    import pandas as pd
    from dataclasses import asdict
    from .edgar import IndexEntry

    fixed = failed = 0
    for path in sorted((STORE.raw / "offerings").glob("*/*.csv.gz")):
        try:
            df = pd.read_csv(path, compression="gzip")
        except pd.errors.EmptyDataError:
            continue
        if df.empty or not df["status"].astype(str).str.startswith("error").any():
            continue
        rows = df.to_dict(orient="records")
        for i, r in enumerate(rows):
            if not str(r["status"]).startswith("error"):
                continue
            filename = r["url"].split("/Archives/", 1)[-1]
            e = IndexEntry(r["form"], r["company"], str(r["cik"]), str(r["filed"]), filename)
            try:
                rows[i] = asdict(offerings.read_offering(client, e, True))
                fixed += 1
            except Exception as exc:
                failed += 1
                log(f"読み直しに失敗: {r['url']} ({type(exc).__name__}: {exc})")
        pd.DataFrame(rows).to_csv(path, index=False, compression="gzip")
    log(f"目論見書の読み直し：成功 {fixed} 件、失敗 {failed} 件")


def main(argv=None) -> int:
    try:
        return _main(argv)
    except Exception as exc:
        error(f"処理全体が止まりました ({type(exc).__name__}: {exc})")
        log(traceback.format_exc())
        return 1
    finally:
        save_log()


def _main(argv=None) -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--start", type=date.fromisoformat)
    p.add_argument("--end", type=date.fromisoformat)
    p.add_argument("--max-days", type=int, default=5)
    p.add_argument("--build-only", action="store_true")
    p.add_argument("--force", action="store_true", help="処理済みの日も取り直す")
    p.add_argument("--collectors", default=",".join(COLLECTORS), help="取得する項目（カンマ区切り）")
    p.add_argument("--skip-buybacks", action="store_true", help="XBRLデータ（自社株買い・株式報酬など）の更新を省く")
    p.add_argument("--retry-errors", action="store_true", help="読み取りに失敗した目論見書だけを読み直す")
    args = p.parse_args(argv)

    status = STORE.load_status()
    failures = 0
    if not args.build_only:
        client = EdgarClient()
        log(client.check_access())
        days = plan_days(status, args.start, args.end, args.max_days, args.force)
        log(f"処理する日: {[d.isoformat() for d in days] or 'なし'}")
        if args.retry_errors:
            retry_offering_errors(client)
            days = []
        collectors = tuple(c.strip() for c in args.collectors.split(",") if c.strip())
        reporters: set[str] = set()
        for day in days:
            try:
                stats, rep = process_day(client, day, collectors)
                reporters |= rep
                prev = status["days"].get(day.isoformat(), {}) if collectors != COLLECTORS else {}
                status["days"][day.isoformat()] = {**prev, "state": "ok", **stats}
            except EdgarNotFound:
                # 過去日でインデックスがない＝米国の休日。最新日なら未確定なので次回に回す
                if day < latest_complete_day():
                    status["days"][day.isoformat()] = {"state": "holiday"}
                    log(f"{day}: インデックスなし（休日として記録）")
                else:
                    log(f"{day}: インデックスが未確定のため次回に回します")
            except Exception as exc:  # 1日分の失敗で全体を止めない
                failures += 1
                error(f"{day}: 失敗しました ({type(exc).__name__}: {exc})")
                log(traceback.format_exc())
            STORE.save_status(status)
        if not args.skip_buybacks:
            try:
                if reporters:
                    log(f"XBRL：10-Q / 10-K を出した {len(reporters):,} 社を更新します")
                flows.refresh_all(client, STORE, log, reporters)
            except Exception as exc:
                failures += 1
                error(f"XBRLデータ（自社株買い・株式報酬など）の更新に失敗 ({type(exc).__name__}: {exc})")
                log(traceback.format_exc())
        status["last_run"] = datetime.now(ET_ZONE).isoformat(timespec="seconds")
        status["last_run_requests"] = client.request_count
        STORE.save_status(status)

    from ..site import build_all

    build_all()
    log("ダッシュボードを作成しました")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())

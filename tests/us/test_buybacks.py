from datetime import date

import pandas as pd

from efm.us.buybacks import quarterly_from_facts, daily_estimate, BLACKOUT_WEIGHT

# 累計で報告されるキャッシュフロー（FY2025: 1月〜12月）。Q1=100, Q2=150, Q3=120, Q4=130
FACTS = [
    {"start": "2025-01-01", "end": "2025-03-31", "val": 100, "filed": "2025-05-01", "form": "10-Q"},
    {"start": "2025-01-01", "end": "2025-06-30", "val": 250, "filed": "2025-08-01", "form": "10-Q"},
    {"start": "2025-01-01", "end": "2025-09-30", "val": 370, "filed": "2025-10-31", "form": "10-Q"},
    {"start": "2025-01-01", "end": "2025-12-31", "val": 500, "filed": "2026-02-15", "form": "10-K"},
    # 翌年の書類に前年の数字が比較として再掲される（重複）
    {"start": "2025-01-01", "end": "2025-03-31", "val": 100, "filed": "2026-05-01", "form": "10-Q"},
    {"start": "2026-01-01", "end": "2026-03-31", "val": 140, "filed": "2026-05-01", "form": "10-Q"},
    # 8-Kなど対象外の書類は無視
    {"start": "2026-01-01", "end": "2026-03-31", "val": 999, "filed": "2026-04-20", "form": "8-K"},
]


def test_quarters_from_cumulative():
    q = quarterly_from_facts(FACTS)
    assert [x["usd"] for x in q] == [100, 150, 120, 130, 140]
    assert q[1]["start"] == "2025-04-01" and q[1]["end"] == "2025-06-30"
    assert q[0]["filed"] == "2025-05-01"  # 最初に報告された日


def test_daily_estimate_sums_and_blackout():
    q = pd.DataFrame([{"cik": "1", **x} for x in quarterly_from_facts(FACTS)])
    d = daily_estimate(q, until=date(2026, 9, 30))
    d.index = pd.to_datetime(d.index)
    # 報告済み四半期の配分は、四半期の実績と一致する
    q3 = d.loc["2025-07-01":"2025-09-30", "buyback_reported"].sum()
    assert abs(q3 - 120) < 1e-6
    # 未報告の期間（2026年4月以降）は、直近4四半期の平均（135）で推計される
    est = d.loc["2026-04-01":"2026-06-30", "buyback_estimated"].sum()
    assert 125 < est <= 135
    # 期末の自粛期間中は、通常日より少ない
    normal = d.loc["2025-08-12", "buyback_reported"]
    blackout = d.loc["2025-09-25", "buyback_reported"]
    assert abs(blackout / normal - BLACKOUT_WEIGHT) < 1e-9


def test_subject_company_from_header():
    from efm.us.mergers import subject_company
    header = """<SEC-HEADER>
CONFORMED SUBMISSION TYPE:	25-NSE
SUBJECT COMPANY:
	COMPANY DATA:
		COMPANY CONFORMED NAME:			TARGET THERAPEUTICS INC
		CENTRAL INDEX KEY:			0001234567
FILED BY:
	COMPANY DATA:
		COMPANY CONFORMED NAME:			NASDAQ STOCK MARKET LLC
		CENTRAL INDEX KEY:			0001120193
"""
    assert subject_company(header) == ("1234567", "TARGET THERAPEUTICS INC")


def test_process_day_runs_with_fake_client(tmp_path, monkeypatch):
    """毎日の処理の組み立て（引数・戻り値）が壊れていないかを、通信なしで確かめる。"""
    from efm.us import run
    from efm.us.edgar import IndexEntry
    from efm.core.store import Store

    monkeypatch.setattr(run, "STORE", Store("test"))
    run.STORE.base = tmp_path
    run.STORE.raw = tmp_path / "raw"

    class Fake:
        def daily_index(self, day):
            return [IndexEntry("10-Q", "A CO", "0000123", "20261023", "edgar/data/123/0000000000-26-000001.txt")]

    stats, reporters = run.process_day(Fake(), date(2026, 10, 23))
    assert reporters == {"123"} and stats["filings"] == 1
    assert run.main(["--build-only"]) in (0, 1)

"""取得したデータの保存（全市場共通）。

市場ごとに data/<market>/ の下へ、日付ごとの圧縮CSVで追記していく（過去分は消さない）。
  data/us/raw/insider_trades/2026/2026-10-23.csv.gz
  data/us/status.json
"""
from __future__ import annotations

import json
from datetime import date
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parents[2]
DATA = ROOT / "data"
SITE = ROOT / "site"


class Store:
    def __init__(self, market: str):
        self.market = market
        self.base = DATA / market
        self.raw = self.base / "raw"
        self.status_path = self.base / "status.json"

    def write_day(self, dataset: str, day: date, rows: list[dict]) -> None:
        folder = self.raw / dataset / f"{day:%Y}"
        folder.mkdir(parents=True, exist_ok=True)
        pd.DataFrame(rows).to_csv(folder / f"{day:%Y-%m-%d}.csv.gz", index=False, compression="gzip")

    def read_all(self, dataset: str) -> pd.DataFrame:
        frames = []
        for f in sorted((self.raw / dataset).glob("*/*.csv.gz")):
            try:
                df = pd.read_csv(f, compression="gzip")
            except pd.errors.EmptyDataError:
                continue
            if not df.empty:
                frames.append(df)
        return pd.concat(frames, ignore_index=True) if frames else pd.DataFrame()

    def load_status(self) -> dict:
        if self.status_path.exists():
            return json.loads(self.status_path.read_text(encoding="utf-8"))
        return {"days": {}}

    def save_status(self, status: dict) -> None:
        self.base.mkdir(parents=True, exist_ok=True)
        status["days"] = dict(sorted(status["days"].items()))
        self.status_path.write_text(json.dumps(status, ensure_ascii=False, indent=1), encoding="utf-8")

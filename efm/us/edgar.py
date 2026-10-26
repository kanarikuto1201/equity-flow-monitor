"""SEC EDGARへのアクセスをまとめた共通部品。

- SECの要件に従い、User-Agentに利用者の名前と連絡先を入れる（環境変数 SEC_USER_AGENT）
- アクセスは毎秒10回まで（余裕を見て毎秒8回に抑える）
- 一時的なエラーは間隔をあけて再試行する
"""
from __future__ import annotations

import os
import re
import threading
import time
from dataclasses import dataclass
from datetime import date

import requests

ARCHIVES = "https://www.sec.gov/Archives"
MAX_PER_SECOND = 8


class EdgarNotFound(Exception):
    """指定したファイルがEDGARに存在しない（休日、または未公開）。"""


@dataclass(frozen=True)
class IndexEntry:
    form: str
    company: str
    cik: str
    date_filed: str  # YYYYMMDD
    filename: str  # edgar/data/<cik>/<accession>.txt

    @property
    def accession(self) -> str:
        return self.filename.rsplit("/", 1)[-1].removesuffix(".txt")

    @property
    def url(self) -> str:
        return f"{ARCHIVES}/{self.filename}"


class EdgarClient:
    def __init__(self, user_agent: str | None = None):
        ua = user_agent or os.environ.get("SEC_USER_AGENT", "").strip()
        if not ua or "@" not in ua:
            raise RuntimeError(
                "SEC_USER_AGENT に「氏名 メールアドレス」を設定してください（SECの利用規則）"
            )
        self.session = requests.Session()
        self.session.headers.update({"User-Agent": ua, "Accept-Encoding": "gzip, deflate"})
        self._lock = threading.Lock()
        self._last = 0.0
        self.request_count = 0

    def _wait_turn(self) -> None:
        with self._lock:
            gap = 1.0 / MAX_PER_SECOND
            now = time.monotonic()
            if now - self._last < gap:
                time.sleep(gap - (now - self._last))
            self._last = time.monotonic()
            self.request_count += 1

    def get(self, url: str, *, max_bytes: int | None = None, retries: int = 4) -> bytes:
        """URLを取得する。max_bytesを指定すると先頭だけ読む（大きな目論見書向け）。"""
        for attempt in range(retries):
            self._wait_turn()
            try:
                resp = self.session.get(url, timeout=30, stream=max_bytes is not None)
            except requests.RequestException:
                time.sleep(2 ** attempt)
                continue
            if resp.status_code == 404:
                raise EdgarNotFound(url)
            if resp.status_code == 403:
                body = resp.text[:400].replace("\n", " ")
                resp.close()
                # SECは存在しないファイルにも403を返すことがある。利用規則違反の拒否とは本文で見分ける
                if "Undeclared Automated Tool" in body or "Request Rate Threshold" in body:
                    raise RuntimeError(f"SECにアクセスを拒否されました（User-Agentまたはアクセス頻度）: {body[:200]}")
                if "/daily-index/" in url:
                    raise EdgarNotFound(url)
                raise RuntimeError(f"403 Forbidden: {url} / {body[:200]}")
            if resp.status_code in (429, 500, 502, 503, 504):
                resp.close()
                time.sleep(2 ** attempt * 2)
                continue
            resp.raise_for_status()
            if max_bytes is None:
                return resp.content
            chunks, total = [], 0
            for chunk in resp.iter_content(chunk_size=65536):
                chunks.append(chunk)
                total += len(chunk)
                if total >= max_bytes:
                    break
            resp.close()
            return b"".join(chunks)[:max_bytes]
        raise RuntimeError(f"EDGARから取得できませんでした: {url}")

    def check_access(self) -> str:
        """SECに正しくアクセスできるかを、必ず存在するファイルで確かめる。"""
        data = self.get("https://www.sec.gov/files/company_tickers.json")
        return f"SECへの接続を確認しました（{len(data):,} バイト）"

    def daily_index(self, day: date) -> list[IndexEntry]:
        """その日に提出された全書類の一覧（form.YYYYMMDD.idx）を返す。"""
        qtr = (day.month - 1) // 3 + 1
        url = f"{ARCHIVES}/edgar/daily-index/{day.year}/QTR{qtr}/form.{day:%Y%m%d}.idx"
        text = self.get(url).decode("latin-1")
        return parse_form_index(text)


def parse_form_index(text: str) -> list[IndexEntry]:
    lines = text.splitlines()
    company_col = None
    start = None
    for i, line in enumerate(lines):
        if line.startswith("Form Type") and "Company Name" in line:
            company_col = line.index("Company Name")
        if company_col is not None and line.startswith("---"):
            start = i + 1
            break
    if company_col is None or start is None:
        raise ValueError("日次インデックスの形式を読み取れませんでした")

    entries: list[IndexEntry] = []
    seen: set[tuple[str, str]] = set()
    for line in lines[start:]:
        if not line.strip():
            continue
        parts = line.rsplit(None, 3)
        if len(parts) != 4:
            continue
        left, cik, date_filed, filename = parts
        form = left[:company_col].strip()
        company = left[company_col:].strip()
        # 同じ書類が提出者ごとに複数行出るため、書類番号で重複を除く
        key = (form, filename)
        if key in seen:
            continue
        seen.add(key)
        entries.append(IndexEntry(form, company, cik, date_filed, filename))
    return entries


_XML_BLOCK = re.compile(rb"<XML>(.*?)</XML>", re.S | re.I)


def extract_xml(submission: bytes) -> bytes | None:
    """提出書類（.txt）の中からXML部分を取り出す（Form 4、Form 144で使う）。"""
    m = _XML_BLOCK.search(submission)
    return m.group(1).strip() if m else None

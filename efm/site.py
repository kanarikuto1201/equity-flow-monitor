"""公開サイト全体を作る（全市場共通）。

どの市場の処理から呼ばれても、保存済みデータから全市場のページを作り直す。
こうしておくと、米国版と日本版の更新が別々に動いても、片方の公開でもう片方が消えない。

  site/index.html     市場の一覧（トップページ）
  site/us/index.html  米国版
  site/jp/index.html  日本版（今後追加）

使い方: python -m efm.site
"""
from __future__ import annotations

import html
import importlib

from .core.store import SITE, Store

# 市場を追加するときは、ここに1行足し、efm/<code>/dashboard.py に build() を用意する
MARKETS = [
    {"code": "us", "name": "米国株", "module": "efm.us.dashboard"},
    {"code": "jp", "name": "日本株", "module": None},  # 準備中
]


def _landing(cards: list[dict]) -> str:
    items = []
    for c in cards:
        if c["ready"]:
            items.append(
                f'<a class="card" href="{c["code"]}/"><span class="name">{html.escape(c["name"])}</span>'
                f'<span class="meta">データ：{html.escape(c["last_day"] or "—")} まで</span></a>'
            )
        else:
            items.append(f'<div class="card off"><span class="name">{html.escape(c["name"])}</span><span class="meta">準備中</span></div>')
    return f"""<!doctype html>
<html lang="ja"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>株式 需給モニター</title>
<style>
:root {{ --bg:#f7f7f4; --panel:#fff; --ink:#1d1d1b; --quiet:#6b6b66; --rule:#e4e3dd; --link:#1d4ed8; }}
@media (prefers-color-scheme: dark) {{ :root {{ --bg:#151514; --panel:#1f1f1d; --ink:#ededea; --quiet:#a3a39c; --rule:#33332f; --link:#7aa2ff; }} }}
body {{ margin:0; background:var(--bg); color:var(--ink); font:15px/1.6 -apple-system,BlinkMacSystemFont,"Hiragino Sans","Noto Sans JP",sans-serif; }}
main {{ max-width:720px; margin:0 auto; padding:40px 16px; }}
h1 {{ font-size:22px; margin:0 0 6px; }} p {{ color:var(--quiet); margin:0 0 24px; font-size:14px; }}
.cards {{ display:grid; grid-template-columns:repeat(auto-fit,minmax(220px,1fr)); gap:12px; }}
.card {{ display:flex; flex-direction:column; gap:4px; padding:18px 20px; background:var(--panel); border:1px solid var(--rule); border-radius:10px; text-decoration:none; color:var(--ink); }}
a.card:hover {{ border-color:var(--link); }}
.card .name {{ font-size:18px; font-weight:650; }} .card .meta {{ color:var(--quiet); font-size:13px; }}
.card.off {{ opacity:.55; }}
</style></head>
<body><main>
<h1>株式 需給モニター</h1>
<p>株式市場で「誰が株を吸収し、誰が株を出しているか」を、公開データから毎日推計します。</p>
<div class="cards">{''.join(items)}</div>
</main></body></html>"""


def build_all() -> None:
    SITE.mkdir(parents=True, exist_ok=True)
    cards = []
    for m in MARKETS:
        ready = False
        last_day = None
        if m["module"]:
            importlib.import_module(m["module"]).build()
            days = Store(m["code"]).load_status().get("days", {})
            ok = [k for k, v in days.items() if v.get("state") == "ok"]
            last_day = max(ok) if ok else None
            ready = True
        cards.append({**m, "ready": ready, "last_day": last_day})
    (SITE / "index.html").write_text(_landing(cards), encoding="utf-8")


if __name__ == "__main__":
    build_all()

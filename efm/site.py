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
:root {{ --bg:#fff; --ink:#0f1b2d; --quiet:#5b6676; --rule:#d9dee5; --navy:#13294b; --accent:#2f6db5; }}
@media (prefers-color-scheme: dark) {{ :root:not([data-theme="light"]) {{ --bg:#0d1420; --ink:#e7ecf3; --quiet:#9aa6b6; --rule:#2a3648; --navy:#c9d7ec; --accent:#7fa6d6; }} }}
:root[data-theme="dark"] {{ --bg:#0d1420; --ink:#e7ecf3; --quiet:#9aa6b6; --rule:#2a3648; --navy:#c9d7ec; --accent:#7fa6d6; }}
body {{ margin:0; background:var(--bg); color:var(--ink); font:14px/1.6 "Hiragino Sans","Noto Sans JP",-apple-system,sans-serif; }}
main {{ max-width:880px; margin:0 auto; padding:0 16px 48px; }}
.mast {{ border-bottom:3px solid var(--navy); padding:28px 0 12px; }}
.kicker {{ font-size:11px; letter-spacing:.18em; color:var(--quiet); text-transform:uppercase; }}
h1 {{ margin:4px 0 0; font:600 28px/1.3 "Hiragino Mincho ProN","Yu Mincho","Noto Serif JP",Georgia,serif; color:var(--navy); }}
p {{ color:var(--quiet); margin:10px 0 0; font-size:13px; }}
.cards {{ display:grid; grid-template-columns:repeat(auto-fit,minmax(240px,1fr)); gap:0 32px; margin-top:8px; }}
.card {{ display:block; padding:18px 0; border-bottom:1px solid var(--rule); text-decoration:none; color:var(--ink); }}
.card .name {{ display:block; font:600 20px/1.4 "Hiragino Mincho ProN","Yu Mincho","Noto Serif JP",Georgia,serif; color:var(--navy); }}
a.card:hover .name {{ color:var(--accent); }}
.card .meta {{ font-size:12px; color:var(--quiet); }}
.card.off {{ opacity:.5; }}
</style></head>
<body><main>
<div class="mast"><div class="kicker">Equity Flows</div><h1>株式 需給モニター</h1></div>
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

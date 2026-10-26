"""Claude APIの呼び出し（全市場共通）。書類から決まった項目をJSONで取り出す用途に限る。"""
from __future__ import annotations

import json
import os
import re


def available() -> bool:
    return bool(os.environ.get("ANTHROPIC_API_KEY"))


def ask_json(prompt: str, max_tokens: int = 600) -> dict:
    import anthropic

    client = anthropic.Anthropic()
    model = os.environ.get("CLAUDE_MODEL", "claude-haiku-5-5")
    msg = client.messages.create(model=model, max_tokens=max_tokens, messages=[{"role": "user", "content": prompt}])
    out = "".join(block.text for block in msg.content if getattr(block, "type", "") == "text")
    m = re.search(r"\{.*\}", out, re.S)
    if not m:
        raise ValueError("Claudeの返答からJSONを読み取れませんでした")
    return json.loads(m.group(0))

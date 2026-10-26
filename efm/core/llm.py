"""Claude APIの呼び出し（全市場共通）。書類から決まった項目をJSONで取り出す用途に限る。"""
from __future__ import annotations

import json
import os
import re


def available() -> bool:
    return bool(os.environ.get("ANTHROPIC_API_KEY"))


def _parse(out: str) -> dict | None:
    out = re.sub(r"```(?:json)?", "", out)
    start = out.find("{")
    if start == -1:
        return None
    # 最初の { から、括弧の対応が閉じるところまでを読む
    depth = 0
    for i, ch in enumerate(out[start:], start):
        depth += ch == "{"
        depth -= ch == "}"
        if depth == 0:
            try:
                return json.loads(out[start : i + 1])
            except json.JSONDecodeError:
                return None
    return None


def ask_json(prompt: str, max_tokens: int = 1000) -> dict:
    import anthropic

    client = anthropic.Anthropic()
    model = os.environ.get("CLAUDE_MODEL", "claude-haiku-5-5")
    out = ""
    for attempt in range(2):
        content = prompt if attempt == 0 else prompt + "\n\nReturn only the JSON object, starting with { and ending with }."
        msg = client.messages.create(model=model, max_tokens=max_tokens, messages=[{"role": "user", "content": content}])
        out = "".join(block.text for block in msg.content if getattr(block, "type", "") == "text")
        data = _parse(out)
        if isinstance(data, dict):
            return data
    raise ValueError(f"Claudeの返答からJSONを読み取れませんでした: {out[:150]!r}")

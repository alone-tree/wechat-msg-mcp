"""消息渲染：同一份数据出两种人读格式。

依据 docs/重构方案-消息模型与输出规范-2026-11-14.md 第 6 节。
text 与 md 都按会话分节、一行一条；多行正文缩进续写，不切成表格。
程序用 json/ndjson/csv，不走这里。
"""
from __future__ import annotations

from typing import Iterable, Optional

INDENT = "  "


def _who(m: dict) -> str:
    return m.get("from") or "（未知发送者）"


def _body(m: dict) -> str:
    return (m.get("text") or "").strip()


def _chat_of(m: dict, default: str = "") -> str:
    return m.get("chat_name") or m.get("chat") or default or ""


def message_line(m: dict, chat: str = "") -> str:
    """一条消息压成一行，多行正文续行缩进。"""
    head = f"[{m.get('time') or ''}] "
    if chat:
        head += f"{chat} "
    who = _who(m)
    body = _body(m)
    first = f"{head}{who}: {body}" if body else f"{head}{who}:"
    lines = first.split("\n")
    out = [lines[0]]
    out.extend(INDENT + line.rstrip() for line in lines[1:])
    media = m.get("media")
    if isinstance(media, dict) and media.get("path"):
        out.append(INDENT + f"path: {media['path']}")
    return "\n".join(out)


def _sections(rows: list[dict], default_chat: str) -> list[tuple[str, list[dict]]]:
    order: list[str] = []
    grouped: dict[str, list[dict]] = {}
    for m in rows:
        chat = _chat_of(m, default_chat)
        if chat not in grouped:
            grouped[chat] = []
            order.append(chat)
        grouped[chat].append(m)
    return [(chat, grouped[chat]) for chat in order]


def _summary(obj: dict) -> str:
    total = obj.get("total")
    returned = obj.get("returned", len(obj.get("messages") or []))
    more = "是" if obj.get("has_more") else "否"
    cut = obj.get("data_until") or ""
    parts = [f"本次 {returned} 条", f"命中 {total} 条" if total is not None else "", f"还有更多 {more}"]
    if cut:
        parts.append(f"数据截至 {cut}")
    return "，".join(p for p in parts if p)


def render_text(obj: dict, default_chat: str = "") -> str:
    rows = obj.get("messages") or []
    sections = _sections(rows, default_chat)
    out: list[str] = []
    for chat, items in sections:
        title = chat or "（未命名会话）"
        out.append(f"=== {title}（{len(items)} 条）===")
        for m in items:
            out.append(message_line(m))
        out.append("")
    out.append(_summary(obj))
    return "\n".join(out).rstrip() + "\n"


def render_md(obj: dict, default_chat: str = "") -> str:
    rows = obj.get("messages") or []
    sections = _sections(rows, default_chat)
    out: list[str] = []
    for chat, items in sections:
        out.append(f"## {chat or '（未命名会话）'}（{len(items)} 条）")
        out.append("")
        for m in items:
            out.append(message_line(m))
            out.append("")
    out.append(_summary(obj))
    return "\n".join(out).rstrip() + "\n"


def iter_ndjson(rows: Iterable[dict], default_chat: str = "") -> Iterable[str]:
    import json

    for m in rows:
        item = dict(m)
        chat = _chat_of(m, default_chat)
        if chat:
            item.setdefault("chat", chat)
        yield json.dumps(item, ensure_ascii=False)


def render_ndjson(obj: dict, default_chat: str = "") -> str:
    return "\n".join(iter_ndjson(obj.get("messages") or [], default_chat)) + "\n"

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
    out.extend((INDENT + line.rstrip()) if line.strip() else "" for line in lines[1:])
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
    more = "还有更多" if obj.get("has_more") else "已到末尾"
    cut = obj.get("data_until") or ""
    parts = [f"本次 {returned} 条", f"命中 {total} 条" if total is not None else "", more]
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


def render_html(obj: dict, default_chat: str = "") -> str:
    """消息的 HTML 导出：按会话分节，一条一段，保留换行。给浏览器看，不出表格。"""
    import html as html_mod

    rows = obj.get("messages") or []
    out = [
        "<!doctype html><meta charset='utf-8'><title>聊天记录导出</title>",
        "<style>body{font:14px system-ui;margin:24px;color:#222;background:#fff}"
        "h3{margin:28px 0 8px;padding-bottom:4px;border-bottom:1px solid #ddd}"
        ".m{margin:0 0 10px;line-height:1.6}.t{color:#888;margin-right:8px}"
        ".who{color:#0a6;font-weight:600;margin-right:6px}.body{white-space:pre-wrap}"
        ".path{color:#888;font-size:12px;white-space:pre-wrap}.f{margin-top:24px;color:#666}</style>",
    ]
    for chat, items in _sections(rows, default_chat):
        out.append(f"<h3>{html_mod.escape(chat or '（未命名会话）')}（{len(items)} 条）</h3>")
        for m in items:
            text = html_mod.escape(_body(m))
            who = html_mod.escape(_who(m))
            out.append(
                f"<div class='m'><span class='t'>{html_mod.escape(str(m.get('time') or ''))}</span>"
                f"<span class='who'>{who}</span><span class='body'>{text}</span></div>"
            )
            media = m.get("media")
            if isinstance(media, dict) and media.get("path"):
                out.append(f"<div class='path'>{html_mod.escape(str(media['path']))}</div>")
    out.append(f"<div class='f'>{html_mod.escape(_summary(obj))}</div>")
    return "\n".join(out) + "\n"


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

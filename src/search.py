"""统一搜索：实体与消息一起搜。

依据 docs/重构方案-2026-11-13.md 第 6.4 节：scope=all 时同时返回
命中的实体（备注、昵称、微信号、群名）与消息（正文命中，带片段）。
消息结果给出 total 与 offset 分页，类型统一为字符串，便于与 get_messages 对齐。
"""
from __future__ import annotations

import re

from . import index
from .db.locator import LocalSource
from .db.types import type_name as main_type_name

_CJK_SPACE = re.compile(r"(?<=[\u4e00-\u9fff])\s+(?=[\u4e00-\u9fff])")


def _despace(text: str) -> str:
    """把索引里逐字加的空格还原，便于阅读。"""
    return _CJK_SPACE.sub("", text or "")


def _entity_hits(
    src: LocalSource, query: str, kinds: list[str] | None, limit: int
) -> tuple[list[dict], int]:
    """返回 (本页命中的实体, 命中总数)。总数不受 limit 截断，翻页时也照给。"""
    from .entities import display_names, load_contacts

    q = query.strip().lower()
    if not q:
        return [], 0
    named = display_names(src)
    out: list[dict] = []
    total = 0
    for c in load_contacts(src):
        if kinds and c["kind"] not in kinds:
            continue
        name = (named.get(c["username"]) or {}).get("name") or c.get("remark") or c.get("nickname") or c["username"]
        fields = {
            "id": c["username"],
            "remark": c.get("remark", ""),
            "nickname": c.get("nickname", ""),
            "alias": c.get("alias", ""),
            "name": name,
        }
        for field, value in fields.items():
            if value and q in str(value).lower():
                total += 1
                if len(out) < limit:
                    out.append(
                        {
                            "id": c["username"],
                            "kind": c["kind"],
                            "name": name,
                            "matched": field,
                            "value": value,
                        }
                    )
                break
    return out, total


def search(
    src: LocalSource,
    query: str,
    scope: str = "all",
    kinds: list[str] | None = None,
    since: str = "",
    until: str = "",
    limit: int = 30,
    offset: int = 0,
) -> dict:
    from .entities import load_contacts
    from .messages import _ts, _ts_end

    if scope not in ("message", "entity", "all"):
        return {
            "ok": False,
            "error": {
                "code": "unknown_scope",
                "message": f"未知 scope: {scope}",
                "candidates": ["message", "entity", "all"],
            },
            "hint": "写错取值不会自动回退",
        }
    scope = scope if scope in ("message", "entity", "all") else "all"
    entities: list[dict] = []
    msgs: list[dict] = []
    msg_total = 0
    entity_total = 0
    if scope in ("entity", "all"):
        entities, entity_total = _entity_hits(src, query, kinds, limit)
    if scope in ("message", "all"):
        lo, hi = _ts(since), _ts_end(until)
        from .db.sender import ContactIndex
        from .entities import display_names

        contacts = ContactIndex()
        for c in load_contacts(src):
            contacts.add({"id": c.get("id"), **c})
        named = display_names(src)
        try:
            from .config import get_account_wxid

            my_user = get_account_wxid()
        except Exception:
            my_user = ""
        msg_total = index.count_query(src, query, since=lo, until=hi)
        for hit in index.query(src, query, limit=limit, offset=offset, since=lo, until=hi):
            raw_type = int(hit["type"] or 0)
            sid = hit["sender_id"]
            msgs.append(
                {
                    "db": hit["db"],
                    "id": hit["local_id"],
                    "chat_id": hit["chat_id"],
                    "chat_name": (named.get(hit["chat_id"]) or {}).get("name") or hit["chat_id"],
                    "sender_id": sid,
                    "from": "我" if (my_user and sid == my_user) else (contacts.name_of(sid) or ""),
                    "time": __import__("datetime")
                    .datetime.fromtimestamp(hit["ts"])
                    .strftime("%Y-%m-%d %H:%M:%S")
                    if hit["ts"]
                    else "",
                    "type": main_type_name(raw_type),
                    "snippet": _despace(hit.get("snip") or ""),
                }
            )
    next = offset + len(msgs) if scope in ("message", "all") and msg_total > offset + len(msgs) else None
    return {
        "ok": True,
        "query": query,
        "scope": scope,
        "data_until": src.data_until(),
        "entities": entities,
        "messages": msgs,
        "counts": {"entities": len(entities), "messages": len(msgs)},
        "total": {
            "entities": entity_total if scope in ("entity", "all") else None,
            "messages": msg_total,
        },
        "has_more": next is not None,
        "next": next,
    }

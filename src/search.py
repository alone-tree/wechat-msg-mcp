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


def _entity_hits(src: LocalSource, query: str, kinds: list[str] | None, limit: int) -> list[dict]:
    from .entities import load_contacts

    q = query.strip().lower()
    if not q:
        return []
    out = []
    for c in load_contacts(src):
        if kinds and c["kind"] not in kinds:
            continue
        fields = {
            "id": c["username"],
            "remark": c.get("remark", ""),
            "nickname": c.get("nickname", ""),
            "alias": c.get("alias", ""),
            "name": c.get("remark") or c.get("nickname") or c["username"],
        }
        for field, value in fields.items():
            if value and q in str(value).lower():
                out.append(
                    {
                        "id": c["username"],
                        "kind": c["kind"],
                        "name": fields["name"],
                        "matched": field,
                        "value": value,
                    }
                )
                break
        if len(out) >= limit:
            break
    return out


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

    scope = scope if scope in ("message", "entity", "all") else "all"
    entities: list[dict] = []
    msgs: list[dict] = []
    msg_total = 0
    if scope in ("entity", "all"):
        entities = _entity_hits(src, query, kinds, limit)
    if scope in ("message", "all"):
        lo, hi = _ts(since), _ts_end(until)
        from .db.sender import ContactIndex

        contacts = ContactIndex()
        names = {}
        for c in load_contacts(src):
            contacts.add({"id": c.get("id"), **c})
            names[c["username"]] = c.get("remark") or c.get("nickname") or c["username"]
        msg_total = index.count_query(src, query, since=lo, until=hi)
        for hit in index.query(src, query, limit=limit, offset=offset, since=lo, until=hi):
            raw_type = int(hit["type"] or 0)
            msgs.append(
                {
                    "db": hit["db"],
                    "local_id": hit["local_id"],
                    "chat_id": hit["chat_id"],
                    "chat_name": names.get(hit["chat_id"], hit["chat_id"]),
                    "sender_id": hit["sender_id"],
                    "sender_name": contacts.name_of(hit["sender_id"]) or "",
                    "time": __import__("datetime")
                    .datetime.fromtimestamp(hit["ts"])
                    .strftime("%Y-%m-%d %H:%M:%S")
                    if hit["ts"]
                    else "",
                    "type": main_type_name(raw_type),
                    "local_type": raw_type,
                    "snippet": _despace(hit.get("snip") or ""),
                }
            )
    next_offset = offset + len(msgs) if scope in ("message", "all") and msg_total > offset + len(msgs) else None
    return {
        "query": query,
        "scope": scope,
        "data_cutoff": src.data_cutoff(),
        "entities": entities,
        "messages": msgs,
        "counts": {"entities": len(entities), "messages": len(msgs)},
        "total": {"entities": len(entities) if offset == 0 else None, "messages": msg_total},
        "has_more": next_offset is not None,
        "next_offset": next_offset,
    }

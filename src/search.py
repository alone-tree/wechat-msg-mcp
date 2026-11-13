"""统一搜索：实体与消息一起搜。

依据 docs/重构方案-2026-11-13.md 第 6.4 节：scope=all 时同时返回
命中的实体（备注、昵称、微信号、群名）与消息（正文命中，带片段）。
"""
from __future__ import annotations

import re

from . import index
from .db.locator import LocalSource

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
) -> dict:
    from .entities import load_contacts
    from .messages import _ts, _ts_end

    scope = scope if scope in ("message", "entity", "all") else "all"
    entities: list[dict] = []
    msgs: list[dict] = []
    if scope in ("entity", "all"):
        entities = _entity_hits(src, query, kinds, limit)
    if scope in ("message", "all"):
        lo, hi = _ts(since), _ts_end(until)
        from .config import get_account_wxid
        from .db.sender import ContactIndex

        contacts = ContactIndex()
        names = {}
        for c in load_contacts(src):
            contacts.add({"id": c.get("id"), **c})
            names[c["username"]] = c.get("remark") or c.get("nickname") or c["username"]
        for hit in index.query(src, query, limit=limit, since=lo, until=hi):
            msgs.append(
                {
                    "db": hit["db"],
                    "local_id": hit["local_id"],
                    "chat_id": hit["chat_id"],
                    "chat_name": names.get(hit["chat_id"], hit["chat_id"]),
                    "sender_id": hit["sender_id"],
                    "sender_name": contacts.name_of(hit["sender_id"]) or "",
                    "time": __import__("datetime").datetime.fromtimestamp(hit["ts"]).strftime(
                        "%Y-%m-%d %H:%M:%S"
                    )
                    if hit["ts"]
                    else "",
                    "type": hit["type"],
                    "snippet": _despace(hit.get("snip") or ""),
                }
            )
    return {
        "query": query,
        "scope": scope,
        "data_cutoff": src.data_cutoff(),
        "entities": entities,
        "messages": msgs,
        "counts": {"entities": len(entities), "messages": len(msgs)},
    }

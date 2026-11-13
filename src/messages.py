"""消息查询。

依据 docs/重构方案-2026-11-13.md 第 6.2 节：
- 默认最新优先（order=desc），导出时用 asc
- 返回 total、returned、has_more、next_cursor、data_cutoff
- 目标名解析失败返回 unresolved 与候选，不返回空结果冒充无消息
- 游标为 (ts, db, tbl, local_id) 四元组，跨分库翻页不重复不遗漏
"""
from __future__ import annotations

import heapq
import re
import sqlite3
from datetime import datetime
from pathlib import Path
from typing import Iterable, Optional

import zstandard as zstd

from .db.locator import LocalSource
from .db.open_ import connect_ro
from .db.sender import ContactIndex, build_local_sender_map, resolve_sender, username_to_md5
from .db.types import classify

_ZSTD = zstd.ZstdDecompressor()
_IMG_MD5 = re.compile(r'md5="([0-9a-f]{32})"')
_VOICE_LEN = re.compile(r'voicelength="(\d+)"')
_VIDEO_LEN = re.compile(r'playlength="(\d+)"')

_TYPE_ALIASES = {
    "text": ["text"],
    "image": ["image"],
    "voice": ["voice"],
    "video": ["video"],
    "emoticon": ["emoticon"],
    "file": ["app:file"],
    "link": ["app:link"],
    "mini_program": ["app:mini_program", "app:app_36"],
    "quote": ["app:quote"],
    "chat_record": ["app:chat_record"],
    "channels": ["app:channels"],
    "location": ["location"],
    "system": ["system"],
    "revoked": ["revoked"],
}


class _Rev:
    """让 heapq 变成大顶堆：比较反转。"""

    __slots__ = ("key",)

    def __init__(self, key):
        self.key = key

    def __lt__(self, other):
        return self.key > other.key


def _content(raw, flag) -> str:
    if raw is None:
        return ""
    if isinstance(raw, str):
        return raw
    try:
        if flag == 4:
            return _ZSTD.decompress(raw).decode("utf-8", "replace")
        return raw.decode("utf-8", "replace")
    except Exception:
        return ""


def _fmt(ts) -> str:
    try:
        return datetime.fromtimestamp(int(ts)).strftime("%Y-%m-%d %H:%M:%S")
    except Exception:
        return ""


def _ts(date: str) -> Optional[int]:
    if not date:
        return None
    try:
        return int(datetime.strptime(date, "%Y-%m-%d").timestamp())
    except ValueError:
        return None


def _ts_end(date: str) -> Optional[int]:
    t = _ts(date)
    return t + 86399 if t is not None else None


def _media(content: str, type_name: str) -> Optional[dict]:
    if type_name == "image":
        m = _IMG_MD5.search(content or "")
        return {"kind": "image", "md5": m.group(1) if m else ""}
    if type_name == "voice":
        m = _VOICE_LEN.search(content or "")
        return {"kind": "voice", "duration": (int(m.group(1)) // 1000) if m else None}
    if type_name == "video":
        m = _VIDEO_LEN.search(content or "")
        return {"kind": "video", "duration": int(m.group(1)) if m else None}
    if type_name == "emoticon":
        return {"kind": "emoticon"}
    return None


def resolve_tables(src: LocalSource, chat_id: str) -> list[tuple[Path, str]]:
    """找到某个会话的 Msg_ 分表，可能跨多个分库。"""
    table = "Msg_" + username_to_md5(chat_id)
    found: list[tuple[Path, str]] = []
    for db in src.message_dbs():
        conn = connect_ro(db)
        try:
            row = conn.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)
            ).fetchone()
            if row:
                found.append((db, table))
        finally:
            conn.close()
    return found


def _type_wanted(types: Optional[Iterable[str]]) -> Optional[set[str]]:
    if not types:
        return None
    out: set[str] = set()
    for t in types:
        out.update(_TYPE_ALIASES.get(str(t).strip().lower(), [str(t).strip().lower()]))
    return out


def _my_username() -> str:
    try:
        from .config import get_account_wxid

        return get_account_wxid()
    except Exception:
        return ""


def _row(db: Path, table: str, r, sender_map: dict, my_username: str, contacts: ContactIndex) -> dict:
    text = _content(r["message_content"], r["WCDB_CT_message_content"])
    type_name, subtype = classify(r["local_type"], text)
    sid, is_me = resolve_sender(r["real_sender_id"], sender_map, my_username)
    if type_name in ("text", "system", "app", "quote"):
        head, sep, rest = text.partition("\n")
        if sep and head.endswith(":") and re.match(r"^[\w@._-]+$", head[:-1]):
            prefix_id = head[:-1]
            text = rest
            if not sid:
                sid = prefix_id
                is_me = bool(my_username) and prefix_id == my_username
    return {
        "local_id": r["local_id"],
        "time": _fmt(r["create_time"]),
        "ts": int(r["create_time"] or 0),
        "direction": "out" if is_me else "in",
        "type": type_name,
        "subtype": subtype,
        "sender_id": sid or "",
        "sender_name": "我" if is_me else (contacts.name_of(sid) or ""),
        "chat_id": "",
        "chat_name": "",
        "text": text if type_name in ("text", "system", "app", "quote") else "",
        "media": _media(text, type_name),
        "_db": db.name,
        "_tbl": table,
    }


def _sort_key(row: dict):
    return (row["ts"], row["_db"], row["_tbl"], row["local_id"])


def _cursor_key(cursor: str):
    try:
        ts, db, tbl, lid = cursor.split("|")
        return (int(ts), db, tbl, int(lid))
    except Exception:
        return None


def list_messages(
    src: LocalSource,
    targets: Optional[list[str]] = None,
    sender: str = "",
    start: str = "",
    end: str = "",
    types: Optional[list[str]] = None,
    keyword: str = "",
    order: str = "desc",
    limit: int = 200,
    cursor: str = "",
    per_target_limit: int = 50,
) -> dict:
    from .entities import load_contacts, resolve_name

    limit = max(1, min(int(limit), 1000))
    order = "asc" if str(order).lower() == "asc" else "desc"
    lo, hi = _ts(start), _ts_end(end)
    wanted = _type_wanted(types)
    kw = keyword.strip().lower()
    my_user = _my_username()

    contact_rows = load_contacts(src)
    contacts = ContactIndex()
    name_by_id: dict[str, str] = {}
    for c in contact_rows:
        contacts.add({"id": c.get("id"), **c})
        name_by_id[c["username"]] = c.get("remark") or c.get("nickname") or c["username"]

    unresolved: list[dict] = []
    resolved: list[dict] = []
    if targets:
        for t in targets:
            hit = resolve_name(src, t, limit=3)
            if hit.get("candidates"):
                best = hit["candidates"][0]
                resolved.append({"id": best["id"], "name": best.get("name", ""), "input": t})
            else:
                unresolved.append({"input": t, "reason": "not_found", "candidates": []})
    else:
        resolved = [{"id": cid, "name": name, "input": ""} for cid, name in name_by_id.items()]

    per_cap = limit if len(resolved) <= 1 else min(limit, per_target_limit)
    cand_key = _cursor_key(cursor) if cursor else None
    matched = 0
    collected: list[dict] = []

    for tgt in resolved:
        cid = tgt["id"]
        tables = resolve_tables(src, cid)
        if not tables:
            unresolved.append(
                {"input": tgt["input"] or cid, "reason": "no_message_table", "candidates": []}
            )
            continue
        if order == "desc":
            heap: list = []
        else:
            heap = []
        for db, tbl in tables:
            conn = connect_ro(db)
            try:
                sender_map = build_local_sender_map(conn)
                sql = (
                    'SELECT local_id, create_time, local_type, real_sender_id,'
                    ' message_content, WCDB_CT_message_content FROM "%s"' % tbl
                )
                where, args = [], []
                if lo is not None:
                    where.append("create_time >= ?")
                    args.append(lo)
                if hi is not None:
                    where.append("create_time <= ?")
                    args.append(hi)
                if where:
                    sql += " WHERE " + " AND ".join(where)
                for r in conn.execute(sql, args):
                    row = _row(db, tbl, r, sender_map, my_user, contacts)
                    if sender:
                        s = sender.lower()
                        if s not in (row["sender_id"].lower(), row["sender_name"].lower()):
                            continue
                    if wanted:
                        tag = row["type"] if row["type"] != "app" else f"app:{row['subtype']}"
                        if tag not in wanted and row["type"] not in wanted:
                            continue
                    if kw and kw not in (row["text"] or "").lower():
                        continue
                    matched += 1
                    key = _sort_key(row)
                    if cand_key is not None:
                        if order == "desc" and key >= cand_key:
                            continue
                        if order == "asc" and key <= cand_key:
                            continue
                    if order == "desc":
                        if len(heap) < per_cap:
                            heapq.heappush(heap, (key, row))
                        elif key > heap[0][0]:
                            heapq.heapreplace(heap, (key, row))
                    else:
                        wrapped = _Rev(key)
                        if len(heap) < per_cap:
                            heapq.heappush(heap, (wrapped, row))
                        elif key < heap[0][0].key:
                            heapq.heapreplace(heap, (_Rev(key), row))
            finally:
                conn.close()
        rows = [x[1] for x in heap]
        rows.sort(key=_sort_key, reverse=(order == "desc"))
        for row in rows:
            row["chat_id"] = cid
            row["chat_name"] = tgt["name"] or name_by_id.get(cid, cid)
            collected.append(row)

    collected.sort(key=_sort_key, reverse=(order == "desc"))
    page = collected[:limit]
    has_more = matched > len(page)
    next_cursor = None
    if page and has_more:
        k = _sort_key(page[-1])
        next_cursor = f"{k[0]}|{k[1]}|{k[2]}|{k[3]}"
    for row in page:
        row.pop("_db", None)
        row.pop("_tbl", None)
        row.pop("ts", None)
    return {
        "total": matched,
        "returned": len(page),
        "has_more": has_more,
        "next_cursor": next_cursor,
        "data_cutoff": src.data_cutoff(),
        "messages": page,
        "unresolved": unresolved,
    }

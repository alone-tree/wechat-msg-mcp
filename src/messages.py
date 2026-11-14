"""消息查询。

依据 docs/重构方案-2026-11-13.md 第 6.2 节：
- 默认最新优先（order=desc），导出时用 asc
- 返回 total、returned、has_more、next_cursor、data_cutoff
- 目标名解析失败返回 unresolved 与候选，不返回空结果冒充无消息
- 游标为 (ts, db, tbl, local_id) 四元组，跨分库翻页不重复不遗漏
- 不指定目标时限定时间窗（默认最近 30 天），避免全库逐表扫描
"""
from __future__ import annotations

import heapq
import re
import sqlite3
import time
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
_XML_MD5 = re.compile(r"<md5>([0-9a-f]{32})</md5>")
_VOICE_LEN = re.compile(r'voicelength="(\d+)"')
_VIDEO_LEN = re.compile(r'playlength="(\d+)"')
_TITLE_RE = re.compile(r"<title>(.*?)</title>", re.S)
_DES_RE = re.compile(r"<des>(.*?)</des>", re.S)
_TAG_RE = re.compile(r"<[^>]+>")
_WS_RE = re.compile(r"\s+")
_DEFAULT_WINDOW_DAYS = 30

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

_MAIN_OF_ALIAS = {
    "text": {1},
    "image": {3},
    "voice": {34},
    "video": {43},
    "emoticon": {47},
    "location": {48},
    "file": {49},
    "link": {49},
    "mini_program": {49},
    "quote": {49},
    "chat_record": {49},
    "channels": {49},
    "system": {10000},
    "revoked": {10002},
}


class _Rev:
    """把 heapq 变成大顶堆。"""

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


def _distill(text: str) -> str:
    """把 appmsg/引用类 XML 压成可读摘要，避免整段 XML 占响应体。"""
    if not text:
        return ""
    parts = [p.strip() for p in _TITLE_RE.findall(text) + _DES_RE.findall(text) if p.strip()]
    if parts:
        return " / ".join(parts)[:300]
    plain = _WS_RE.sub(" ", _TAG_RE.sub(" ", text)).strip()
    return plain[:300]


def _media(src: LocalSource, text: str, type_name: str, subtype: Optional[str], chat_id: str = "") -> Optional[dict]:
    if subtype == "file":
        from .media_files import resolve as resolve_file

        m = _XML_MD5.search(text or "")
        info = resolve_file(src, m.group(1) if m else "", chat_id)
        if info:
            return info
        title = next((p.strip() for p in _TITLE_RE.findall(text or "") if p.strip()), "")
        return {
            "kind": "file",
            "name": title,
            "size": 0,
            "path": "",
            "exists": False,
            "note": "本地未落盘或未命中 hardlink",
        }
    if type_name == "image":
        m = _IMG_MD5.search(text or "")
        return {"kind": "image", "md5": m.group(1) if m else ""}
    if type_name == "voice":
        m = _VOICE_LEN.search(text or "")
        return {"kind": "voice", "duration": (int(m.group(1)) // 1000) if m else None}
    if type_name == "video":
        m = _VIDEO_LEN.search(text or "")
        return {"kind": "video", "duration": int(m.group(1)) if m else None}
    if type_name == "emoticon":
        return {"kind": "emoticon"}
    return None


def resolve_tables(src: LocalSource, chat_id: str) -> list[tuple[Path, str]]:
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


def _wanted_types(types: Optional[Iterable[str]]) -> tuple[Optional[set[str]], Optional[set[int]]]:
    if not types:
        return None, None
    tags: set[str] = set()
    mains: set[int] = set()
    for t in types:
        key = str(t).strip().lower()
        tags.update(_TYPE_ALIASES.get(key, [key]))
        mains.update(_MAIN_OF_ALIAS.get(key, set()))
    return tags or None, mains or None


def _my_username() -> str:
    try:
        from .config import get_account_wxid

        return get_account_wxid()
    except Exception:
        return ""


def _build_row(db: Path, table: str, r, sender_map: dict, my_user: str, contacts: ContactIndex) -> dict:
    text = _content(r["message_content"], r["WCDB_CT_message_content"])
    type_name, subtype = classify(r["local_type"], text)
    sid, is_me = resolve_sender(r["real_sender_id"], sender_map, my_user)
    if type_name in ("text", "system", "app", "quote"):
        head, sep, rest = text.partition("\n")
        if sep and head.endswith(":") and re.match(r"^[\w@._-]+$", head[:-1]):
            prefix_id = head[:-1]
            text = rest
            if not sid:
                sid = prefix_id
                is_me = bool(my_user) and prefix_id == my_user
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
        "local_type": int(r["local_type"] or 0),
        "text": _distill(text) if type_name in ("app", "quote") else (text if type_name in ("text", "system") else ""),
        "media": _media_dummy(type_name, subtype),
        "_db": db.name,
        "_tbl": table,
        "_raw": text,
    }


def _media_dummy(type_name: str, subtype: Optional[str]) -> Optional[dict]:
    if subtype == "file":
        return {"kind": "file"}
    if type_name in ("image", "voice", "video", "emoticon"):
        return {"kind": type_name}
    return None


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
    include_media: bool = False,
) -> dict:
    from . import index
    from .entities import load_contacts, resolve_name

    limit = max(1, min(int(limit), 1000))
    order = "asc" if str(order).lower() == "asc" else "desc"
    lo, hi = _ts(start), _ts_end(end)
    default_window = 0
    if not targets and lo is None and hi is None:
        default_window = _DEFAULT_WINDOW_DAYS
        lo = int(time.time()) - default_window * 86400
    tags, mains = _wanted_types(types)
    kw = keyword.strip().lower()
    my_user = _my_username()

    contact_rows = load_contacts(src)
    contacts = ContactIndex()
    name_by_id: dict[str, str] = {}
    for c in contact_rows:
        contacts.add({"id": c.get("id"), **c})
        name_by_id[c["username"]] = c.get("remark") or c.get("nickname") or c["username"]

    unresolved: list[dict] = []
    jobs: list[tuple[Optional[str], Optional[str], Path, str]] = []
    if targets:
        for t in targets:
            hit = resolve_name(src, t, limit=3)
            if not hit.get("candidates"):
                unresolved.append({"input": t, "reason": "not_found", "candidates": []})
                continue
            best = hit["candidates"][0]
            cid = best["id"]
            tables = resolve_tables(src, cid)
            if not tables:
                unresolved.append(
                    {"input": t, "reason": "no_message_table", "candidates": hit["candidates"]}
                )
                continue
            for db, tbl in tables:
                jobs.append((cid, best.get("name", ""), db, tbl))
    else:
        pairs = index.tables_in_range(src, lo, hi, mains)
        if pairs is None:
            pairs = [(db.name, tbl) for db in src.message_dbs() for tbl in _tables_of(db)]
        db_by_name = {db.name: db for db in src.message_dbs()}
        for db_name, tbl in pairs:
            db = db_by_name.get(db_name)
            if db:
                jobs.append((None, None, db, tbl))

    target_ids = {j[0] for j in jobs if j[0]}
    multi = len(target_ids) > 1
    per_cap = limit if len(target_ids) <= 1 else min(limit, per_target_limit)
    cand_key = _cursor_key(cursor) if cursor else None
    matched = 0
    heaps: dict[str, list] = {}
    db_cache: dict[str, tuple[dict, dict]] = {}
    media_budget = {"image": 8, "voice": 8}

    for cid, cname, db, tbl in jobs:
        conn = connect_ro(db)
        try:
            cached = db_cache.get(db.name)
            if cached is None:
                md5_to_user = {}
                for col in ("user_name", "userName"):
                    try:
                        for _rid, name in conn.execute(f"SELECT rowid, {col} FROM Name2Id"):
                            if name:
                                md5_to_user[username_to_md5(name)] = name
                        break
                    except sqlite3.OperationalError:
                        continue
                cached = (md5_to_user, build_local_sender_map(conn))
                db_cache[db.name] = cached
            md5_to_user, sender_map = cached
            if cid is None:
                cid = md5_to_user.get(tbl[4:] if tbl.startswith("Msg_") else "")
                cname = name_by_id.get(cid, cid) if cid else ""
            bucket_key = (cid if targets else "__all__") or "__all__"
            bucket = heaps.setdefault(bucket_key, [])
            cap = per_cap if (multi and targets) else limit
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
                row = _build_row(db, tbl, r, sender_map, my_user, contacts)
                row["chat_id"] = cid or ""
                row["chat_name"] = cname or ""
                if sender:
                    s = sender.lower()
                    if s not in (row["sender_id"].lower(), row["sender_name"].lower()):
                        continue
                if tags:
                    tag = row["type"] if row["type"] != "app" else f"app:{row['subtype']}"
                    if tag not in tags and row["type"] not in tags:
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
                cap = cap
                if order == "desc":
                    if len(bucket) < cap:
                        heapq.heappush(bucket, (key, row))
                    elif key > bucket[0][0]:
                        heapq.heapreplace(bucket, (key, row))
                else:
                    if len(bucket) < cap:
                        heapq.heappush(bucket, (_Rev(key), row))
                    elif key < bucket[0][0].key:
                        heapq.heapreplace(bucket, (_Rev(key), row))
        finally:
            conn.close()

    rows = [item[1] for bucket_rows in heaps.values() for item in bucket_rows]
    rows.sort(key=_sort_key, reverse=(order == "desc"))
    page = rows[:limit]
    last_key = _sort_key(page[-1]) if page and matched > len(page) else None
    next_cursor = (
        f"{last_key[0]}|{last_key[1]}|{last_key[2]}|{last_key[3]}" if last_key else None
    )
    for row in page:
        if not row["chat_name"]:
            row["chat_name"] = name_by_id.get(row["chat_id"], row["chat_id"])
        if row["media"] and row["media"].get("kind") == "file":
            row["media"] = _media(
                src, row.get("_raw", ""), row["type"], row["subtype"], row["chat_id"]
            )
        if include_media and row.get("media"):
            from . import media_inline

            media_inline.enrich(src, row, row.get("_raw", ""), media_budget)
        row.pop("_raw", None)
        row.pop("_db", None)
        row.pop("_tbl", None)
        row.pop("ts", None)
    has_more = matched > len(page)
    return {
        "total": matched,
        "returned": len(page),
        "has_more": has_more,
        "next_cursor": next_cursor,
        "default_window_days": default_window,
        "data_cutoff": src.data_cutoff("message"),
        "messages": page,
        "unresolved": unresolved,
    }


def _tables_of(db: Path) -> list[str]:
    conn = connect_ro(db)
    try:
        return [
            r[0]
            for r in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name LIKE 'Msg_%'"
            )
        ]
    finally:
        conn.close()

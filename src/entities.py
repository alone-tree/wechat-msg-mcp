"""实体查询：会话、联系人、群。

统一的人与群模型，字段来源见 docs/重构方案-2026-11-13.md 第 5 节。
本模块只做查询，不写任何文件。
"""
from __future__ import annotations

import difflib
import time
from datetime import datetime
from typing import Optional

import zstandard as zstd

from .db.locator import LocalSource
from .db.open_ import connect_ro, table_exists

_ZSTD = zstd.ZstdDecompressor()


def _fmt(ts: Optional[int]) -> str:
    try:
        ts = int(ts or 0)
    except (TypeError, ValueError):
        return ""
    return datetime.fromtimestamp(ts).strftime("%Y-%m-%d %H:%M:%S") if ts > 0 else ""


def _text(value) -> str:
    if value is None:
        return ""
    if isinstance(value, bytes):
        try:
            return _ZSTD.decompress(value).decode("utf-8", "replace")
        except Exception:
            try:
                return value.decode("utf-8", "replace")
            except Exception:
                return ""
    return str(value)


def load_contacts(src: LocalSource) -> list[dict]:
    """读取 contact 表。列名在个别版本有差异，按实际列取。"""
    db = src.path("contact")
    if not db.exists():
        return []
    conn = connect_ro(db)
    try:
        if not table_exists(conn, "contact"):
            return []
        cols = {r["name"] for r in conn.execute("PRAGMA table_info(contact)")}
        nick_col = "nick_name" if "nick_name" in cols else ("nickname" if "nickname" in cols else None)
        select = ["id", "username", "local_type"]
        for c in ("alias", "remark", "description", "extra_buffer"):
            if c in cols:
                select.append(c)
        if nick_col:
            select.append(nick_col)
        rows = conn.execute(
            f"SELECT {', '.join(select)} FROM contact WHERE username IS NOT NULL"
            " AND username != '' AND username NOT LIKE '@placeholder%'"
        ).fetchall()
        out = []
        for r in rows:
            username = r["username"] or ""
            extra = _extra_fields(r["extra_buffer"] if "extra_buffer" in r.keys() else None)
            out.append(
                {
                    "id": r["id"],
                    "username": username,
                    "kind": "group" if username.endswith("@chatroom") else "person",
                    "local_type": r["local_type"],
                    "is_friend": (r["local_type"] == 1) if r["local_type"] is not None else None,
                    "alias": (r["alias"] if "alias" in r.keys() else "") or "",
                    "remark": (r["remark"] if "remark" in r.keys() else "") or "",
                    "nickname": (r[nick_col] if nick_col else "") or "",
                    "description": (r["description"] if "description" in r.keys() else "") or "",
                    "region": extra.get("region", ""),
                    "add_time": extra.get("add_time", ""),
                }
            )
        return out
    finally:
        conn.close()


def _extra_fields(blob) -> dict:
    """解析 contact.extra_buffer 的 protobuf，取地区与添加时间。

    实测字段：5=国家、6=省、7=市（英文名），41=添加时间 epoch。
    解析失败一律返回空，不抛错。
    """
    if not blob or not isinstance(blob, (bytes, bytearray)):
        return {}
    fields: dict[int, list] = {}
    buf = bytes(blob)
    i = 0
    n = len(buf)
    try:
        while i < n:
            key = 0
            shift = 0
            while True:
                b = buf[i]
                i += 1
                key |= (b & 0x7F) << shift
                shift += 7
                if not b & 0x80:
                    break
            fn, wt = key >> 3, key & 7
            if wt == 0:
                v = 0
                shift = 0
                while True:
                    b = buf[i]
                    i += 1
                    v |= (b & 0x7F) << shift
                    shift += 7
                    if not b & 0x80:
                        break
                fields.setdefault(fn, []).append(v)
            elif wt == 2:
                ln = 0
                shift = 0
                while True:
                    b = buf[i]
                    i += 1
                    ln |= (b & 0x7F) << shift
                    shift += 7
                    if not b & 0x80:
                        break
                fields.setdefault(fn, []).append(buf[i : i + ln])
                i += ln
            elif wt == 5:
                i += 4
            elif wt == 1:
                i += 8
            else:
                break
    except Exception:
        return {}

    def text(fn: int) -> str:
        for v in fields.get(fn, []):
            if isinstance(v, (bytes, bytearray)):
                try:
                    s = v.decode("utf-8")
                except Exception:
                    continue
                if s and all(ord(ch) >= 0x20 for ch in s):
                    return s
        return ""

    province = text(6)
    city = text(7)
    region = " ".join(x for x in (province, city) if x)
    add_time = ""
    for v in fields.get(41, []):
        if isinstance(v, int) and 1000000000 < v < 2200000000:
            add_time = _fmt(v)
            break
    return {"region": region, "add_time": add_time}


def group_meta(src: LocalSource) -> dict[str, dict]:
    """群维度补充：成员数、群主、群公告。"""
    db = src.path("contact")
    meta: dict[str, dict] = {}
    if not db.exists():
        return meta
    conn = connect_ro(db)
    try:
        if table_exists(conn, "chatroom_member"):
            counts = {
                int(room_id): cnt
                for room_id, cnt in conn.execute(
                    "SELECT room_id, COUNT(*) FROM chatroom_member GROUP BY room_id"
                )
                if room_id is not None
            }
            id_to_user = {
                int(r["id"]): r["username"]
                for r in conn.execute("SELECT id, username FROM contact WHERE username LIKE '%@chatroom'")
            }
            for room_id, cnt in counts.items():
                username = id_to_user.get(room_id)
                if username:
                    meta.setdefault(username, {})["member_count"] = cnt
        if table_exists(conn, "chat_room"):
            for r in conn.execute("SELECT username, owner FROM chat_room"):
                username = r["username"]
                if username:
                    meta.setdefault(username, {})["owner"] = r["owner"] or ""
        if table_exists(conn, "chat_room_info_detail"):
            for r in conn.execute(
                "SELECT username_, announcement_ FROM chat_room_info_detail"
                " WHERE announcement_ IS NOT NULL AND announcement_ != ''"
            ):
                username = r["username_"]
                if username:
                    meta.setdefault(username, {})["notice"] = _text(r["announcement_"])
        return meta
    finally:
        conn.close()


def session_meta(src: LocalSource) -> dict[str, dict]:
    """会话表：最后消息时间、未读、摘要。"""
    db = src.path("session")
    meta: dict[str, dict] = {}
    if not db.exists():
        return meta
    conn = connect_ro(db)
    try:
        if not table_exists(conn, "SessionTable"):
            return meta
        for r in conn.execute(
            "SELECT username, sort_timestamp, unread_count, summary FROM SessionTable"
        ):
            username = r["username"]
            if not username:
                continue
            meta[username] = {
                "last_time": r["sort_timestamp"] or 0,
                "unread": r["unread_count"] or 0,
                "last_message": _text(r["summary"])[:100],
            }
        return meta
    finally:
        conn.close()


def _entity(row: dict, gmeta: dict, smeta: dict) -> dict:
    username = row["username"]
    g = gmeta.get(username, {})
    s = smeta.get(username, {})
    return {
        "id": username,
        "kind": row["kind"],
        "name": row.get("remark") or row.get("nickname") or username,
        "remark": row.get("remark", ""),
        "nickname": row.get("nickname", ""),
        "alias": row.get("alias", ""),
        "is_friend": row.get("is_friend"),
        "region": row.get("region", ""),
        "add_time": row.get("add_time", ""),
        "member_count": g.get("member_count"),
        "owner_id": g.get("owner"),
        "notice": g.get("notice", ""),
        "last_active_at": _fmt(s.get("last_time")),
        "last_message": s.get("last_message", ""),
        "unread": s.get("unread", 0),
        "_last_ts": int(s.get("last_time") or 0),
    }


def list_entities(
    src: LocalSource,
    scope: str = "session",
    keyword: str = "",
    since_days: int = 0,
    only_group: bool = False,
    only_unread: bool = False,
    min_members: int = 0,
    max_members: int = 0,
    sort: str = "last_time",
    limit: int = 100,
    offset: int = 0,
) -> dict:
    contacts = load_contacts(src)
    gmeta = group_meta(src) if any(c["kind"] == "group" for c in contacts) else {}
    smeta = session_meta(src)
    by_user = {c["username"]: c for c in contacts}

    if scope == "session":
        rows = []
        for username, s in smeta.items():
            base = by_user.get(username)
            if base is None:
                base = {
                    "id": None,
                    "username": username,
                    "kind": "group" if username.endswith("@chatroom") else "person",
                    "remark": "",
                    "nickname": username,
                    "alias": "",
                    "is_friend": None,
                }
            rows.append(_entity(base, gmeta, {username: s}))
    else:
        rows = [_entity(c, gmeta, smeta) for c in contacts]
        if scope == "group":
            rows = [r for r in rows if r["kind"] == "group"]
        elif scope == "contact":
            rows = [r for r in rows if r["kind"] == "person"]

    kw = keyword.strip().lower()
    for r in rows:
        oid = r.get("owner_id") or ""
        owner = by_user.get(oid) if oid else None
        r["owner_name"] = (
            (owner.get("remark") or owner.get("nickname") or oid) if owner else ""
        )
    if kw:
        rows = [
            r
            for r in rows
            if kw in (r["name"] + "|" + r["nickname"] + "|" + r["id"] + "|" + r["remark"]).lower()
        ]
    if only_group:
        rows = [r for r in rows if r["kind"] == "group"]
    if only_unread:
        rows = [r for r in rows if r["unread"] > 0]
    if since_days > 0:
        floor = int(time.time()) - since_days * 86400
        rows = [r for r in rows if r["_last_ts"] >= floor]
    if min_members > 0:
        rows = [r for r in rows if (r["member_count"] or 0) >= min_members]
    if max_members > 0:
        rows = [r for r in rows if 0 < (r["member_count"] or 0) <= max_members]

    if sort == "members":
        rows.sort(key=lambda r: (r["member_count"] or 0), reverse=True)
    elif sort == "name":
        rows.sort(key=lambda r: r["name"])
    else:
        rows.sort(key=lambda r: r["_last_ts"], reverse=True)

    total = len(rows)
    page = rows[offset : offset + limit]
    for r in page:
        r.pop("_last_ts", None)
    return {
        "scope": scope,
        "data_cutoff": src.data_cutoff(),
        "total": total,
        "returned": len(page),
        "has_more": offset + len(page) < total,
        "next_offset": offset + len(page) if offset + len(page) < total else None,
        "entities": page,
    }


def resolve_name(src: LocalSource, query: str, limit: int = 5) -> dict:
    """名称解析：精确、包含、相似三级，返回候选而不是空结果。"""
    contacts = load_contacts(src)
    gmeta = group_meta(src)
    q = (query or "").strip()
    out: list[dict] = []
    if not q:
        return {"query": q, "matched": False, "data_cutoff": src.data_cutoff(), "candidates": []}

    fields = ("id", "name", "remark", "nickname")
    exact = [c for c in contacts if q in (c["username"], c["remark"], c["nickname"])]
    if exact:
        out = [{"id": c["username"], "name": c["remark"] or c["nickname"] or c["username"],
                "kind": c["kind"], "matched": "exact",
                "member_count": gmeta.get(c["username"], {}).get("member_count")} for c in exact[:limit]]
        return {"query": q, "matched": True, "data_cutoff": src.data_cutoff(), "candidates": out}

    ql = q.lower()
    contains = [
        c
        for c in contacts
        if ql in ((c["remark"] or "") + (c["nickname"] or "") + c["username"]).lower()
    ]
    if contains:
        out = [{"id": c["username"], "name": c["remark"] or c["nickname"] or c["username"],
                "kind": c["kind"], "matched": "contains",
                "member_count": gmeta.get(c["username"], {}).get("member_count")} for c in contains[:limit]]
        return {"query": q, "matched": True, "data_cutoff": src.data_cutoff(), "candidates": out}

    names = [(c["remark"] or c["nickname"] or c["username"], c) for c in contacts]
    close = difflib.get_close_matches(q, [n for n, _ in names], n=limit, cutoff=0.4)
    picked = {n for n in close}
    out = [{"id": c["username"], "name": n, "kind": c["kind"], "matched": "similar",
            "member_count": gmeta.get(c["username"], {}).get("member_count")}
           for n, c in names if n in picked]
    return {"query": q, "matched": bool(out), "data_cutoff": src.data_cutoff(), "candidates": out}

"""实体查询：会话、联系人、群。

统一的人与群模型，字段来源见 docs/重构方案-2026-11-13.md 第 5 节。
群名兜底见 docs/重构方案-消息模型与输出规范-2026-11-14.md 第 4 节：
备注 > 群昵称 > 成员昵称拼接 > 「群聊」，用 name_source 标明名字来源。
本模块只做查询，不写任何文件；联系人、群与会话元信息按库文件时间缓存。
"""
from __future__ import annotations

import difflib
import time
from datetime import datetime
from pathlib import Path
from typing import Optional

import zstandard as zstd

from .db.locator import LocalSource
from .db.open_ import connect_ro, table_exists

_ZSTD = zstd.ZstdDecompressor()

# 按 (文件路径, mtime, size) 缓存：解密库一更新就自动失效
_CACHE: dict[tuple, object] = {}
_NAME_CACHE: dict[tuple, dict] = {}
_DIGEST_HEAD = 3
_DIGEST_KEEP = 6


def _cache_key(path: Path) -> tuple:
    try:
        st = path.stat()
        return (str(path), st.st_mtime, st.st_size)
    except OSError:
        return (str(path), 0.0, 0)


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
    key = ("contacts",) + _cache_key(db)
    cached = _CACHE.get(key)
    if isinstance(cached, list):
        return cached
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
                    "gender": extra.get("gender", ""),
                    "add_time": extra.get("add_time", ""),
                }
            )
        _CACHE[key] = out
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
    # 字段 2 是性别枚举：1 男、2 女、0 未设置。实测好友全覆盖，群成员约 78%。
    gender = ""
    for v in fields.get(2, []):
        if isinstance(v, int):
            if v == 1:
                gender = "男"
                break
            if v == 2:
                gender = "女"
                break
    add_time = ""
    for v in fields.get(41, []):
        if isinstance(v, int) and 1000000000 < v < 2200000000:
            add_time = _fmt(v)
            break
    return {"region": region, "add_time": add_time, "gender": gender}


def group_meta(src: LocalSource) -> dict[str, dict]:
    """群维度补充：成员数、群主、群公告。"""
    db = src.path("contact")
    if not db.exists():
        return {}
    key = ("group_meta",) + _cache_key(db)
    cached = _CACHE.get(key)
    if isinstance(cached, dict):
        return cached
    meta: dict[str, dict] = {}
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
        _CACHE[key] = meta
        return meta
    finally:
        conn.close()


def session_meta(src: LocalSource) -> dict[str, dict]:
    """会话表：最后消息时间、未读、摘要。"""
    db = src.path("session")
    if not db.exists():
        return {}
    key = ("session_meta",) + _cache_key(db)
    cached = _CACHE.get(key)
    if isinstance(cached, dict):
        return cached
    meta: dict[str, dict] = {}
    conn = connect_ro(db)
    try:
        if not table_exists(conn, "SessionTable"):
            return {}
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
        _CACHE[key] = meta
        return meta
    finally:
        conn.close()


def _own_wxid(src: LocalSource) -> str:
    try:
        from .config import get_account_wxid

        return get_account_wxid()
    except Exception:
        return ""


def group_digest(src: LocalSource, room_ids: list[int], contacts: list[dict], my_user: str) -> dict[int, list[str]]:
    """群 id -> 成员昵称（按成员表顺序，去掉自己）。只算传入的群，代价可控。"""
    if not room_ids:
        return {}
    db = src.path("contact")
    if not db.exists():
        return {}
    id_to_user = {int(c["id"]): c["username"] for c in contacts if c.get("id")}
    user_to_name = {c["username"]: (c.get("remark") or c.get("nickname") or "") for c in contacts}
    out: dict[int, list[str]] = {}
    conn = connect_ro(db)
    try:
        if not table_exists(conn, "chatroom_member"):
            return {}
        for room_id in room_ids:
            names = []
            for (member_id,) in conn.execute(
                "SELECT member_id FROM chatroom_member WHERE room_id = ?", (room_id,)
            ):
                username = id_to_user.get(int(member_id or 0), "")
                if not username or (my_user and username == my_user):
                    continue
                name = user_to_name.get(username) or ""
                if name and name not in names:
                    names.append(name)
            out[room_id] = names
        return out
    finally:
        conn.close()


def display_names(src: LocalSource, contacts: Optional[list[dict]] = None) -> dict[str, dict]:
    """会话 id -> 显示名与来源。群名缺失时用成员昵称拼，与客户端显示效果一致。"""
    db = src.path("contact")
    key = ("names",) + _cache_key(db)
    cached = _NAME_CACHE.get(key)
    if isinstance(cached, dict):
        return cached
    rows = contacts if contacts is not None else load_contacts(src)
    gmeta = group_meta(src)
    my_user = _own_wxid(src)
    unnamed = [
        c
        for c in rows
        if c["kind"] == "group" and not (c.get("remark") or c.get("nickname")) and c.get("id")
    ]
    digests = group_digest(src, [int(c["id"]) for c in unnamed], rows, my_user)
    names: dict[str, dict] = {}
    for c in rows:
        username = c["username"]
        remark, nickname = c.get("remark") or "", c.get("nickname") or ""
        if remark:
            names[username] = {"name": remark, "source": "remark"}
            continue
        if nickname:
            names[username] = {"name": nickname, "source": "nickname"}
            continue
        if c["kind"] == "person":
            names[username] = {"name": username, "source": "id"}
            continue
        members = digests.get(int(c["id"]), []) if c.get("id") else []
        total = (gmeta.get(username) or {}).get("member_count") or len(members)
        if members:
            head = members[:_DIGEST_HEAD]
            label = "、".join(head)
            if total and total > len(head):
                label += f"等{total}人"
            names[username] = {
                "name": label,
                "source": "members",
                "digest": members[:_DIGEST_KEEP],
            }
        else:
            names[username] = {"name": "群聊", "source": "fallback"}
    _NAME_CACHE[key] = names
    return names


def entity_name(names: dict[str, dict], username: str) -> str:
    return (names.get(username) or {}).get("name") or username


def _entity(row: dict, gmeta: dict, smeta: dict, names: dict[str, dict]) -> dict:
    username = row["username"]
    g = gmeta.get(username, {})
    s = smeta.get(username, {})
    named = names.get(username) or {}
    return {
        "id": username,
        "kind": row["kind"],
        "name": named.get("name") or row.get("remark") or row.get("nickname") or username,
        "name_source": named.get("source") or "id",
        "remark": row.get("remark", ""),
        "nickname": row.get("nickname", ""),
        "alias": row.get("alias", ""),
        "is_friend": row.get("is_friend"),
        "region": row.get("region", ""),
        "gender": row.get("gender", ""),
        "add_time": row.get("add_time", ""),
        "member_count": g.get("member_count"),
        "owner_id": g.get("owner"),
        "owner_name": "",
        "notice": g.get("notice", ""),
        "last_active_at": _fmt(s.get("last_time")),
        "last_message": s.get("last_message", ""),
        "unread": s.get("unread", 0),
        "members_digest": named.get("digest", []) if named.get("source") == "members" else [],
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
    detail: str = "lite",
) -> dict:
    if scope not in ("session", "contact", "group", "all"):
        return {
            "ok": False,
            "error": {
                "code": "unknown_scope",
                "message": f"未知 scope: {scope}",
                "candidates": ["session", "contact", "group", "all"],
            },
            "hint": "写错取值不会自动回退，避免静默返回一份看起来正常的结果",
        }
    detail = str(detail or "lite").lower()
    contacts = load_contacts(src)
    gmeta = group_meta(src) if any(c["kind"] == "group" for c in contacts) else {}
    smeta = session_meta(src)
    names = display_names(src, contacts)
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
                    "nickname": "",
                    "alias": "",
                    "is_friend": None,
                }
            rows.append(_entity(base, gmeta, {username: s}, names))
    else:
        rows = [_entity(c, gmeta, smeta, names) for c in contacts]
        if scope == "group":
            rows = [r for r in rows if r["kind"] == "group"]
        elif scope == "contact":
            rows = [r for r in rows if r["kind"] == "person"]

    kw = keyword.strip().lower()
    for r in rows:
        oid = r.get("owner_id") or ""
        r["owner_name"] = entity_name(names, oid) if oid else ""
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
    has_more = offset + len(page) < total
    for r in page:
        r.pop("_last_ts", None)
        if not r.get("members_digest"):
            r.pop("members_digest", None)
        if detail == "lite":
            r.pop("region", None)
            r.pop("gender", None)
            r.pop("add_time", None)
            r.pop("notice", None)
    return {
        "ok": True,
        "detail": detail,
        "scope": scope,
        "data_until": src.data_until("session"),
        "total": total,
        "returned": len(page),
        "has_more": has_more,
        "next": offset + len(page) if has_more else None,
        "entities": page,
    }


_LOOKUP_CACHE: dict[tuple, dict[str, list[str]]] = {}


def _lookup_index(src: LocalSource, contacts: list[dict], names: dict[str, dict]) -> dict[str, list[str]]:
    """名字 -> wxid 的倒排表，精确命中走它，不再每次扫 3 万行。"""
    key = ("lookup",) + _cache_key(src.path("contact"))
    cached = _LOOKUP_CACHE.get(key)
    if cached is not None:
        return cached
    index: dict[str, list[str]] = {}
    for c in contacts:
        username = c["username"]
        for value in (username, c.get("remark") or "", c.get("nickname") or "", entity_name(names, username)):
            if not value:
                continue
            bucket = index.setdefault(value, [])
            if username not in bucket:
                bucket.append(username)
    _LOOKUP_CACHE.clear()
    _LOOKUP_CACHE[key] = index
    return index


def resolve_name(src: LocalSource, query: str, limit: int = 5) -> dict:
    """名称解析：精确、包含、相似三级，返回候选而不是空结果。"""
    contacts = load_contacts(src)
    gmeta = group_meta(src)
    names = display_names(src, contacts)
    q = (query or "").strip()
    if not q:
        return {"query": q, "matched": False, "data_until": src.data_until("session"), "candidates": []}

    def cand(c: dict, matched: str) -> dict:
        return {
            "id": c["username"],
            "name": entity_name(names, c["username"]),
            "kind": c["kind"],
            "matched": matched,
            "member_count": gmeta.get(c["username"], {}).get("member_count"),
        }

    index = _lookup_index(src, contacts, names)
    by_username = {c["username"]: c for c in contacts}
    exact = [by_username[u] for u in index.get(q, []) if u in by_username]
    if exact:
        out = [cand(c, "exact") for c in exact[:limit]]
        return {"query": q, "matched": True, "data_until": src.data_until("session"), "candidates": out}

    ql = q.lower()
    contains = [
        c
        for c in contacts
        if ql
        in (
            (c["remark"] or "") + (c["nickname"] or "") + c["username"] + entity_name(names, c["username"])
        ).lower()
    ]
    if contains:
        out = [cand(c, "contains") for c in contains[:limit]]
        return {"query": q, "matched": True, "data_until": src.data_until("session"), "candidates": out}

    pairs = [(entity_name(names, c["username"]), c) for c in contacts]
    close = difflib.get_close_matches(q, [n for n, _ in pairs], n=limit, cutoff=0.4)
    picked = set(close)
    out = [cand(c, "similar") for n, c in pairs if n in picked]
    return {
        "query": q,
        "matched": bool(out),
        "data_until": src.data_until("session"),
        "candidates": out,
    }


def _resolve_person(contacts: list[dict], names: dict[str, dict], query: str) -> tuple[str, list[dict]]:
    """把名字解析成 wxid，返回 (wxid, 歧义候选)。解析不到 wxid 为空。"""
    q = query.strip()
    if not q:
        return "", []
    if any(c["username"] == q for c in contacts):
        return q, []
    hits = [
        c["username"]
        for c in contacts
        if q in (c.get("remark") or "")
        or q in (c.get("nickname") or "")
        or entity_name(names, c["username"]) == q
        or q == (c.get("alias") or "")
    ]
    if len(hits) == 1:
        return hits[0], []
    if hits:
        return "", [{"id": u, "name": entity_name(names, u)} for u in hits[:5]]
    return "", []


def groups_by_members(
    src: LocalSource,
    members: list[str],
    limit: int = 20,
    require_all: bool = True,
) -> dict:
    """按成员组合找群：这些人同时在哪些群里。

    输入可以是名称、备注、昵称、微信号或 wxid，支持用逗号分隔。
    解析不到或有歧义的名字进 unresolved，不会当成「没有这个群」。
    """
    contacts = load_contacts(src)
    names = display_names(src, contacts)
    gmeta = group_meta(src)
    wanted: list[str] = []
    unresolved: list[dict] = []
    for raw in members or []:
        for part in str(raw).replace("，", ",").split(","):
            q = part.strip()
            if not q:
                continue
            username, ambiguous = _resolve_person(contacts, names, q)
            if username:
                if username not in wanted:
                    wanted.append(username)
            else:
                unresolved.append(
                    {
                        "input": q,
                        "reason": "ambiguous" if ambiguous else "not_found",
                        "candidates": ambiguous,
                    }
                )
    if not wanted:
        return {
            "ok": False,
            "error": {
                "code": "no_valid_members",
                "message": "没有一个成员名被解析到，无法按成员找群",
                "candidates": [],
            },
            "unresolved": unresolved,
        }

    db = src.path("contact")
    if not db.exists():
        return {"ok": False, "error": {"code": "no_contact_db", "message": "联系人库不存在"}}
    conn = connect_ro(db)
    try:
        rows = conn.execute("SELECT room_id, member_id FROM chatroom_member").fetchall()
    finally:
        conn.close()
    id_to_user = {int(c["id"]): c["username"] for c in contacts if c.get("id")}
    room_to_group = {
        int(c["id"]): c["username"] for c in contacts if c.get("id") and c["kind"] == "group"
    }
    room_members: dict[int, set[str]] = {}
    for room_id, member_id in rows:
        username = id_to_user.get(int(member_id or 0))
        if username:
            room_members.setdefault(int(room_id), set()).add(username)

    target = set(wanted)
    groups: list[dict] = []
    for room_id, members_set in room_members.items():
        hit = target & members_set
        if not hit:
            continue
        if require_all and hit != target:
            continue
        group_id = room_to_group.get(room_id)
        if not group_id:
            continue
        named = names.get(group_id) or {}
        groups.append(
            {
                "id": group_id,
                "name": named.get("name") or group_id,
                "name_source": named.get("source") or "id",
                "member_count": (gmeta.get(group_id) or {}).get("member_count"),
                "matched": [entity_name(names, u) for u in wanted if u in hit],
                "missing": [entity_name(names, u) for u in wanted if u not in hit],
            }
        )
    groups.sort(key=lambda g: (g["member_count"] or 0, g["name"]))
    total = len(groups)
    page = groups[:limit]
    return {
        "ok": True,
        "criteria": {
            "members": [entity_name(names, u) for u in wanted],
            "member_ids": wanted,
            "require_all": require_all,
        },
        "data_until": src.data_until("contact"),
        "total": total,
        "returned": len(page),
        "has_more": total > len(page),
        "next": None,
        "groups": page,
        "unresolved": unresolved,
    }

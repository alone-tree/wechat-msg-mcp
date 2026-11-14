"""关系查询：群成员、共同群、某人在多群的发言、活跃统计。

依据 docs/重构方案-2026-11-13.md 第 6.3 节。
跨群聚合走索引（文本消息为基数），单会话统计走源库精确扫描，
返回体用 basis 字段标明口径，不把近似值当成精确值。
"""
from __future__ import annotations

import sqlite3
import time
from datetime import datetime
from typing import Optional

from . import index
from .db.locator import LocalSource
from .db.open_ import connect_ro, table_exists
from .entities import _extra_fields


def _fmt(ts) -> str:
    try:
        return datetime.fromtimestamp(int(ts)).strftime("%Y-%m-%d %H:%M:%S")
    except Exception:
        return ""


def _resolve(src: LocalSource, subject: str) -> Optional[dict]:
    from .entities import resolve_name

    hit = resolve_name(src, subject, limit=1)
    if not hit.get("candidates"):
        return None
    return hit["candidates"][0]


def _resolve_kind(src: LocalSource, subject: str, kind: str | None) -> tuple[Optional[dict], list]:
    """关系查询只接受精确或包含级命中，且类型要对得上；kind 为空则不限制类型。"""
    from .entities import resolve_name

    hit = resolve_name(src, subject, limit=5)
    cands = hit.get("candidates", [])
    for c in cands:
        if c.get("matched") in ("exact", "contains") and (kind is None or c.get("kind") == kind):
            return c, cands
    return None, cands


def _fail(src: LocalSource, subject: str, message: str, kind: str, candidates: list | None = None) -> dict:
    from .entities import resolve_name

    cands = candidates if candidates is not None else resolve_name(src, subject, limit=5).get("candidates", [])
    return {
        "ok": False,
        "error": {"code": "subject_not_found", "message": message, "candidates": cands},
        "kind": kind,
        "subject": subject,
        "unresolved": [
            {
                "input": subject,
                "reason": "not_found" if not cands else "ambiguous",
                "candidates": cands,
            }
        ],
        "total": 0,
        "data_until": src.data_until("message"),
    }


def group_members(src: LocalSource, subject: str, limit: int = 500) -> dict:
    target, cands = _resolve_kind(src, subject, "group")
    if not target:
        return _fail(src, subject, "未找到该群", "group_members", cands)
    gid = target["id"]
    db = src.path("contact")
    conn = connect_ro(db)
    try:
        row = conn.execute(
            "SELECT id, remark, nick_name, extra_buffer FROM contact WHERE username=?", (gid,)
        ).fetchone()
        if not row:
            return _fail(src, subject, "联系人表里没有该群", "group_members")
        room_id = row["id"]
        owner = ""
        if table_exists(conn, "chat_room"):
            r2 = conn.execute("SELECT owner FROM chat_room WHERE username=?", (gid,)).fetchone()
            owner = (r2["owner"] or "") if r2 else ""
        total = 0
        members = []
        if table_exists(conn, "chatroom_member"):
            total = conn.execute(
                "SELECT COUNT(*) FROM chatroom_member WHERE room_id = ?", (room_id,)
            ).fetchone()[0]
            for m in conn.execute(
                "SELECT c.username, c.remark, c.nick_name, c.alias, c.local_type, c.description, c.extra_buffer"
                " FROM chatroom_member cm JOIN contact c ON c.id = cm.member_id"
                " WHERE cm.room_id = ? LIMIT ?",
                (room_id, limit),
            ):
                extra = _extra_fields(m["extra_buffer"] if "extra_buffer" in m.keys() else None)
                members.append(
                    {
                        "id": m["username"],
                        "name": m["remark"] or m["nick_name"] or m["username"],
                        "remark": m["remark"] or "",
                        "nickname": m["nick_name"] or "",
                        "alias": m["alias"] or "",
                        "relation": "好友" if m["local_type"] == 1 else "非好友",
                        "signature": m["description"] or "",
                        "gender": extra.get("gender", ""),
                        "region": extra.get("region", ""),
                    }
                )
        return {
            "ok": True,
            "kind": "group_members",
            "group": {"id": gid, "name": row["remark"] or row["nick_name"] or gid, "owner_id": owner},
            "total": total,
            "returned": len(members),
            "has_more": total > len(members),
            "next": None,
            "members": members,
            "data_until": src.data_until("message"),
        }
    finally:
        conn.close()


def common_groups(src: LocalSource, subject: str, limit: int = 200) -> dict:
    target, cands = _resolve_kind(src, subject, "person")
    if not target:
        return _fail(src, subject, "未找到该联系人", "common_groups", cands)
    person_id = target["id"]
    db = src.path("contact")
    conn = connect_ro(db)
    try:
        prow = conn.execute("SELECT id FROM contact WHERE username=?", (person_id,)).fetchone()
        if not prow:
            return _fail(src, subject, "联系人表里没有该人", "common_groups")
        pid = prow["id"]
        rows = conn.execute(
            "SELECT c.username, c.remark, c.nick_name FROM chatroom_member cm"
            " JOIN contact c ON c.id = cm.room_id WHERE cm.member_id = ?",
            (pid,),
        ).fetchall()
        gids = [r["username"] for r in rows]
        counts: dict[str, int] = {}
        if gids:
            idx = index.count(src)
            if idx.get("exists"):
                iconn = sqlite3.connect(
                    f"file:{index.index_path(src).as_posix()}?mode=ro", uri=True
                )
                try:
                    q = (
                        "SELECT chat_id, COUNT(*) FROM msg_meta WHERE chat_id IN (%s)"
                        " GROUP BY chat_id" % ",".join("?" * len(gids))
                    )
                    counts = {cid: n for cid, n in iconn.execute(q, gids)}
                finally:
                    iconn.close()
        groups = [
            {
                "id": r["username"],
                "name": r["remark"] or r["nick_name"] or r["username"],
                "messages": counts.get(r["username"], 0),
            }
            for r in rows
        ]
        groups.sort(key=lambda g: g["messages"], reverse=True)
        return {
            "ok": True,
            "kind": "common_groups",
            "person": {"id": person_id, "name": target.get("name", "")},
            "total": len(groups),
            "returned": len(groups[:limit]),
            "has_more": len(groups) > limit,
            "next": None,
            "groups": groups[:limit],
            "basis": "文本消息计数，来自本地索引",
            "data_until": src.data_until("message"),
        }
    finally:
        conn.close()


def person_activity(
    src: LocalSource,
    subject: str,
    targets: Optional[list[str]] = None,
    since: str = "",
    until: str = "",
    limit: int = 200,
) -> dict:
    from .messages import _ts, _ts_end

    target, cands = _resolve_kind(src, subject, "person")
    if not target:
        return _fail(src, subject, "未找到该联系人", "person_activity", cands)
    pid = target["id"]
    lo, hi = _ts(since), _ts_end(until)
    idx = index.count(src)
    if not idx.get("exists"):
        return {
            "ok": False,
            "error": {"code": "index_missing", "message": "索引尚未构建，先运行 python -m src.cli index"},
            "data_until": src.data_until("message"),
        }
    iconn = sqlite3.connect(f"file:{index.index_path(src).as_posix()}?mode=ro", uri=True)
    try:
        where = ["sender_id = ?"]
        args: list = [pid]
        if lo:
            where.append("ts >= ?")
            args.append(lo)
        if hi:
            where.append("ts <= ?")
            args.append(hi)
        chat_filter = None
        if targets:
            resolved = []
            for t in targets:
                hit = _resolve(src, t)
                if hit:
                    resolved.append(hit["id"])
            if not resolved:
                return {
                    "ok": False,
                    "error": {
                        "code": "targets_unresolved",
                        "message": "targets 里的会话一个都没解析到，不会退化成全部群",
                        "candidates": targets,
                    },
                    "data_until": src.data_until("message"),
                }
            where.append("chat_id IN (%s)" % ",".join("?" * len(resolved)))
            args.extend(resolved)
            chat_filter = resolved
        sql = (
            "SELECT chat_id, COUNT(*) AS n, MAX(ts) AS last FROM msg_meta WHERE "
            + " AND ".join(where)
            + " GROUP BY chat_id ORDER BY n DESC"
        )
        rows = iconn.execute(sql, args).fetchall()
    finally:
        iconn.close()
    from .entities import load_contacts

    names = {
        c["username"]: c.get("remark") or c.get("nickname") or c["username"]
        for c in load_contacts(src)
    }
    groups = [{"id": cid, "name": names.get(cid, cid), "messages": n, "last_at": _fmt(last)} for cid, n, last in rows]
    page = groups[:limit]
    return {
        "ok": True,
        "kind": "person_activity",
        "person": {"id": pid, "name": target.get("name", "")},
        "scope": {"targets": chat_filter or "all_groups"},
        "total": len(groups),
        "messages_total": sum(g["messages"] for g in groups),
        "returned": len(page),
        "has_more": len(groups) > len(page),
        "next": None,
        "groups": page,
        "basis": "文本消息计数，来自本地索引",
        "data_until": src.data_until("message"),
    }


def activity(src: LocalSource, subject: str, since: str = "", until: str = "") -> dict:
    """单会话精确统计：总数、按天、按小时、按类型、Top 发言人。"""
    from .db.sender import ContactIndex, build_local_sender_map
    from .messages import _content, _ts, _ts_end, resolve_tables
    from .db.types import classify

    target, cands = _resolve_kind(src, subject, None)
    if not target:
        return _fail(src, subject, "未找到该会话", "activity", cands)
    cid = target["id"]
    tables = resolve_tables(src, cid)
    if not tables:
        return {
            "ok": False,
            "error": {"code": "no_message_table", "message": "没有消息分表"},
            "subject": subject,
            "data_until": src.data_until("message"),
        }
    lo, hi = _ts(since), _ts_end(until)
    from .entities import load_contacts

    contacts = ContactIndex()
    for c in load_contacts(src):
        contacts.add({"id": c.get("id"), **c})
    total = 0
    by_day: dict[str, int] = {}
    by_hour = [0] * 24
    by_type: dict[str, int] = {}
    senders: dict[str, int] = {}
    for db, tbl in tables:
        conn = connect_ro(db)
        try:
            smap = build_local_sender_map(conn)
            sql = 'SELECT create_time, local_type, real_sender_id, message_content, WCDB_CT_message_content FROM "%s"' % tbl
            where, args = [], []
            if lo:
                where.append("create_time >= ?")
                args.append(lo)
            if hi:
                where.append("create_time <= ?")
                args.append(hi)
            if where:
                sql += " WHERE " + " AND ".join(where)
            for r in conn.execute(sql, args):
                ts = int(r["create_time"] or 0)
                if ts <= 0:
                    continue
                total += 1
                dt = datetime.fromtimestamp(ts)
                day = dt.strftime("%Y-%m-%d")
                by_day[day] = by_day.get(day, 0) + 1
                by_hour[dt.hour] += 1
                name, subtype = classify(r["local_type"], _content(r["message_content"], r["WCDB_CT_message_content"]))
                tag = name if name != "app" else f"app:{subtype}"
                by_type[tag] = by_type.get(tag, 0) + 1
                if tag in ("system", "revoked"):
                    continue
                rid = r["real_sender_id"]
                sender = smap.get(int(rid)) if rid not in (None, "") else ""
                disp = "我" if sender == "" else (contacts.name_of(sender) or sender or "未知")
                senders[disp] = senders.get(disp, 0) + 1
        finally:
            conn.close()
    top = sorted(senders.items(), key=lambda x: x[1], reverse=True)[:10]
    days = sorted(by_day.items())
    return {
        "ok": True,
        "kind": "activity",
        "chat": {"id": cid, "name": target.get("name", "")},
        "total": total,
        "by_day": [{"day": d, "count": n} for d, n in days],
        "by_hour": [{"hour": i, "count": n} for i, n in enumerate(by_hour)],
        "by_type": dict(sorted(by_type.items(), key=lambda x: x[1], reverse=True)),
        "top_senders": [{"name": n, "messages": c} for n, c in top],
        "basis": "精确扫描源库",
        "data_until": src.data_until("message"),
    }

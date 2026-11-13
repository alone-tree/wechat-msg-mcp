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


def group_members(src: LocalSource, subject: str, limit: int = 500) -> dict:
    target = _resolve(src, subject)
    if not target:
        return {"error": "未找到该群", "subject": subject, "data_cutoff": src.data_cutoff()}
    gid = target["id"]
    db = src.path("contact")
    conn = connect_ro(db)
    try:
        row = conn.execute(
            "SELECT id, remark, nick_name, extra_buffer FROM contact WHERE username=?", (gid,)
        ).fetchone()
        if not row:
            return {"error": "联系人表里没有该群", "subject": subject, "data_cutoff": src.data_cutoff()}
        room_id = row["id"]
        owner = ""
        if table_exists(conn, "chat_room"):
            r2 = conn.execute("SELECT owner FROM chat_room WHERE username=?", (gid,)).fetchone()
            owner = (r2["owner"] or "") if r2 else ""
        members = []
        if table_exists(conn, "chatroom_member"):
            for m in conn.execute(
                "SELECT c.username, c.remark, c.nick_name, c.alias, c.local_type, c.description"
                " FROM chatroom_member cm JOIN contact c ON c.id = cm.member_id"
                " WHERE cm.room_id = ? LIMIT ?",
                (room_id, limit),
            ):
                members.append(
                    {
                        "id": m["username"],
                        "name": m["remark"] or m["nick_name"] or m["username"],
                        "remark": m["remark"] or "",
                        "nickname": m["nick_name"] or "",
                        "alias": m["alias"] or "",
                        "relation": "好友" if m["local_type"] == 1 else "非好友",
                        "signature": m["description"] or "",
                    }
                )
        return {
            "kind": "group_members",
            "group": {"id": gid, "name": row["remark"] or row["nick_name"] or gid, "owner_id": owner},
            "total": len(members),
            "members": members,
            "data_cutoff": src.data_cutoff(),
        }
    finally:
        conn.close()


def common_groups(src: LocalSource, subject: str, limit: int = 200) -> dict:
    target = _resolve(src, subject)
    if not target:
        return {"error": "未找到该联系人", "subject": subject, "data_cutoff": src.data_cutoff()}
    person_id = target["id"]
    db = src.path("contact")
    conn = connect_ro(db)
    try:
        prow = conn.execute("SELECT id FROM contact WHERE username=?", (person_id,)).fetchone()
        if not prow:
            return {"error": "联系人表里没有该人", "subject": subject, "data_cutoff": src.data_cutoff()}
        pid = prow["id"]
        rows = conn.execute(
            "SELECT c.username, c.remark, c.nick_name FROM chatroom_member cm"
            " JOIN contact c ON c.id = cm.room_id WHERE cm.member_id = ? LIMIT ?",
            (pid, limit),
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
            "kind": "common_groups",
            "person": {"id": person_id, "name": target.get("name", "")},
            "total": len(groups),
            "groups": groups,
            "basis": "文本消息计数，来自本地索引",
            "data_cutoff": src.data_cutoff(),
        }
    finally:
        conn.close()


def person_activity(
    src: LocalSource,
    subject: str,
    targets: Optional[list[str]] = None,
    since: str = "",
    until: str = "",
) -> dict:
    from .messages import _ts, _ts_end

    target = _resolve(src, subject)
    if not target:
        return {"error": "未找到该联系人", "subject": subject, "data_cutoff": src.data_cutoff()}
    pid = target["id"]
    lo, hi = _ts(since), _ts_end(until)
    idx = index.count(src)
    if not idx.get("exists"):
        return {"error": "索引尚未构建，先运行 wechatq index", "data_cutoff": src.data_cutoff()}
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
            if resolved:
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
    return {
        "kind": "person_activity",
        "person": {"id": pid, "name": target.get("name", "")},
        "scope": {"targets": chat_filter or "all_groups"},
        "total": sum(g["messages"] for g in groups),
        "groups": groups,
        "basis": "文本消息计数，来自本地索引",
        "data_cutoff": src.data_cutoff(),
    }


def activity(src: LocalSource, subject: str, since: str = "", until: str = "") -> dict:
    """单会话精确统计：总数、按天、按小时、按类型、Top 发言人。"""
    from .db.sender import ContactIndex, build_local_sender_map
    from .messages import _content, _ts, _ts_end, resolve_tables
    from .db.types import classify

    target = _resolve(src, subject)
    if not target:
        return {"error": "未找到该会话", "subject": subject, "data_cutoff": src.data_cutoff()}
    cid = target["id"]
    tables = resolve_tables(src, cid)
    if not tables:
        return {"error": "没有消息分表", "subject": subject, "data_cutoff": src.data_cutoff()}
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
        "kind": "activity",
        "chat": {"id": cid, "name": target.get("name", "")},
        "total": total,
        "by_day": [{"day": d, "count": n} for d, n in days],
        "by_hour": by_hour,
        "by_type": dict(sorted(by_type.items(), key=lambda x: x[1], reverse=True)),
        "top_senders": [{"name": n, "messages": c} for n, c in top],
        "basis": "精确扫描源库",
        "data_cutoff": src.data_cutoff(),
    }

"""朋友圈查询。

依据 docs/重构方案-2026-11-13.md 第 18.1 节：
数据源 decrypted/sns/sns.db。SnsTimeLine.content 是 TimelineObject XML，
SnsMessage_tmp3 里 type=2 且 content 非空为评论，type=1 为点赞类记录。
"""
from __future__ import annotations

import re
import sqlite3
from datetime import datetime
from typing import Optional
from xml.etree import ElementTree as ET

from .db.locator import LocalSource
from .db.open_ import connect_ro

_CTRL = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f]")
_HTTP = re.compile(r"https?://[^\s<\"]+")
_LIKE_USER = re.compile(r"<user_comment>.*?</user_comment>", re.S)
_NICK = re.compile(r"<nickname>(.*?)</nickname>", re.S)


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


def _parse(content: str) -> dict:
    out = {"time": 0, "text": "", "media": [], "likes": []}
    if not content:
        return out
    cleaned = _CTRL.sub("", content)
    try:
        root = ET.fromstring(cleaned)
    except ET.ParseError:
        m = re.search(r"<createTime>(\d+)</createTime>", cleaned)
        if m:
            out["time"] = int(m.group(1))
        m = re.search(r"<contentDesc>(.*?)</contentDesc>", cleaned, re.S)
        if m:
            out["text"] = m.group(1)
        return out
    t = root.find(".//TimelineObject")
    node = t if t is not None else root
    out["time"] = int(node.findtext("createTime") or 0)
    out["text"] = node.findtext("contentDesc") or ""
    for media in node.findall(".//mediaList/media"):
        kind = media.findtext("type") or ""
        url = ""
        for child in media.iter():
            if child.text and _HTTP.match(child.text.strip()):
                url = child.text.strip()
                break
        if url:
            out["media"].append({"type": kind, "url": url})
    for uc in node.findall(".//like_user_list/user_comment"):
        nick = uc.findtext("nickname") or uc.findtext("username") or ""
        if nick:
            out["likes"].append(nick)
    return out


def list_moments(
    src: LocalSource,
    author: str = "",
    keyword: str = "",
    since: str = "",
    until: str = "",
    limit: int = 20,
    offset: int = 0,
    with_interactions: bool = True,
) -> dict:
    from .entities import load_contacts, resolve_name

    db = src.path("sns")
    if not db.exists():
        return {
            "ok": False,
            "error": {"code": "no_sns_db", "message": "sns.db 不存在"},
            "moments": [],
            "total": 0,
            "data_until": src.data_until("sns"),
        }

    names = {
        c["username"]: (c.get("remark") or c.get("nickname") or c["username"])
        for c in load_contacts(src)
    }
    author_id = ""
    if author:
        hit = resolve_name(src, author, limit=1)
        if hit.get("candidates"):
            author_id = hit["candidates"][0]["id"]
        elif author in names:
            author_id = author
        else:
            return {
                "ok": False,
                "error": {"code": "person_not_found", "message": "未找到该联系人", "candidates": cands},
                "subject": author,
                "candidates": hit.get("candidates", []),
                "total": 0,
                "data_until": src.data_until("sns"),
            }

    lo, hi = _ts(since), _ts(until)
    if hi:
        hi += 86399
    kw = keyword.strip().lower()
    rows = []
    conn = connect_ro(db)
    try:
        cur = conn.execute("SELECT tid, user_name, content FROM SnsTimeLine")
        for r in cur:
            if author_id and r["user_name"] != author_id:
                continue
            parsed = _parse(r["content"] or "")
            if lo and parsed["time"] < lo:
                continue
            if hi and parsed["time"] > hi:
                continue
            if kw and kw not in (parsed["text"] or "").lower():
                continue
            rows.append(
                {
                    "tid": r["tid"],
                    "author_id": r["user_name"] or "",
                    "author_name": names.get(r["user_name"])
                    or ("陌生人" if str(r["user_name"] or "").startswith("v3_") else (r["user_name"] or "")),
                    "time": _fmt(parsed["time"]),
                    "ts": parsed["time"],
                    "text": parsed["text"],
                    "media": parsed["media"],
                    "likes": parsed["likes"],
                    "like_count": len(parsed["likes"]),
                    "comments": [],
                    "comment_count": 0,
                }
            )
        total = len(rows)
        rows.sort(key=lambda m: m["ts"], reverse=True)
        page = rows[offset : offset + limit]
        if with_interactions and page:
            _attach_comments(conn, page, names)
    finally:
        conn.close()

    for m in page:
        m.pop("ts", None)
    return {
        "ok": True,
        "total": total,
        "returned": len(page),
        "has_more": offset + len(page) < total,
        "next": offset + len(page) if offset + len(page) < total else None,
        "data_until": src.data_until("sns"),
        "moments": page,
    }


def _attach_comments(conn: sqlite3.Connection, page: list[dict], names: dict[str, str]) -> None:
    ids = [str(m["tid"]) for m in page]
    if not ids:
        return
    q = (
        "SELECT feed_id, type, from_username, from_nickname, to_nickname, content, create_time"
        " FROM SnsMessage_tmp3 WHERE feed_id IN (%s)" % ",".join("?" * len(ids))
    )
    by_feed: dict[str, list[dict]] = {}
    try:
        for r in conn.execute(q, ids):
            by_feed.setdefault(str(r["feed_id"]), []).append(dict(r))
    except sqlite3.OperationalError:
        return
    for m in page:
        items = by_feed.get(str(m["tid"]), [])
        comments = [
            {
                "from": it["from_nickname"] or names.get(it["from_username"], it["from_username"] or ""),
                "to": it["to_nickname"] or "",
                "text": it["content"] or "",
                "time": _fmt(it["create_time"]),
            }
            for it in items
            if it["type"] == 2 and it["content"]
        ]
        m["comments"] = comments
        m["comment_count"] = len(comments)
        extra_likes = sum(1 for it in items if it["type"] == 1)
        if extra_likes and not m["likes"]:
            m["like_count"] = extra_likes
            m["likes_note"] = "点赞数来自小红点记录，昵称未解析"
        elif extra_likes > m["like_count"]:
            m["like_count"] = extra_likes

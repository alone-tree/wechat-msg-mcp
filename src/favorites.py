"""收藏查询。

依据 docs/重构方案-2026-11-13.md 第 18.2 节：
数据源 decrypted/favorite/favorite.db。fav_db_item.content 为 favitem XML，
标签在 fav_tag_db_item 与 fav_bind_tag_db_item。
"""
from __future__ import annotations

import re
from datetime import datetime
from typing import Optional
from xml.etree import ElementTree as ET

from .db.locator import LocalSource
from .db.open_ import connect_ro

_TYPE_NAMES = {
    1: "text",
    2: "image",
    3: "voice",
    4: "video",
    5: "link",
    6: "location",
    7: "music",
    8: "file",
    14: "chat_record",
    16: "product",
    18: "note",
    19: "app",
    20: "channels",
}
_NAME_TO_TYPE = {v: k for k, v in _TYPE_NAMES.items()}
_CTRL = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f]")


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
    out = {"title": "", "desc": "", "url": "", "source_user": "", "source_chat": "", "data_type": ""}
    if not content:
        return out
    cleaned = _CTRL.sub("", content)
    try:
        root = ET.fromstring(cleaned)
    except ET.ParseError:
        m = re.search(r"<title>(.*?)</title>", cleaned, re.S)
        if m:
            out["title"] = m.group(1)
        m = re.search(r"<desc>(.*?)</desc>", cleaned, re.S)
        if m:
            out["desc"] = m.group(1)
        return out
    out["title"] = root.findtext("title") or ""
    out["desc"] = root.findtext("desc") or ""
    di = root.find(".//dataitem")
    if di is not None:
        out["title"] = out["title"] or (di.findtext("datatitle") or "")
        out["desc"] = out["desc"] or (di.findtext("datadesc") or "")
        out["data_type"] = di.findtext("datatype") or ""
        out["url"] = di.findtext("link") or di.findtext("url") or ""
    if not out["url"]:
        for el in root.iter():
            tag = el.tag.split("}")[-1]
            if tag in ("link", "url") and el.text and el.text.strip().startswith("http"):
                out["url"] = el.text.strip()
                break
    src = root.find("source")
    if src is not None:
        out["source_user"] = src.findtext("fromusr") or ""
        out["source_chat"] = src.findtext("realchatname") or ""
    return out


def list_favorites(
    src: LocalSource,
    type: str = "",
    keyword: str = "",
    tag: str = "",
    since: str = "",
    until: str = "",
    limit: int = 20,
    offset: int = 0,
) -> dict:
    from .entities import load_contacts

    db = src.path("favorite")
    if not db.exists():
        return {
            "ok": False,
            "error": {"code": "no_favorite_db", "message": "favorite.db 不存在"},
            "items": [],
            "total": 0,
            "data_until": src.data_until("favorite"),
        }

    names = {
        c["username"]: (c.get("remark") or c.get("nickname") or c["username"])
        for c in load_contacts(src)
    }
    want_type = _NAME_TO_TYPE.get((type or "").strip().lower(), 0)
    if (type or "").strip().isdigit():
        want_type = int(type)
    if (type or "").strip() and not want_type:
        return {
            "ok": False,
            "error": {
                "code": "unknown_type",
                "message": f"未知 type: {type}",
                "candidates": sorted(_NAME_TO_TYPE.keys()),
            },
            "hint": "写错类型不会自动返回全部，避免看起来像有结果",
        }
    lo, hi = _ts(since), _ts(until)
    if hi:
        hi += 86399
    kw = keyword.strip().lower()
    tag_want = tag.strip()

    conn = connect_ro(db)
    try:
        tag_map: dict[int, list[str]] = {}
        try:
            for r in conn.execute(
                "SELECT b.fav_local_id, t.name FROM fav_bind_tag_db_item b"
                " JOIN fav_tag_db_item t ON t.local_id = b.tag_local_id"
            ):
                tag_map.setdefault(int(r[0]), []).append(r[1] or "")
        except Exception:
            pass
        rows = []
        for r in conn.execute(
            "SELECT local_id, type, content, fromusr, realchatname, update_time FROM fav_db_item"
        ):
            t = int(r["type"] or 0)
            if want_type and t != want_type:
                continue
            ts = int(r["update_time"] or 0)
            if lo and ts < lo:
                continue
            if hi and ts > hi:
                continue
            tags = [x for x in tag_map.get(int(r["local_id"]), []) if x]
            if tag_want and tag_want not in " ".join(tags):
                continue
            parsed = _parse(r["content"] or "")
            text = (parsed["title"] + " " + parsed["desc"]).lower()
            if kw and kw not in text:
                continue
            rows.append(
                {
                    "local_id": r["local_id"],
                    "type": _TYPE_NAMES.get(t, str(t)),
                    "data_type": parsed["data_type"],
                    "title": parsed["title"],
                    "desc": parsed["desc"],
                    "url": parsed["url"],
                    "source_chat": parsed["source_chat"] or (r["realchatname"] or ""),
                    "source_user": names.get(parsed["source_user"], parsed["source_user"]),
                    "time": _fmt(ts),
                    "ts": ts,
                    "tags": tags,
                }
            )
        total = len(rows)
        rows.sort(key=lambda x: x["ts"], reverse=True)
        page = rows[offset : offset + limit]
    finally:
        conn.close()
    for item in page:
        item.pop("ts", None)
    return {
        "ok": True,
        "total": total,
        "returned": len(page),
        "has_more": offset + len(page) < total,
        "next": offset + len(page) if offset + len(page) < total else None,
        "data_until": src.data_until("favorite"),
        "items": page,
    }

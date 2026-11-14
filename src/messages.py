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
import html
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
_CDATA_RE = re.compile(r"^\s*<!\[CDATA\[(.*?)\]\]>\s*$", re.S)
_TAG_RE = re.compile(r"<[^>]+>")
_WS_RE = re.compile(r"\s+")
_DEFAULT_WINDOW_DAYS = 30
# 返回条数超过这个数就不提取媒体：会被落盘裁成 3 条预览，提取了也白提
MEDIA_MAX_ROWS = 50

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


def _clean(text: str) -> str:
    """去 CDATA 包裹与 HTML 实体，只留可读文本。"""
    if not text:
        return ""
    m = _CDATA_RE.match(text.strip())
    if m:
        text = m.group(1)
    return html.unescape(text).strip()


def _first_tag(text: str, tag: str) -> str:
    m = re.search(rf"<{tag}>(.*?)</{tag}>", text or "", re.S)
    return _clean(m.group(1)) if m else ""


def _distill(text: str) -> str:
    """把 appmsg/引用类 XML 压成可读摘要，避免整段 XML 占响应体。"""
    if not text:
        return ""
    parts = [_clean(p) for p in _TITLE_RE.findall(text) + _DES_RE.findall(text)]
    parts = [p for p in parts if p]
    if parts:
        return " / ".join(parts)[:300]
    plain = _WS_RE.sub(" ", html.unescape(_TAG_RE.sub(" ", text))).strip()
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
            "missing_reason": "no_local_file",
            "note": "本地未落盘或未命中 hardlink",
        }
    if subtype == "link":
        url = _first_tag(text, "url")
        return {
            "kind": "link",
            "url": url,
            "title": _clean(next(iter(_TITLE_RE.findall(text or "")), "")),
            "desc": _clean(next(iter(_DES_RE.findall(text or "")), "")),
            "openable": bool(url),
        }
    if subtype == "transfer":
        return {
            "kind": "transfer",
            "amount": _first_tag(text, "feedesc"),
            "des": _clean(next(iter(_DES_RE.findall(text or "")), "")),
            "pay_sub_type": _first_tag(text, "paysubtype"),
        }
    if subtype == "red_packet":
        return {
            "kind": "red_packet",
            "url": _first_tag(text, "url"),
            "amount": None,
            "des": _clean(next(iter(_DES_RE.findall(text or "")), "")),
            "note": "红包金额需拆开后才可见",
        }
    if subtype in ("mini_program", "channels", "app"):
        return {
            "kind": subtype,
            "url": _first_tag(text, "url"),
            "title": _clean(next(iter(_TITLE_RE.findall(text or "")), "")),
            "openable": bool(_first_tag(text, "url")),
        }
    if type_name == "image":
        m = _IMG_MD5.search(text or "")
        return {"kind": "image", "md5": m.group(1) if m else ""}
    if type_name == "voice":
        m = _VOICE_LEN.search(text or "")
        return {"kind": "voice", "duration": (int(m.group(1)) // 1000) if m else None}
    if type_name == "video":
        m = _VIDEO_LEN.search(text or "")
        info = {"kind": "video", "duration": int(m.group(1)) if m else None}
        try:
            from .media_files import resolve_video

            v = resolve_video(text or "")
            if v:
                info["video_path"] = v.get("path", "")
                info["video_exists"] = bool(v.get("exists"))
                info["video_size"] = int(v.get("size") or 0)
        except Exception:
            pass
        return info
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


def _self_info(my_user: str, contacts=None, src=None) -> dict:
    """账号主人信息。AI 靠它区分「我发的」与「别人发的」，不用猜。"""
    if not my_user:
        return {
            "id": "",
            "nickname": "",
            "alias": "",
            "note": "未能识别本机账号，direction 可能不可靠，请勿绝对依赖收发方向",
        }
    name, alias = "", ""
    if contacts is not None:
        try:
            name = contacts.name_of(my_user) or ""
        except Exception:
            pass
    if src is not None:
        conn = None
        try:
            from .db.open_ import connect_ro

            conn = connect_ro(src.path("contact"))
            r = conn.execute(
                "SELECT remark, nick_name, alias FROM contact WHERE username=?", (my_user,)
            ).fetchone()
            if r:
                name = r["remark"] or r["nick_name"] or name
                alias = r["alias"] or ""
        except Exception:
            pass
        finally:
            if conn is not None:
                conn.close()
    return {
        "id": my_user,
        "nickname": name,
        "alias": alias,
        "note": "这个账号就是「我」。direction=out 或 sender_name 显示「我」的都是本人发出，direction=in 是别人发来的",
    }


def _senders_index(src, page: list, contacts) -> dict:
    """发言人 ID 映射：键是 wxid，值是名字、微信号、是否好友。

    每条消息只给名字（自己发的显示「我」），要拿 wxid 就来这张表按名字反查，
    避免每条消息重复一长串 ID 把响应撑大。
    """
    from .db.open_ import connect_ro

    ids = []
    seen = set()
    for m in page:
        sid = m.get("_sender_id") or ""
        if sid and sid not in seen:
            seen.add(sid)
            ids.append(sid)
    if not ids:
        return {}

    info: dict = {}
    conn = None
    try:
        conn = connect_ro(src.path("contact"))
        for i in range(0, len(ids), 300):
            chunk = ids[i:i + 300]
            q = ",".join("?" * len(chunk))
            for r in conn.execute(
                f"SELECT username, remark, nick_name, alias, local_type FROM contact WHERE username IN ({q})",
                chunk,
            ):
                rec = {"name": r["remark"] or r["nick_name"] or r["username"]}
                if r["nick_name"] and r["nick_name"] != rec["name"]:
                    rec["nickname"] = r["nick_name"]
                if r["alias"]:
                    rec["alias"] = r["alias"]
                rec["friend"] = r["local_type"] == 1
                info[r["username"]] = rec
    except Exception:
        pass
    finally:
        if conn is not None:
            conn.close()

    for sid in ids:
        info.setdefault(sid, {"name": contacts.name_of(sid) or sid})
    return info


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
        "_sender_id": sid or (my_user if is_me else ""),
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
    if subtype in ("file", "link", "transfer", "red_packet", "mini_program", "channels", "app"):
        return {"kind": subtype}
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
    media_budget = {"voice": 8}
    voice_model_ok = True
    if include_media:
        try:
            from .db import voice as _voice_mod

            voice_model_ok = _voice_mod.model_ready()
        except Exception:
            voice_model_ok = False
    voice_model_missing = 0

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
    # 条数太多时提取媒体没意义：会被落盘裁成 3 条预览，提取了也白提
    want_media = include_media and len(page) <= MEDIA_MAX_ROWS
    last_key = _sort_key(page[-1]) if page and matched > len(page) else None
    next_cursor = (
        f"{last_key[0]}|{last_key[1]}|{last_key[2]}|{last_key[3]}" if last_key else None
    )
    images: list[dict] = []
    media_missing: dict = {}
    for row in page:
        if not row["chat_name"]:
            row["chat_name"] = name_by_id.get(row["chat_id"], row["chat_id"])
        kind = (row["media"] or {}).get("kind")
        if kind in ("file", "link", "transfer", "red_packet", "mini_program", "channels", "app"):
            row["media"] = _media(
                src, row.get("_raw", ""), row["type"], row["subtype"], row["chat_id"]
            )
            kind = (row["media"] or {}).get("kind")
        if isinstance(row.get("media"), dict) and kind in ("image", "voice", "video", "emoticon"):
            media = row["media"]
            if not want_media:
                media["missing_reason"] = "not_requested" if not include_media else "too_many_rows"
            elif kind == "voice":
                if not voice_model_ok:
                    voice_model_missing += 1
                    media["missing_reason"] = "voice_model_missing"
                elif media_budget["voice"] <= 0:
                    media["missing_reason"] = "budget_exhausted"
                else:
                    from . import media_inline

                    media_budget["voice"] -= 1
                    text, blob = media_inline.voice_transcript(src, row)
                    if text:
                        row["voice_text"] = text
                    if blob:
                        images.append(
                            {
                                "local_id": row["local_id"],
                                "kind": "voice",
                                "source": "silk",
                                "bytes": blob,
                            }
                        )
                    else:
                        media["missing_reason"] = "no_audio"
            else:
                from . import media_inline

                data, source, reason = media_inline.image_bytes_for(row, row.get("_raw", ""))
                if data:
                    media.update({"source": source, "bytes": len(data)})
                    if source == "thumb":
                        media["note"] = "原图未落盘，返回的是缩略图，字小的长截图可能看不清"
                    images.append(
                        {
                            "local_id": row["local_id"],
                            "kind": kind,
                            "source": source,
                            "bytes": data,
                        }
                    )
                else:
                    reason = reason or "extract_failed"
                    media["missing_reason"] = reason
                    media_missing[reason] = media_missing.get(reason, 0) + 1
        row.pop("_raw", None)
        row.pop("_db", None)
        row.pop("_tbl", None)
        row.pop("ts", None)
    has_more = matched > len(page)
    senders_index = _senders_index(src, page, contacts)
    for row in page:
        row.pop("_sender_id", None)
    result = {
        "total": matched,
        "returned": len(page),
        "has_more": has_more,
        "next_cursor": next_cursor,
        "default_window_days": default_window,
        "self": _self_info(my_user, contacts, src),
        "senders": senders_index,
        "data_cutoff": src.data_cutoff("message"),
        "messages": page,
        "unresolved": unresolved,
    }
    if images:
        result["_images"] = images
    hints = []
    if any(isinstance(m.get("media"), dict) and m["media"].get("openable") for m in page):
        hints.append("media.url 可直接在浏览器打开查看原文")
    if any(isinstance(m.get("media"), dict) and m["media"].get("kind") == "transfer" for m in page):
        hints.append("media.amount 为转账金额，直接取自消息里的应付金额字段")
    if any(isinstance(m.get("media"), dict) and m["media"].get("kind") == "red_packet" for m in page):
        hints.append("红包金额本地不存，需拆开后才可见")
    if hints:
        result["hints"] = hints
    if media_missing:
        result["media_missing"] = media_missing
        if media_missing.get("hardlink_row_missing") or media_missing.get("file_not_on_disk"):
            result.setdefault("warnings", []).append(
                "部分图片本机没有原件也没有缩略图（原因见 media.missing_reason，"
                "no_local_image 表示这张图从未在微信里打开过）。"
                "登记库副本落后时先 system(action='sync', force=true) 刷新再查，"
                "看 refresh.failed_detail 确认是否被占用"
            )
    if voice_model_missing:
        try:
            from .db import voice as _vm

            result.setdefault("warnings", []).append(_vm.install_hint())
        except Exception:
            pass
        result["voice_model_missing"] = voice_model_missing
    return result


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

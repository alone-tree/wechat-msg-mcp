"""消息查询。

依据 docs/重构方案-消息模型与输出规范-2026-11-14.md 第 2、3、5 节：
- 不带收发方向，只有发送者 from：自己发的固定「我」，系统消息留空；
- text 自足，每种类型单看这一行就能读懂，系统消息折叠成一句话；
- 三档详细度 lite / full / raw，MCP 默认 lite，CLI 默认 full；
- 分页四件套 total / returned / has_more / next（消息的 next 是游标，传回 cursor）；
- 目标名解析失败返回 unresolved 与候选，不返回空结果冒充无消息；
- 不指定目标时限定时间窗（默认最近 30 天），避免全库逐表扫描。
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

from . import sysmsg
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
_REFER_RE = re.compile(r"<refermsg>(.*?)</refermsg>", re.S)
_PREFIX_RE = re.compile(r"^[\w@._-]+$")
_DEFAULT_WINDOW_DAYS = 30
# 返回条数超过这个数就不提取媒体：会被落盘裁成 3 条预览，提取了也白提
MEDIA_MAX_ROWS = 50
DETAILS = ("lite", "full", "raw")

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
    "transfer": ["app:transfer"],
    "red_packet": ["app:red_packet"],
    "location": ["location"],
    "card": ["card"],
    "voip": ["voip"],
    "system": ["system", "revoked"],
    "revoked": ["revoked", "system"],
}

_MAIN_OF_ALIAS = {
    "text": {1},
    "image": {3},
    "voice": {34},
    "video": {43},
    "file": {49},
    "link": {49},
    "mini_program": {49},
    "quote": {49},
    "chat_record": {49},
    "channels": {49},
    "transfer": {49},
    "red_packet": {49},
    "emoticon": {47},
    "location": {48},
    "card": {42},
    "voip": {50},
    "system": {10000, 10002},
    "revoked": {10000, 10002},
}

# lite 档媒体只留必要字段，路径与明细在 full 档
_LITE_MEDIA_KEYS = ("kind", "name", "title", "url", "duration", "amount", "text", "missing_reason")


class _Rev:
    """把 heapq 变成大顶堆。"""

    __slots__ = ("key",)

    def __init__(self, key):
        self.key = key

    def __lt__(self, other):
        return self.key > other.key


def valid_types() -> list[str]:
    return sorted(_TYPE_ALIASES)


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


def _attr(text: str, name: str) -> str:
    m = re.search(rf'\b{name}="([^"]*)"', text or "")
    return _clean(m.group(1)) if m else ""


def _to_int(value) -> int:
    try:
        return int(str(value).strip())
    except (TypeError, ValueError):
        return 0


def _distill(text: str, limit: int = 300) -> str:
    """把 appmsg/引用类 XML 压成可读摘要，避免整段 XML 占响应体。"""
    if not text:
        return ""
    parts = [_clean(p) for p in _TITLE_RE.findall(text) + _DES_RE.findall(text)]
    parts = [p for p in parts if p]
    if parts:
        return " / ".join(parts)[:limit]
    plain = _WS_RE.sub(" ", html.unescape(_TAG_RE.sub(" ", text))).strip()
    return plain[:limit]


def _quote_of(body: str) -> Optional[dict]:
    """引用消息里的被引内容：谁说的、说了什么。"""
    m = _REFER_RE.search(body or "")
    if not m:
        return None
    inner = m.group(1)
    who = _first_tag(inner, "displayname") or _first_tag(inner, "fromusr")
    text = _distill(_first_tag(inner, "content"), 200) or "[非文字内容]"
    return {"from": who, "text": text}


def _describe(
    type_name: str, subtype: Optional[str], body: str, lookup=None
) -> tuple[str, Optional[dict], Optional[dict]]:
    """返回 (自足正文, 媒体, 引用)。正文保证非空，单看这一行就能读懂。"""
    if type_name in ("system", "revoked"):
        sentence = sysmsg.pat_text(body) if subtype == "pat" else sysmsg.fold(body, lookup)[0]
        return f"[系统] {sentence}", None, None
    if type_name == "text":
        return body, None, None
    if type_name == "image":
        m = _IMG_MD5.search(body or "")
        return "[图片]", {"kind": "image", "md5": m.group(1) if m else ""}, None
    if type_name == "emoticon":
        return "[表情]", {"kind": "emoticon"}, None
    if type_name == "voice":
        m = _VOICE_LEN.search(body or "")
        secs = _to_int(m.group(1)) // 1000 if m else 0
        text = f"[语音 {secs} 秒]" if secs else "[语音]"
        return text, {"kind": "voice", "duration": secs or None}, None
    if type_name == "video":
        m = _VIDEO_LEN.search(body or "")
        secs = _to_int(m.group(1)) if m else 0
        text = f"[视频 {secs} 秒]" if secs else "[视频]"
        return text, {"kind": "video", "duration": secs or None}, None
    if type_name == "location":
        poi, label = _attr(body, "poiname"), _attr(body, "label")
        text = f"[位置] {poi or label}" if (poi or label) else "[位置]"
        return text, {"kind": "location", "poi": poi, "label": label}, None
    if type_name == "voip":
        inner = _first_tag(body, "msg")
        return (f"[通话] {inner}" if inner else "[通话]"), {"kind": "voip", "text": inner}, None
    if type_name == "card":
        nick = _attr(body, "nickname") or _first_tag(body, "nickname")
        return (f"[名片] {nick}" if nick else "[名片]"), {"kind": "card", "title": nick}, None

    title = _clean(next(iter(_TITLE_RE.findall(body or "")), ""))
    desc = _clean(next(iter(_DES_RE.findall(body or "")), ""))
    url = _first_tag(body, "url")
    if subtype == "file":
        name = title or "未命名文件"
        size = _to_int(_first_tag(body, "totallen") or _attr(body, "totallen"))
        return f"[文件] {name}", {"kind": "file", "name": name, "size": size}, None
    if subtype == "link":
        return (title or "[链接]"), {"kind": "link", "url": url, "title": title, "desc": desc}, None
    if subtype == "transfer":
        amount = _first_tag(body, "feedesc")
        text = f"[转账] {amount}" if amount else "[转账]"
        return text, {"kind": "transfer", "amount": amount, "desc": desc}, None
    if subtype == "red_packet":
        return "[红包]", {"kind": "red_packet", "desc": desc, "url": url}, None
    if subtype == "quote":
        return (title or "[引用]"), {"kind": "quote", "title": title}, _quote_of(body)
    if subtype == "chat_record":
        text = f"[聊天记录] {title}" if title else "[聊天记录]"
        return text, {"kind": "chat_record", "title": title, "desc": desc[:200]}, None
    if subtype == "mini_program":
        text = f"[小程序] {title}" if title else "[小程序]"
        return text, {"kind": "mini_program", "title": title, "url": url}, None
    if subtype == "channels":
        return (title or "[视频号]"), {"kind": "channels", "title": title, "url": url}, None
    if subtype == "music":
        text = f"[音乐] {title}" if title else "[音乐]"
        return text, {"kind": "music", "title": title, "url": url}, None
    if subtype == "sticker":
        return "[表情]", {"kind": "emoticon"}, None
    if subtype == "card":
        text = f"[名片] {title}" if title else "[名片]"
        return text, {"kind": "card", "title": title}, None
    text = title or _distill(body) or f"[{subtype or '应用消息'}]"
    return text, {"kind": subtype or "app", "title": title, "url": url}, None


def _wanted_types(
    types: Optional[Iterable[str]],
) -> tuple[Optional[set[str]], Optional[set[int]], list[str]]:
    """返回 (标签集合, 主类型集合, 无法识别的取值)。不认识的取值不静默当空结果。"""
    if not types:
        return None, None, []
    tags: set[str] = set()
    mains: set[int] = set()
    unknown: list[str] = []
    for t in types:
        key = str(t).strip().lower()
        if key not in _TYPE_ALIASES:
            unknown.append(str(t))
            continue
        tags.update(_TYPE_ALIASES[key])
        mains.update(_MAIN_OF_ALIAS.get(key, set()))
    return tags or None, mains or None, unknown


def _my_username() -> str:
    try:
        from .config import get_account_wxid

        return get_account_wxid()
    except Exception:
        return ""


_SELF_CACHE: dict[str, dict] = {}


def _self_info(my_user: str, contacts=None, src=None) -> dict:
    """账号主人信息。AI 靠它把「我」换成具体的人。按 wxid 缓存，只查一次库。"""
    cached = _SELF_CACHE.get(my_user)
    if cached is not None:
        return dict(cached)
    info = _build_self_info(my_user, contacts, src)
    _SELF_CACHE[my_user] = dict(info)
    return info


def _build_self_info(my_user: str, contacts=None, src=None) -> dict:
    if not my_user:
        return {
            "id": "",
            "nickname": "",
            "alias": "",
            "note": "未能识别本机账号；from 为「我」的消息仍是本人发出，只是没有对应 wxid",
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
        "note": "这个账号就是「我」；from 为「我」的消息是本人发出",
    }


def _senders_index(src, page: list, contacts) -> dict:
    """发言人 ID 映射：键是 wxid，值是名字、微信号、是否好友。

    每条消息只给名字（自己发的显示「我」），要拿 wxid 就来这张表按名字反查，
    避免每条消息重复一长串 ID 把响应撑大。系统消息不进这张表。
    """
    ids: list[str] = []
    seen: set[str] = set()
    for m in page:
        sid = m.get("sender_id") or ""
        if sid and sid not in seen and not sid.endswith("@chatroom"):
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


def _build_row(
    db: Path,
    table: str,
    r,
    sender_map: dict,
    my_user: str,
    contacts: ContactIndex,
    lookup=None,
) -> dict:
    body = _content(r["message_content"], r["WCDB_CT_message_content"])
    # 群消息正文前可能带「发送者 wxid:」前缀，去掉；发送者另有 real_sender_id 可查
    head, sep, rest = body.partition("\n")
    if sep and head.endswith(":") and _PREFIX_RE.match(head[:-1]):
        body = rest
    type_name, subtype = classify(r["local_type"], body)
    if type_name == "app" and subtype == "channels" and sysmsg.is_pat(body):
        type_name, subtype = "system", "pat"
    sid, is_me = resolve_sender(r["real_sender_id"], sender_map, my_user)
    if type_name == "system":
        sid, is_me = None, False
    text, media, quote = _describe(type_name, subtype, body, lookup)
    if not text and type_name == "text":
        text = ""
    return {
        "local_id": r["local_id"],
        "time": _fmt(r["create_time"]),
        "ts": int(r["create_time"] or 0),
        "from": "我" if is_me else (contacts.name_of(sid) or "" if sid else ""),
        "sender_id": sid or "",
        "is_me": is_me,
        "unknown_sender": _unknown_sender(type_name, is_me, sid),
        "type": type_name,
        "subtype": subtype,
        "chat_id": "",
        "chat_name": "",
        "local_type": int(r["local_type"] or 0),
        "text": text,
        "media": media,
        "quote": quote,
        "db": db.name,
        "tbl": table,
        "raw": body,
    }


def _unknown_sender(type_name: str, is_me: bool, sid: Optional[str]) -> bool:
    """解析不到发送者才算未知。系统消息本来就没有发送者，不算未知。"""
    if type_name in ("system", "revoked"):
        return False
    return bool(not is_me and not sid)


def _enrich_media(src: LocalSource, row: dict) -> None:
    """把文件与视频消息补成本地真实路径。只对当前页做，扫描阶段不碰磁盘。"""
    media = row.get("media")
    if not isinstance(media, dict):
        return
    kind = media.get("kind")
    if kind == "file":
        from .media_files import resolve as resolve_file

        m = _XML_MD5.search(row.get("raw") or "")
        info = resolve_file(src, m.group(1) if m else "", row.get("chat_id") or "")
        if info:
            media.update({k: v for k, v in info.items() if k != "kind"})
        else:
            media.update({"path": "", "exists": False, "missing_reason": "no_local_file"})
    elif kind == "video":
        from .media_files import resolve_video

        info = resolve_video(row.get("raw") or "")
        if info:
            media["path"] = info.get("path", "")
            media["exists"] = bool(info.get("exists"))
            media["size"] = int(info.get("size") or 0)
            if not info.get("exists"):
                media["missing_reason"] = "no_local_video"
        else:
            media["missing_reason"] = "no_local_video"


def _project(row: dict, detail: str, show_chat: bool) -> dict:
    """按详细度裁剪一条消息。lite 也能独立读懂，full 给 ID 与类型，raw 给原文。"""
    out = {
        "id": row["local_id"],
        "time": row["time"],
        "from": row["from"],
        "text": row["text"],
    }
    if row.get("unknown_sender"):
        out["from_unknown"] = True
    media = row.get("media")
    if isinstance(media, dict) and media:
        if detail == "lite":
            slim = {
                k: v
                for k, v in media.items()
                if k in _LITE_MEDIA_KEYS and v not in (None, "", 0, [])
            }
            out["media"] = slim or {"kind": str(media.get("kind") or "")}
        else:
            out["media"] = {k: v for k, v in media.items() if v is not None}
    if row.get("quote"):
        out["quote"] = row["quote"]
    if detail == "lite":
        if show_chat:
            out["chat"] = row.get("chat_name") or row.get("chat_id") or ""
        return out
    out["sender_id"] = row.get("sender_id") or ""
    out["type"] = row["type"]
    if row["type"] == "app" and row.get("subtype"):
        out["subtype"] = row["subtype"]
    out["chat_id"] = row.get("chat_id") or ""
    out["chat_name"] = row.get("chat_name") or row.get("chat_id") or ""
    if detail == "raw":
        out["local_type"] = row["local_type"]
        out["raw"] = row.get("raw") or ""
        out["db"] = row.get("db") or ""
    return out


_TABLE_CACHE: dict[tuple, list[tuple[Path, str]]] = {}


def resolve_tables(src: LocalSource, chat_id: str) -> list[tuple[Path, str]]:
    """会话在哪几个分库里有消息表。按分库文件时间缓存，省掉每次开 19 个库。"""
    table = "Msg_" + username_to_md5(chat_id)
    dbs = src.message_dbs()
    try:
        sig = tuple((db.name, db.stat().st_mtime, db.stat().st_size) for db in dbs)
    except OSError:
        sig = tuple((db.name, 0.0, 0) for db in dbs)
    key = (chat_id, sig)
    cached = _TABLE_CACHE.get(key)
    if cached is not None:
        return cached
    found: list[tuple[Path, str]] = []
    for db in dbs:
        conn = connect_ro(db)
        try:
            row = conn.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)
            ).fetchone()
            if row:
                found.append((db, table))
        finally:
            conn.close()
    if len(_TABLE_CACHE) > 512:
        _TABLE_CACHE.clear()
    _TABLE_CACHE[key] = found
    return found


def _sort_key(row: dict):
    return (row["ts"], row["db"], row["tbl"], row["local_id"])


def _cursor_key(cursor: str):
    try:
        ts, db, tbl, lid = cursor.split("|")
        return (int(ts), db, tbl, int(lid))
    except Exception:
        return None


def _error(code: str, message: str, **extra) -> dict:
    out = {"ok": False, "error": {"code": code, "message": message}}
    out["error"].update(extra)
    return out


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
    per_target_limit: int = 0,
    include_media: bool = False,
    detail: str = "lite",
) -> dict:
    from . import index
    from .entities import display_names, load_contacts, resolve_name

    detail = str(detail or "lite").lower()
    if detail not in DETAILS:
        return _error("bad_detail", f"未知详细度: {detail}", candidates=list(DETAILS))
    limit = max(1, min(int(limit), 1000))
    order = "asc" if str(order).lower() == "asc" else "desc"
    lo, hi = _ts(start), _ts_end(end)
    default_window = 0
    if not targets and lo is None and hi is None:
        default_window = _DEFAULT_WINDOW_DAYS
        lo = int(time.time()) - default_window * 86400
    tags, mains, unknown_types = _wanted_types(types)
    if unknown_types:
        return _error(
            "unknown_types",
            "无法识别的类型过滤，已拒绝执行以免静默返回空结果",
            unknown=unknown_types,
            candidates=valid_types(),
        )
    kw = keyword.strip().lower()
    my_user = _my_username()

    contact_rows = load_contacts(src)
    contacts = ContactIndex()
    for c in contact_rows:
        contacts.add({"id": c.get("id"), **c})
    names = display_names(src, contact_rows)
    name_by_id: dict[str, str] = {k: v.get("name", k) for k, v in names.items()}

    def lookup(wxid: str) -> str:
        return contacts.name_of(wxid) or ""

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
    per_cap = limit if per_target_limit <= 0 else max(1, min(limit, int(per_target_limit)))
    # 只有每会话上限比 limit 还小时才会真的丢数据，否则多出来的行下一页仍能取到
    trimming = bool(multi and targets and per_cap < limit)
    cand_key = _cursor_key(cursor) if cursor else None
    matched = 0
    remaining = 0
    dropped: dict[str, int] = {}
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
                cached = _db_maps(db, conn)
                db_cache[db.name] = cached
            md5_to_user, sender_map = cached
            if cid is None:
                cid = md5_to_user.get(tbl[4:] if tbl.startswith("Msg_") else "")
                cname = name_by_id.get(cid, cid) if cid else ""
            bucket_key = (cid if targets else "__all__") or "__all__"
            bucket = heaps.setdefault(bucket_key, [])
            cap = per_cap if (multi and targets) else limit
            sql = (
                "SELECT local_id, create_time, local_type, real_sender_id,"
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
                row = _build_row(db, tbl, r, sender_map, my_user, contacts, lookup)
                row["chat_id"] = cid or ""
                row["chat_name"] = cname or ""
                if sender:
                    s = sender.lower()
                    if s not in (row["sender_id"].lower(), row["from"].lower()):
                        continue
                if tags:
                    tag = f"app:{row['subtype']}" if row["type"] == "app" else row["type"]
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
                remaining += 1
                if order == "desc":
                    if len(bucket) < cap:
                        heapq.heappush(bucket, (key, row))
                    elif key > bucket[0][0]:
                        heapq.heapreplace(bucket, (key, row))
                    elif trimming:
                        dropped[bucket_key] = dropped.get(bucket_key, 0) + 1
                else:
                    if len(bucket) < cap:
                        heapq.heappush(bucket, (_Rev(key), row))
                    elif key < bucket[0][0].key:
                        heapq.heapreplace(bucket, (_Rev(key), row))
                    elif trimming:
                        dropped[bucket_key] = dropped.get(bucket_key, 0) + 1
        finally:
            conn.close()

    rows = [item[1] for bucket_rows in heaps.values() for item in bucket_rows]
    rows.sort(key=_sort_key, reverse=(order == "desc"))
    page = rows[:limit]
    has_more = remaining > len(page)
    last_key = _sort_key(page[-1]) if page and has_more else None
    next_value = (
        f"{last_key[0]}|{last_key[1]}|{last_key[2]}|{last_key[3]}" if last_key else None
    )

    # 条数太多时提取媒体没意义：会被落盘裁成 3 条预览，提取了也白提
    want_media = include_media and len(page) <= MEDIA_MAX_ROWS
    images: list[dict] = []
    media_missing: dict = {}
    for row in page:
        if not row["chat_name"]:
            row["chat_name"] = name_by_id.get(row["chat_id"], row["chat_id"])
        _enrich_media(src, row)
        kind = (row["media"] or {}).get("kind") if isinstance(row.get("media"), dict) else None
        if not kind:
            continue
        media = row["media"]
        if kind in ("image", "voice", "video", "emoticon"):
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
                    transcript, blob = media_inline.voice_transcript(src, row)
                    if transcript:
                        row["text"] = transcript
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

                data, source, reason = media_inline.image_bytes_for(row, row.get("raw") or "")
                if data:
                    media["bytes"] = len(data)
                    media["source"] = source
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

    chat_ids = {row["chat_id"] for row in page if row["chat_id"]}
    show_chat = len(chat_ids) > 1
    single_chat = ""
    if len(chat_ids) == 1 and page:
        single_chat = page[0]["chat_name"] or next(iter(chat_ids))

    senders_index = _senders_index(src, page, contacts) if detail != "lite" else {}
    messages = [_project(row, detail, show_chat) for row in page]

    result = {
        "ok": True,
        "detail": detail,
        "total": matched,
        "returned": len(page),
        "has_more": has_more,
        "next": next_value,
        "per_target_limit": per_cap if (multi and targets) else None,
        "default_window_days": default_window,
        "chat": single_chat,
        "self": _self_info(my_user, contacts, src),
        "data_until": src.data_until("message"),
        "messages": messages,
        "unresolved": unresolved,
    }
    if detail != "lite":
        result["senders"] = senders_index
    if images:
        result["_images"] = images

    hints = []
    if any(isinstance(m.get("media"), dict) and m["media"].get("url") for m in messages):
        hints.append("media.url 可直接在浏览器打开查看原文")
    if any(isinstance(m.get("media"), dict) and m["media"].get("kind") == "transfer" for m in messages):
        hints.append("media.amount 为转账金额，直接取自消息里的应付金额字段")
    if any(isinstance(m.get("media"), dict) and m["media"].get("kind") == "red_packet" for m in messages):
        hints.append("红包金额本地不存，需拆开后才可见")
    if hints:
        result["hints"] = hints

    warnings: list[str] = []
    if dropped:
        labels = []
        for key, count in dropped.items():
            label = "未指定会话" if key == "__all__" else name_by_id.get(key, key)
            labels.append(f"{label}（少 {count} 条）")
        warnings.append(
            f"per_target_limit={per_cap} 小于 limit，每个会话只保留了前 {per_cap} 条，"
            "以下会话还有消息取不到（翻页也拿不到），调大 per_target_limit 或分开查询："
            + "、".join(labels[:10])
        )
    if unresolved:
        warnings.append("有目标名没解析到，见 unresolved；不要把它当成该会话没有消息")
    if include_media and len(page) > MEDIA_MAX_ROWS:
        warnings.append(
            f"本次返回 {len(page)} 条超过 {MEDIA_MAX_ROWS} 条，未提取媒体；"
            "要看图请缩小范围或加 types=['image']"
        )
    if media_missing:
        result["media_missing"] = media_missing
        if media_missing.get("no_local_image") or media_missing.get("file_not_on_disk"):
            warnings.append(
                "部分图片本机没有原件也没有缩略图（原因见 media.missing_reason，"
                "no_local_image 表示这张图从未在微信里打开过）"
            )
    if voice_model_missing:
        try:
            from .db import voice as _vm

            warnings.append(_vm.install_hint())
        except Exception:
            pass
        result["voice_model_missing"] = voice_model_missing
    if warnings:
        result["warnings"] = warnings
    return result


_MAP_CACHE: dict[tuple, tuple[dict, dict]] = {}


def _db_maps(db: Path, conn) -> tuple[dict, dict]:
    """消息库的两张映射表：分表 md5 -> 会话 id、real_sender_id -> wxid。

    按库文件时间缓存：分库一旦被刷新写过就自动重建，不会用到旧人。
    """
    try:
        st = db.stat()
        key = (str(db), st.st_mtime, st.st_size)
    except OSError:
        key = (str(db), 0.0, 0)
    cached = _MAP_CACHE.get(key)
    if cached is not None:
        return cached
    md5_to_user: dict[str, str] = {}
    for col in ("user_name", "userName"):
        try:
            for _rid, name in conn.execute(f"SELECT rowid, {col} FROM Name2Id"):
                if name:
                    md5_to_user[username_to_md5(name)] = name
            break
        except sqlite3.OperationalError:
            continue
    value = (md5_to_user, build_local_sender_map(conn))
    if len(_MAP_CACHE) > 24:
        _MAP_CACHE.clear()
    _MAP_CACHE[key] = value
    return value


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

"""message_resource.db：消息到本地文件基名的映射。

微信 4.x 把图片、视频、文件落盘后的文件名写进 MessageResourceInfo.packed_info，
不再（或不再及时）写进 image_hardlink_info_v4 这类登记表。硬啃登记表会出现
「图明明在盘上却查不到」，靠这张表能稳定定位磁盘文件。

packed_info 结构（protobuf）：{ field 2 { field 1: 文件基名 } }，实测为 32 位十六进制串。
读取一律开-查-关，不长期持有连接，否则会锁住库、让解密刷新换不掉副本。
"""
from __future__ import annotations

import os
import sqlite3
from pathlib import Path
from typing import Optional

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
RESOURCE_DB = os.path.join(PROJECT_ROOT, "decrypted", "message", "message_resource.db")

_cache: dict = {}


def _pb_str(blob: bytes) -> str:
    """从 packed_info 里取出文件基名；解析不了返回空串。"""
    i, n = 0, len(blob)
    while i < n:
        tag = blob[i]
        i += 1
        if tag & 0x07 != 2 or i >= n:
            return ""
        size = blob[i]
        i += 1
        value = blob[i : i + size]
        i += size
        if tag >> 3 != 2:
            continue
        if value and value[0] == 0x0A and len(value) > 2:
            inner = value[2 : 2 + value[1]]
        else:
            inner = value
        return inner.decode("ascii", "ignore").strip()
    return ""


def file_base(chat_id: str, local_id: int, create_time: int = 0) -> str:
    """消息 -> 本地文件基名（不含扩展名与 _h/_t 后缀）。查不到返回空串。"""
    if not chat_id or not local_id:
        return ""
    key = (chat_id, int(local_id), int(create_time or 0))
    if key in _cache:
        return _cache[key]
    db = Path(RESOURCE_DB)
    name = ""
    if db.exists():
        conn = None
        try:
            conn = sqlite3.connect(f"file:{db.as_posix()}?mode=ro&immutable=1", uri=True)
            rows = conn.execute(
                "SELECT i.packed_info FROM MessageResourceInfo i "
                "JOIN ChatName2Id n ON n.rowid = i.chat_id "
                "WHERE n.user_name = ? AND i.message_local_id = ?",
                (chat_id, int(local_id)),
            ).fetchall()
        except Exception:
            rows = []
        finally:
            if conn is not None:
                conn.close()
        for (blob,) in rows:
            got = _pb_str(blob or b"")
            if got:
                name = got
                break
    if len(_cache) > 8192:
        _cache.clear()
    _cache[key] = name
    return name


def media_paths(chat_id: str, local_id: int, create_time: int, kind: str = "img") -> list[Path]:
    """按消息定位磁盘上的候选文件：图片 Img/<基名>{,_h,_t}.dat，视频 video/<年月>/<基名>{,_thumb}.jpg。"""
    import hashlib
    import time as _time

    base = file_base(chat_id, local_id, create_time)
    if not base:
        return []
    wechat = os.environ.get("WECHAT_FILES_DIR") or r"F:\xwechat_files"
    root = Path(wechat)
    if not root.exists():
        return []
    chat_md5 = hashlib.md5(chat_id.encode("utf-8", "ignore")).hexdigest()
    months = []
    for delta in (0, -1, 1):
        months.append(_time.strftime("%Y-%m", _time.localtime(int(create_time or 0) + delta * 30 * 86400)))
    out: list[Path] = []
    for acc in root.iterdir():
        if kind == "img":
            for month in months:
                d = acc / "msg" / "attach" / chat_md5 / month / "Img"
                if not d.exists():
                    continue
                for suffix in ("", "_h", "_t"):
                    p = d / f"{base}{suffix}.dat"
                    if p.exists():
                        out.append(p)
                if out:
                    return out
        else:
            vroot = acc / "msg" / "video"
            for month in months:
                d = vroot / month
                if not d.exists():
                    continue
                for suffix in (".jpg", "_thumb.jpg"):
                    p = d / f"{base}{suffix}"
                    if p.exists():
                        out.append(p)
                if out:
                    return out
    return out

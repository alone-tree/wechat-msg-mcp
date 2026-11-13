"""媒体内联：图片落盘为 JPEG、语音转成文字、视频与表情取首帧。

依据 docs/重构方案-2026-11-13.md 第 6.2 节：include_media=true 时启用，
默认关闭，避免响应体过大。产物写 decrypted/exports/media/，不修改源库。
"""
from __future__ import annotations

import hashlib
import sqlite3
from pathlib import Path
from typing import Optional

from .db.locator import LocalSource
from .db.open_ import connect_ro

_MAX_ITEMS = 8


def _media_dir(src: LocalSource) -> Path:
    d = src.root / "exports" / "media"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _save_jpeg(src: LocalSource, data: bytes, tag: str) -> dict:
    d = _media_dir(src)
    name = hashlib.md5(data).hexdigest()[:16]
    path = d / f"{tag}-{name}.jpg"
    if not path.exists():
        path.write_bytes(data)
    return {"path": str(path), "bytes": len(data)}


def _voice_blob(src: LocalSource, chat_id: str, local_id: int) -> Optional[bytes]:
    for db in sorted((src.root / "message").glob("media_*.db")):
        conn = connect_ro(db)
        try:
            row = conn.execute(
                "SELECT rowid FROM Name2Id WHERE user_name = ?", (chat_id,)
            ).fetchone()
            if not row:
                continue
            v = conn.execute(
                "SELECT voice_data FROM VoiceInfo WHERE chat_name_id = ? AND local_id = ?",
                (row[0], local_id),
            ).fetchone()
            if v and v[0]:
                return v[0]
        except sqlite3.OperationalError:
            continue
        finally:
            conn.close()
    return None


def enrich(src: LocalSource, row: dict, raw_text: str, budget: dict) -> None:
    """就地补充 row['media'] 与 row['voice_text']。budget 控制本次内联数量。"""
    kind = (row.get("media") or {}).get("kind")
    if not kind:
        return
    if kind in ("image", "video", "emoticon"):
        if budget["image"] <= 0:
            return
        from .db import image as image_mod

        data = None
        source = ""
        if kind == "image":
            data, source = image_mod.extract_image_meta(
                raw_text, row.get("chat_id", ""), 0
            )
        elif kind == "video":
            data = image_mod.extract_video_cover(raw_text)
            source = "cover"
        else:
            data = image_mod.extract_emoticon_bytes(raw_text)
            source = "emoticon"
        if data:
            budget["image"] -= 1
            info = _save_jpeg(src, data, kind)
            row["media"] = {
                "kind": kind,
                "source": source,
                "path": info["path"],
                "bytes": info["bytes"],
            }
        return
    if kind == "voice":
        if budget["voice"] <= 0:
            return
        blob = _voice_blob(src, row.get("chat_id", ""), int(row.get("local_id") or 0))
        if not blob:
            return
        budget["voice"] -= 1
        from .db import voice as voice_mod

        work = str(_media_dir(src))
        text, duration = voice_mod.transcribe_voice_blob(blob, work)
        media = row.get("media") or {}
        media["duration"] = media.get("duration") or duration
        media["transcribed"] = bool(text)
        row["media"] = media
        if text:
            row["voice_text"] = text

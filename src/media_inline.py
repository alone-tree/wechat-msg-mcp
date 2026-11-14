"""媒体处理：图片字节提取（不落盘）、语音转写、按需落盘。

依据 docs/重构方案-2026-11-13.md 第 21 节：
- 图片、表情、视频封面只在内存里解成 JPEG 字节，是否内联或落盘由调用方决定；
- 语音直接转成文字，音频不落盘；
- 数量与体积预算由工具层按返回体大小统一控制，本模块不设上限。
"""
from __future__ import annotations

import hashlib
import sqlite3
from pathlib import Path
from typing import Optional

from .db.locator import LocalSource
from .db.open_ import connect_ro


def media_dir(src: LocalSource) -> Path:
    d = src.root / "exports" / "media"
    d.mkdir(parents=True, exist_ok=True)
    return d


def save_jpeg(src: LocalSource, data: bytes, tag: str, out_dir: Optional[Path] = None) -> dict:
    """把图片字节写到磁盘，返回路径与大小。out_dir 为空时写默认媒体目录。"""
    d = Path(out_dir) if out_dir else media_dir(src)
    d.mkdir(parents=True, exist_ok=True)
    name = hashlib.md5(data).hexdigest()[:16]
    path = d / f"{tag}-{name}.jpg"
    if not path.exists():
        path.write_bytes(data)
    return {"path": str(path), "bytes": len(data)}


def image_bytes_for(row: dict, raw_text: str) -> tuple[Optional[bytes], str]:
    """按消息类型提取可内联的图片字节，返回 (JPEG 字节, 来源标记)。

    来源标记：dat 原图、cache 微信渲染缓存、thumb 缩略图、cover 视频封面、emoticon 表情首帧。
    """
    kind = (row.get("media") or {}).get("kind")
    if kind not in ("image", "video", "emoticon"):
        return None, ""
    from .db import image as image_mod

    if kind == "image":
        return image_mod.extract_image_meta(
            raw_text, row.get("chat_id", ""), int(row.get("ts") or 0)
        )
    if kind == "video":
        return image_mod.extract_video_cover(raw_text), "cover"
    return image_mod.extract_emoticon_bytes(raw_text), "emoticon"


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


def voice_transcript(src: LocalSource, row: dict) -> Optional[str]:
    """语音消息转文字。取不到音频或转写失败返回 None。"""
    blob = _voice_blob(src, row.get("chat_id", ""), int(row.get("local_id") or 0))
    if not blob:
        return None
    from .db import voice as voice_mod

    try:
        text, duration = voice_mod.transcribe_voice_blob(blob, str(media_dir(src)))
    except Exception:
        return None
    media = row.get("media") or {}
    if duration and not media.get("duration"):
        media["duration"] = duration
    media["transcribed"] = bool(text)
    row["media"] = media
    return text or None

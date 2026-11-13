"""文件消息落盘路径解析。

依据实际目录结构：attach/<md5(会话)>/<年-月>/File/<文件名>，
合并转发里的文件则在 <年-月>/Rec/<记录号>/F/<序号>/<文件名>。

md5 与月份来自 hardlink.db 的 file_hardlink_info_v4 与 dir2id，
会话哈希与 Msg_ 表后缀同源（md5(会话 id)）。
解析不到就返回 exists=False，并保留文件名与大小。
"""
from __future__ import annotations

import hashlib
import os
from pathlib import Path
from typing import Optional
from urllib.parse import unquote

from .db.locator import LocalSource
from .db.open_ import connect_ro

_CACHE: dict[tuple[str, str, str], Optional[dict]] = {}


def _attach_root() -> Optional[Path]:
    files_dir = os.environ.get("WECHAT_FILES_DIR")
    if not files_dir:
        return None
    base = Path(files_dir).expanduser()
    if not base.exists():
        return None
    for d in base.iterdir():
        if d.is_dir() and (d / "db_storage").exists():
            return d / "msg" / "attach"
    return None


def _msg_root() -> Optional[Path]:
    files_dir = os.environ.get("WECHAT_FILES_DIR")
    if not files_dir:
        return None
    base = Path(files_dir).expanduser()
    if not base.exists():
        return None
    for d in base.iterdir():
        if d.is_dir() and (d / "db_storage").exists():
            return d / "msg"
    return None


def _locate(root: Path, chat_id: str, month: str, name: str) -> tuple[str, bool]:
    if not name:
        return "", False
    msg_root = _msg_root()
    if msg_root:
        file_root = msg_root / "file"
        if file_root.exists():
            if month:
                direct = file_root / month / name
                if direct.exists():
                    return str(direct), True
            for hit in file_root.glob(f"*/{name}"):
                return str(hit), True
    chat_dir = root / hashlib.md5(chat_id.encode()).hexdigest()
    if not chat_dir.exists():
        return "", False
    month_dirs = [chat_dir / month] if month else [p for p in chat_dir.iterdir() if p.is_dir()]
    for mdir in month_dirs:
        if not mdir.exists():
            continue
        direct = mdir / "File" / name
        if direct.exists():
            return str(direct), True
        rec_root = mdir / "Rec"
        if rec_root.exists():
            for hit in rec_root.glob(f"*/F/*/{name}"):
                return str(hit), True
    return "", False


def resolve(src: LocalSource, md5: str, chat_id: str = "") -> Optional[dict]:
    if not md5:
        return None
    key = (str(src.root), md5, chat_id)
    if key in _CACHE:
        return _CACHE[key]
    db = src.path("hardlink")
    result = None
    if db.exists():
        conn = connect_ro(db)
        try:
            try:
                row = conn.execute(
                    "SELECT file_name, file_size, dir1 FROM file_hardlink_info_v4 WHERE md5 = ? LIMIT 1",
                    (md5,),
                ).fetchone()
            except Exception:
                row = None
            if row:
                name = unquote(row["file_name"] or "")
                month = ""
                if row["dir1"] is not None:
                    r2 = conn.execute(
                        "SELECT username FROM dir2id WHERE rowid=?", (row["dir1"],)
                    ).fetchone()
                    month = (r2["username"] if r2 else "") or ""
                path, exists = "", False
                root = _attach_root()
                if root and chat_id:
                    path, exists = _locate(root, chat_id, month, name)
                result = {
                    "kind": "file",
                    "name": name,
                    "size": int(row["file_size"] or 0),
                    "path": path,
                    "exists": exists,
                }
        finally:
            conn.close()
    _CACHE[key] = result
    return result

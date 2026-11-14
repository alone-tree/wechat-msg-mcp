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
import re
import shutil
from pathlib import Path
from typing import Optional
from urllib.parse import unquote

from .db.locator import LocalSource, latest_copy
from .db.open_ import connect_ro

_CACHE: dict[tuple, Optional[dict]] = {}


def _wechat_base() -> Optional[Path]:
    """微信数据根目录。优先环境变量，没有则用 config 的探测逻辑，保证 CLI 也能用。"""
    from .config import get_wechat_files_dir

    return get_wechat_files_dir()


def _attach_root() -> Optional[Path]:
    base = _wechat_base()
    if base is None:
        return None
    for d in base.iterdir():
        if d.is_dir() and (d / "db_storage").exists():
            return d / "msg" / "attach"
    return None


def _msg_root() -> Optional[Path]:
    base = _wechat_base()
    if base is None:
        return None
    for d in base.iterdir():
        if d.is_dir() and (d / "db_storage").exists():
            return d / "msg"
    return None


def _locate(root: Optional[Path], chat_id: str, month: str, name: str) -> tuple[str, bool]:
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
    # 以下是 attach 目录，需要会话哈希，会话未知时到此为止。
    if not chat_id or root is None:
        return "", False
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
    db = latest_copy(src.path("hardlink"))
    key = (str(src.root), md5, chat_id, db.stat().st_mtime if db.exists() else 0.0)
    if key in _CACHE:
        return _CACHE[key]
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
    return result


_VIDEO_MD5_RE = re.compile(r'<videomsg[^>]*\bmd5="([0-9a-f]{32})"')
_VIDEO_CACHE: dict[tuple, Optional[dict]] = {}


def resolve_video(content: str) -> Optional[dict]:
    """视频消息 -> 本地 mp4 路径。本地不存在时 exists=False。"""
    if not content:
        return None
    m = _VIDEO_MD5_RE.search(content)
    if not m:
        return None
    md5 = m.group(1)
    result = None
    msg_root = _msg_root()
    db = Path(os.environ.get("WECHAT_DECRYPTED_DIR") or "").expanduser() / "hardlink" / "hardlink.db"
    if not db.exists():
        from .config import get_decrypted_dir

        db = get_decrypted_dir() / "hardlink" / "hardlink.db"
    db = latest_copy(db)
    key = (md5, db.stat().st_mtime if db.exists() else 0.0)
    if key in _VIDEO_CACHE:
        return _VIDEO_CACHE[key]
    if db.exists() and msg_root:
        conn = connect_ro(db)
        try:
            row = conn.execute(
                "SELECT file_name, dir1 FROM video_hardlink_info_v4 WHERE md5=? LIMIT 1", (md5,)
            ).fetchone()
            month = ""
            if row and row["dir1"] is not None:
                r2 = conn.execute("SELECT username FROM dir2id WHERE rowid=?", (row["dir1"],)).fetchone()
                month = (r2["username"] if r2 else "") or ""
        except Exception:
            row = None
        finally:
            conn.close()
        if row:
            name = row["file_name"] or ""
            base = name
            for suffix in (".mp4", ".mp4_raw", "_raw.mp4"):
                if base.endswith(suffix):
                    base = base[: -len(suffix)]
                    break
            if base.endswith("_raw"):
                base = base[: -4]
            video_root = msg_root / "video"
            candidates = []
            if month:
                candidates.append(video_root / month)
            candidates.append(video_root)
            for d in candidates:
                for candidate_name in (f"{base}.mp4", f"{base}_raw.mp4"):
                    p = d / candidate_name
                    if p.exists():
                        result = {
                            "kind": "video",
                            "name": p.name,
                            "size": p.stat().st_size,
                            "path": str(p),
                            "exists": True,
                        }
                        break
                if result:
                    break
            if not result:
                result = {"kind": "video", "name": base, "size": 0, "path": "", "exists": False}
    _VIDEO_CACHE[key] = result
    return result


def export_copy(info: Optional[dict], target_dir: Path, max_bytes: int = 100 * 1024 * 1024) -> Optional[dict]:
    """把已定位的本地文件复制到 target_dir，返回带副本路径的新信息。

    源文件不存在、超过体积上限或复制失败时原样返回，并把原因写进 note。
    """
    if not info or not info.get("exists") or not info.get("path"):
        return info
    size = int(info.get("size") or 0)
    if size > max_bytes:
        out = dict(info)
        out["note"] = f"文件 {size // 1048576}MB 超过 {max_bytes // 1048576}MB 上限，未复制，可直接读 source_path"
        out["source_path"] = info["path"]
        return out
    src_path = Path(info["path"])
    if not src_path.exists():
        return info
    target_dir.mkdir(parents=True, exist_ok=True)
    dst = target_dir / src_path.name
    if not dst.exists():
        try:
            shutil.copy2(src_path, dst)
        except OSError as exc:
            out = dict(info)
            out["note"] = f"复制失败: {exc}"
            out["source_path"] = info["path"]
            return out
    out = dict(info)
    out["source_path"] = info["path"]
    out["path"] = str(dst)
    out["exported"] = True
    return out

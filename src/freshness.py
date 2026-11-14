"""数据新鲜度：按需刷新解密副本与索引。

设计见 docs/重构方案-2026-11-13.md 第 19 节：
懒刷新加 TTL，只在有人查询时付出解密成本；多进程用锁文件互斥；
刷新失败不阻断查询，照常返回既有数据并把原因写进返回值。
"""
from __future__ import annotations

import json
import os
import time
from pathlib import Path

from . import decrypt, index
from .config import get_db_storage, get_wechat_files_dir
from .db.locator import LocalSource

DEFAULT_TTL = 60
FAIL_RETRY = 15
LOCK_STALE = 300


def state_path(src: LocalSource) -> Path:
    return src.root / "_index" / "refresh_state.json"


def lock_path(src: LocalSource) -> Path:
    return src.root / "_index" / "refresh.lock"


def ttl_seconds() -> int:
    """WECHAT_REFRESH_TTL：秒数。0 表示每次查询前都刷，负数表示关闭自动刷新。"""
    raw = os.environ.get("WECHAT_REFRESH_TTL", "").strip()
    if not raw:
        return DEFAULT_TTL
    try:
        return int(raw)
    except ValueError:
        return DEFAULT_TTL


def _read_state(src: LocalSource) -> dict:
    path = state_path(src)
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {}


def _write_state(src: LocalSource, payload: dict) -> None:
    path = state_path(src)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    except OSError:
        pass


def _acquire(src: LocalSource) -> bool:
    """抢刷新锁。持有超过 LOCK_STALE 秒视为残留锁。"""
    path = lock_path(src)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
    except OSError:
        return False
    try:
        fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        os.write(fd, f"{os.getpid()} {time.time():.0f}".encode())
        os.close(fd)
        return True
    except FileExistsError:
        try:
            if time.time() - path.stat().st_mtime > LOCK_STALE:
                path.unlink(missing_ok=True)
                return _acquire(src)
        except OSError:
            return False
        return False
    except OSError:
        return False


def _release(src: LocalSource) -> None:
    try:
        lock_path(src).unlink(missing_ok=True)
    except OSError:
        pass


def ensure(
    src: LocalSource,
    force: bool = False,
    ttl: int | None = None,
    update_index: bool = True,
) -> dict:
    """按需刷新。返回 refresh 元信息，任何异常都转成结果字段而不是抛出。"""
    ttl = ttl_seconds() if ttl is None else ttl
    if ttl < 0 and not force:
        return {"refreshed": False, "skipped": "disabled", "ttl_left": None}

    state = _read_state(src)
    now = time.time()
    last = float(state.get("last_attempt") or 0)
    interval = min(ttl, FAIL_RETRY) if state.get("last_skipped") else ttl
    left = max(0.0, interval - (now - last)) if ttl > 0 else 0.0
    if not force and left > 0:
        return {
            "refreshed": False,
            "skipped": "ttl",
            "ttl_left": round(left, 1),
            "last_changed": state.get("changed") or [],
        }

    files_dir = get_wechat_files_dir()
    if files_dir is None:
        return {"refreshed": False, "skipped": "no_source", "error": "未定位到微信数据目录"}

    db_storage = get_db_storage(files_dir)
    if db_storage is None:
        return {"refreshed": False, "skipped": "no_source", "error": "账号目录下没有 db_storage"}

    if not _acquire(src):
        return {"refreshed": False, "skipped": "locked", "ttl_left": 0.0}

    try:
        keys_file = Path(__file__).resolve().parent.parent / "keys.json"
        if not keys_file.exists():
            _write_state(src, {"last_attempt": now, "last_skipped": "no_keys"})
            return {"refreshed": False, "skipped": "no_keys", "error": f"找不到 {keys_file}"}

        result = decrypt.refresh(db_storage, src.root, keys_file, only_changed=True)
        changed = result.get("changed") or []
        index_result = None
        if changed and update_index:
            try:
                index_result = index.update(src)
            except Exception as exc:
                index_result = {"ok": False, "error": str(exc)}

        _write_state(
            src,
            {
                "last_attempt": now,
                "last_done": time.time(),
                "changed": changed,
                "seconds": result.get("seconds"),
                "failed": result.get("failed") or [],
            },
        )
        return {
            "refreshed": True,
            "skipped": "",
            "changed_dbs": changed,
            "failed": result.get("failed") or [],
            "seconds": result.get("seconds"),
            "ttl_left": round(float(ttl), 1) if ttl > 0 else 0.0,
            "index": index_result,
        }
    except Exception as exc:
        _write_state(src, {"last_attempt": now, "last_skipped": "error", "error": str(exc)})
        return {"refreshed": False, "skipped": "error", "error": str(exc)}
    finally:
        _release(src)


def peek_ttl_left(src: LocalSource) -> float | None:
    """不触发刷新，只算距下次允许刷新的秒数。"""
    ttl = ttl_seconds()
    if ttl <= 0:
        return None
    state = _read_state(src)
    last = float(state.get("last_attempt") or 0)
    return round(max(0.0, ttl - (time.time() - last)), 1)

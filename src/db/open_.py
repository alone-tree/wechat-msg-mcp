"""只读打开数据库。

依据 docs/总体设计.md 第 2 节：不修改 decrypted/ 下的任何文件。
open 使用 immutable=1，避免 SQLite 生成或读取 -wal / -shm 旁的临时文件。
"""
from __future__ import annotations

import sqlite3
from pathlib import Path


def connect_ro(path: str | Path) -> sqlite3.Connection:
    p = Path(path)
    if not p.exists():
        raise FileNotFoundError(f"数据库不存在: {p}")
    uri = f"file:{p.as_posix()}?mode=ro&immutable=1"
    conn = sqlite3.connect(uri, uri=True, timeout=30)
    conn.row_factory = sqlite3.Row
    return conn


def table_exists(conn: sqlite3.Connection, name: str) -> bool:
    row = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (name,)
    ).fetchone()
    return row is not None

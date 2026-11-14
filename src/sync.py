"""数据状态：新鲜度报告。

刷新已移到 src/freshness.py：进程内增量解密，不再起子进程。
本模块只负责 status，依据 docs/重构方案-2026-11-13.md 第 19 节。
"""
from __future__ import annotations

from datetime import datetime

from .db.locator import LocalSource
from .entities import load_contacts


def status(src: LocalSource) -> dict:
    contacts = load_contacts(src)
    groups = [c for c in contacts if c["kind"] == "group"]
    persons = [c for c in contacts if c["kind"] == "person"]
    info = src.detect()
    return {
        "state": "ok" if info.get("ready") else "not_ready",
        "data_cutoff": info.get("data_cutoff", ""),
        "root": info.get("root", ""),
        "message_dbs": info.get("message_dbs", 0),
        "contacts": len(persons),
        "groups": len(groups),
        "files": info.get("files", {}),
    }

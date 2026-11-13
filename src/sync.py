"""数据新鲜度：状态检查与刷新。

依据 docs/重构方案-2026-11-13.md 第 6.5 节：
status 报告数据截止时间与各库 mtime；sync 调既有 quick_decrypt 脚本刷新，
不在这里重新实现解密。
"""
from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

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


def refresh(src: LocalSource, timeout: int = 600) -> dict:
    """调用 scripts/quick_decrypt.py。需要 WECHAT_FILES_DIR 指向微信数据目录。"""
    script = Path(__file__).resolve().parent.parent / "scripts" / "quick_decrypt.py"
    if not script.exists():
        return {"ok": False, "error": f"找不到刷新脚本: {script}"}
    env = dict(os.environ)
    if not env.get("WECHAT_FILES_DIR"):
        return {
            "ok": False,
            "error": "未设置 WECHAT_FILES_DIR，无法刷新；请先设置微信数据目录，或先运行一次 start.ps1",
        }
    try:
        proc = subprocess.run(
            [sys.executable, str(script)],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            env=env,
            timeout=timeout,
        )
    except subprocess.TimeoutExpired:
        return {"ok": False, "error": f"刷新超时（{timeout} 秒）"}
    tail = [line for line in (proc.stdout or "").strip().splitlines() if line][-5:]
    return {
        "ok": proc.returncode == 0,
        "returncode": proc.returncode,
        "tail": tail,
        "error": (proc.stderr or "").strip()[-500:] if proc.returncode != 0 else "",
        "status": status(src),
    }

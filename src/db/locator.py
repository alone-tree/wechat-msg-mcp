"""数据源定位。

默认数据源是本地解密目录 decrypted/（模块分目录）。
WeChatDataAnalysis 输出为扁平目录，按账号一个文件夹，接口预留但本次不实现。
依据 docs/重构方案-2026-11-13.md 第 12 节。
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Protocol


class DataSource(Protocol):
    def detect(self) -> dict: ...
    def path(self, kind: str) -> Path: ...
    def message_dbs(self) -> list[Path]: ...
    def data_cutoff(self) -> str: ...


def _fmt_ts(ts: float) -> str:
    return datetime.fromtimestamp(ts).strftime("%Y-%m-%d %H:%M:%S")


def latest_copy(path: Path) -> Path:
    """同一个库的最新副本：正本与旁路副本（<名>.new）里取 mtime 较新且存在的。

    旁路副本用于正本被别的进程占用、无法原子替换时继续供新数据，
    读取方照常按可读文件查询，不需要知道背后发生了什么。
    """
    path = Path(path)
    alt = path.with_name(path.name + ".new")
    cands = [p for p in (path, alt) if p.exists()]
    if not cands:
        return path
    return max(cands, key=lambda p: p.stat().st_mtime)


def latest_in_dir(directory: Path, pattern: str) -> list[Path]:
    """目录下按同一库去重取最新：X.db 与 X.db.new 同时存在时只留较新的那个。

    分库扫描一律走这里，否则旁路副本会被当成另一个库，导致同一张表被查两遍。
    """
    picked: dict[str, Path] = {}
    for p in sorted(directory.glob(pattern)) + sorted(directory.glob(pattern + ".new")):
        if not p.is_file():
            continue
        base = p.name[:-4] if p.name.endswith(".new") else p.name
        cur = picked.get(base)
        try:
            newer = cur is None or p.stat().st_mtime > cur.stat().st_mtime
        except OSError:
            continue
        if newer:
            picked[base] = p
    return [picked[k] for k in sorted(picked)]


@dataclass
class LocalSource:
    """本地解密目录：contact/contact.db、message/message_N.db ..."""

    root: Path

    def __post_init__(self) -> None:
        self.root = Path(self.root).expanduser()
        if not self.root.exists():
            raise FileNotFoundError(f"解密目录不存在: {self.root}")

    def path(self, kind: str) -> Path:
        table = {
            "contact": self.root / "contact" / "contact.db",
            "session": self.root / "session" / "session.db",
            "hardlink": self.root / "hardlink" / "hardlink.db",
            "general": self.root / "general" / "general.db",
            "sns": self.root / "sns" / "sns.db",
            "favorite": self.root / "favorite" / "favorite.db",
            "media_dir": self.root / "message",
        }
        if kind not in table:
            raise KeyError(f"未知数据源类型: {kind}")
        if kind == "media_dir":
            return table[kind]
        # 正本被占用时刷新会写 <名>.new，读取方统一取最新的那个
        return latest_copy(table[kind])

    def message_dbs(self) -> list[Path]:
        """微信 4.x 的消息分库，排除资源库、媒体库与旧名。"""
        msg_dir = self.root / "message"
        if not msg_dir.exists():
            return []
        dbs = [
            p
            for p in latest_in_dir(msg_dir, "*.db")
            if p.stat().st_size >= 4096
            and not p.name.startswith(("message_fts", "message_resource", "media_", "weclaw"))
        ]
        if not dbs:
            dbs = [p for p in latest_in_dir(msg_dir, "msg_*.db") if p.stat().st_size >= 4096]
        return dbs

    def _max_stamp(self, files: list[Path]) -> str:
        stamps = [p.stat().st_mtime for p in files if p.exists()]
        return _fmt_ts(max(stamps)) if stamps else ""

    def data_cutoff(self, kind: str = "all") -> str:
        """按数据面返回截止时间。

        kind: message / sns / favorite / contact / session / all。
        分类取各自的库，避免把联系人缓存或头像库的时间算进消息与朋友圈，
        否则刚更新过联系人就推送会显示成「消息到几点」，误导使用者。
        """
        if kind == "message":
            return self._max_stamp(self.message_dbs())
        if kind == "sns":
            return self._max_stamp([self.path("sns")])
        if kind == "favorite":
            return self._max_stamp([self.path("favorite")])
        if kind == "contact":
            return self._max_stamp([self.path("contact")])
        if kind == "session":
            return self._max_stamp([self.path("session"), self.path("contact")])
        return self._max_stamp([self.path("contact"), self.path("session"), *self.message_dbs()])

    def detect(self) -> dict:
        files = [self.path("contact"), self.path("session"), *self.message_dbs()]
        return {
            "source": "local",
            "root": str(self.root),
            "contact_db": str(self.path("contact")),
            "message_dbs": len(self.message_dbs()),
            "ready": self.path("contact").exists(),
            "data_cutoff": self.data_cutoff(),
            "files": {p.name: _fmt_ts(p.stat().st_mtime) for p in files if p.exists()},
        }

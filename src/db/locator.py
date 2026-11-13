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
        return table[kind]

    def message_dbs(self) -> list[Path]:
        """微信 4.x 的消息分库，排除资源库、媒体库与旧名。"""
        msg_dir = self.root / "message"
        if not msg_dir.exists():
            return []
        dbs = [
            p
            for p in sorted(msg_dir.glob("*.db"))
            if p.stat().st_size >= 4096
            and not p.name.startswith(("message_fts", "message_resource", "media_", "weclaw"))
        ]
        if not dbs:
            dbs = sorted(p for p in msg_dir.glob("msg_*.db") if p.stat().st_size >= 4096)
        return dbs

    def data_cutoff(self) -> str:
        """数据截止时间：所有参与查询的库中最新的 mtime。"""
        files = [self.path("contact"), self.path("session"), *self.message_dbs()]
        stamps = [p.stat().st_mtime for p in files if p.exists()]
        return _fmt_ts(max(stamps)) if stamps else ""

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

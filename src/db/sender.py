"""发送者解析。

依据 docs/总体设计.md 第 3 节从旧 reader.py 抽取的核心技巧：
微信 4.x 每个 message 分库各有独立的 Name2Id，Msg_.real_sender_id 是本库 Name2Id 的 rowid，
不是 contact 表的 rowid，两者不能混用。user_name 为空串表示自己发送。
"""
from __future__ import annotations

import hashlib
import sqlite3
from typing import Optional


def username_to_md5(username: str) -> str:
    return hashlib.md5(username.encode()).hexdigest()


def build_local_sender_map(conn: sqlite3.Connection) -> dict[int, str]:
    """本库 Name2Id：rowid -> user_name（空串代表自己）。"""
    mapping: dict[int, str] = {}
    try:
        for rid, name in conn.execute("SELECT rowid, user_name FROM Name2Id"):
            if rid is not None:
                mapping[int(rid)] = name or ""
    except sqlite3.OperationalError:
        pass
    return mapping


def build_name2id(conn: sqlite3.Connection) -> dict[str, str]:
    """本库 Name2Id：md5(user_name) -> user_name，用于定位会话分表。"""
    mapping: dict[str, str] = {}
    try:
        for _rid, name in conn.execute("SELECT rowid, userName FROM Name2Id"):
            if name:
                mapping[username_to_md5(name)] = name
    except sqlite3.OperationalError:
        pass
    return mapping


def resolve_sender(
    real_sender_id, local_map: dict[int, str], my_username: str = ""
) -> tuple[Optional[str], bool]:
    """返回 (wxid 或 None, 是否自己发送)。查不到不猜。"""
    if real_sender_id is None or real_sender_id == "":
        return None, True
    try:
        rid = int(real_sender_id)
    except (TypeError, ValueError):
        s = str(real_sender_id)
        return s, bool(my_username) and s == my_username
    name = local_map.get(rid)
    if name is None:
        return None, False
    if name == "":
        return None, True
    return name, bool(my_username) and name == my_username


class ContactIndex:
    """username 与 contact.rowid 双索引，用于把 wxid 换成显示名。"""

    def __init__(self) -> None:
        self.by_username: dict[str, dict] = {}
        self.by_id: dict[int, dict] = {}

    def add(self, row: dict) -> None:
        self.by_username[row["username"]] = row
        if row.get("id"):
            self.by_id[int(row["id"])] = row

    @staticmethod
    def display_name(row: Optional[dict], fallback: str = "") -> str:
        if not row:
            return fallback
        return row.get("remark") or row.get("nickname") or row.get("username") or fallback

    def name_of(self, sender_id: Optional[str]) -> Optional[str]:
        if not sender_id:
            return None
        row = self.by_username.get(sender_id)
        if row:
            return self.display_name(row)
        try:
            row = self.by_id.get(int(sender_id))
        except (TypeError, ValueError):
            row = None
        return self.display_name(row, sender_id) if row else sender_id

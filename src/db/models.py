"""
数据模型定义
"""
from dataclasses import dataclass, field
from datetime import datetime
from typing import Optional


# 消息类型映射
MESSAGE_TYPES = {
    1: "文本",
    3: "图片",
    34: "语音",
    43: "视频",
    47: "表情",
    49: "链接/文件/小程序",
    10000: "系统消息",
    10002: "撤回消息",
}


@dataclass
class Contact:
    """联系人信息"""
    username: str           # 微信ID（wxid_xxx 或 群ID xxx@chatroom）
    remark: str = ""        # 备注名
    nickname: str = ""      # 昵称
    is_group: bool = False  # 是否为群聊

    @property
    def display_name(self) -> str:
        """优先显示备注名，其次昵称，最后微信ID"""
        return self.remark or self.nickname or self.username

    def __repr__(self):
        tag = "[群]" if self.is_group else "[联系人]"
        return f"{tag} {self.display_name} ({self.username})"


@dataclass
class Message:
    """聊天消息"""
    local_id: int
    server_id: int
    create_time: int            # Unix 时间戳（秒）
    local_type: int             # 消息类型（原始值）
    content: str                # 消息内容
    is_from_me: bool            # True=自己发送, False=对方发送
    sender_id: Optional[str]    # 群消息中的发送者 wxid
    sender_name: Optional[str]  # 发送者显示名称
    chat_username: str          # 所属会话的 username
    source_db: str = ""         # 来源数据库文件路径

    @property
    def datetime(self) -> datetime:
        return datetime.fromtimestamp(self.create_time)

    @property
    def type_name(self) -> str:
        base_type = self.local_type & 0xFFFF
        return MESSAGE_TYPES.get(base_type, f"未知({base_type})")

    @property
    def is_text(self) -> bool:
        return (self.local_type & 0xFFFF) == 1

    @property
    def is_system(self) -> bool:
        base = self.local_type & 0xFFFF
        return base in (10000, 10002)

    def to_dict(self) -> dict:
        return {
            "local_id": self.local_id,
            "server_id": self.server_id,
            "time": self.datetime.strftime("%Y-%m-%d %H:%M:%S"),
            "timestamp": self.create_time,
            "type": self.type_name,
            "content": self.content,
            "is_from_me": self.is_from_me,
            "sender_id": self.sender_id,
            "sender_name": self.sender_name,
            "chat_username": self.chat_username,
        }

    @property
    def media_tag(self) -> Optional[str]:
        """图片消息的简短标记（替代原始 XML）。语音的展示由渲染层实时转写。"""
        base = self.local_type & 0xFFFF
        if base == 3:
            return f"[图片 local_id={self.local_id}]"
        return None

    def format_text(self, sender_display: Optional[str] = None) -> str:
        """格式化为可读文本"""
        time_str = self.datetime.strftime("%Y-%m-%d %H:%M:%S")
        if self.is_from_me:
            who = "我"
        else:
            who = sender_display or self.sender_name or self.sender_id or "对方"

        if self.is_text:
            return f"[{time_str}] {who}: {self.content}"
        tag = self.media_tag
        if tag:
            return f"[{time_str}] {who}: {tag}"
        return f"[{time_str}] {who}: [{self.type_name}] {self.content[:100] if self.content else ''}"


@dataclass
class Session:
    """会话（最近联系列表中的条目）"""
    username: str           # 联系人/群 username
    display_name: str       # 显示名称
    last_message: str       # 最后一条消息摘要
    last_time: int          # 最后消息时间戳
    unread_count: int = 0   # 未读数量
    is_group: bool = False

    @property
    def last_datetime(self) -> datetime:
        return datetime.fromtimestamp(self.last_time) if self.last_time else datetime.min

    def to_dict(self) -> dict:
        return {
            "username": self.username,
            "display_name": self.display_name,
            "last_message": self.last_message,
            "last_time": self.last_datetime.strftime("%Y-%m-%d %H:%M:%S") if self.last_time else "",
            "unread_count": self.unread_count,
            "is_group": self.is_group,
        }

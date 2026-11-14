"""消息类型解析。

local_type 是 64 位打包值：低 32 位为主类型，高 32 位为子类型。
49 是应用消息，具体类别要看正文 XML 里的 type 字段。
依据 docs/重构方案-2026-11-13.md 第 11 节。
"""
from __future__ import annotations

import re
from typing import Optional

TYPE_NAMES: dict[int, str] = {
    1: "text",
    3: "image",
    34: "voice",
    43: "video",
    47: "emoticon",
    48: "location",
    49: "app",
    50: "voip",
    10000: "system",
    10002: "revoked",
}

APPMSG_TYPES: dict[int, str] = {
    1: "text",
    2: "image",
    3: "music",
    4: "video",
    5: "link",
    6: "file",
    7: "card",
    8: "sticker",
    17: "location",
    19: "chat_record",
    33: "mini_program",
    36: "mini_program",
    51: "channels",
    53: "app",
    57: "quote",
    62: "channels",
    63: "channels",
    87: "app",
    2000: "transfer",
    2001: "red_packet",
}

_APPMSG_TYPE_RE = re.compile(r"<type>\s*(?:<!\[CDATA\[)?\s*(\d+)\s*(?:\]\]>)?\s*</type>")


def split_local_type(local_type: int) -> tuple[int, int]:
    """返回 (主类型, 子类型)。"""
    try:
        v = int(local_type)
    except (TypeError, ValueError):
        return 0, 0
    return v & 0xFFFFFFFF, v >> 32


def type_name(local_type: int) -> str:
    main, _sub = split_local_type(local_type)
    return TYPE_NAMES.get(main, f"unknown({main})")


def appmsg_subtype(content: str) -> Optional[str]:
    """从 49 类型消息的 XML 里取 appmsg 子类型名，取不到返回 None。"""
    if not content:
        return None
    m = _APPMSG_TYPE_RE.search(content)
    if not m:
        return None
    return APPMSG_TYPES.get(int(m.group(1)), f"app_{m.group(1)}")


def classify(local_type: int, content: str = "") -> tuple[str, Optional[str]]:
    """返回 (类型名, 子类型名)。49 会进一步解析具体类别。"""
    name = type_name(local_type)
    if name == "app":
        return "app", appmsg_subtype(content)
    return name, None

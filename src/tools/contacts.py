"""
联系人相关 MCP Tools
"""
import json
from typing import Optional

from ..db.reader import WeChatDBReader


def list_contacts(
    reader: WeChatDBReader,
    keyword: str = "",
    include_groups: bool = True,
    limit: int = 100,
) -> str:
    """
    列出联系人和群聊。

    :param keyword: 按名称过滤（空=全部）
    :param include_groups: 是否包含群聊
    :param limit: 最多返回条数
    :return: JSON 格式字符串
    """
    contacts = reader.get_contacts(keyword=keyword, include_groups=include_groups)
    contacts = contacts[:limit]

    result = {
        "total": len(contacts),
        "contacts": [
            {
                "username": c.username,
                "display_name": c.display_name,
                "remark": c.remark,
                "nickname": c.nickname,
                "is_group": c.is_group,
            }
            for c in contacts
        ],
    }
    return json.dumps(result, ensure_ascii=False, indent=2)


def get_sessions(
    reader: WeChatDBReader,
    limit: int = 30,
) -> str:
    """
    获取最近会话列表（类似微信首页的聊天列表）。

    :param limit: 最多返回条数
    :return: JSON 格式字符串
    """
    sessions = reader.get_sessions(limit=limit)

    result = {
        "total": len(sessions),
        "sessions": [s.to_dict() for s in sessions],
    }
    return json.dumps(result, ensure_ascii=False, indent=2)


def find_contact(
    reader: WeChatDBReader,
    name: str,
) -> str:
    """
    精确查找某个联系人的详细信息。

    :param name: 联系人备注名/昵称/微信ID
    :return: JSON 格式字符串
    """
    contact = reader.get_contact(name)
    if not contact:
        return json.dumps(
            {"error": f"未找到联系人: {name}，请尝试用 list_contacts 查看完整列表"},
            ensure_ascii=False,
        )

    return json.dumps(
        {
            "username": contact.username,
            "display_name": contact.display_name,
            "remark": contact.remark,
            "nickname": contact.nickname,
            "is_group": contact.is_group,
        },
        ensure_ascii=False,
        indent=2,
    )

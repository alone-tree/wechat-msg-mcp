"""
消息相关 MCP Tools
"""
import json
from typing import Optional

from ..db.reader import WeChatDBReader
from ..db.models import Message


def _messages_to_output(messages: list[Message], reader: WeChatDBReader, fmt: str = "json") -> str:
    """将消息列表格式化输出"""
    if fmt == "text":
        lines = []
        for m in messages:
            # 获取会话联系人名称
            contact = reader.get_contact(m.chat_username)
            chat_name = contact.display_name if contact else m.chat_username
            lines.append(m.format_text())
        return "\n".join(lines) if lines else "（无消息）"
    else:
        return json.dumps(
            {
                "total": len(messages),
                "messages": [m.to_dict() for m in messages],
            },
            ensure_ascii=False,
            indent=2,
        )


def get_chat_history(
    reader: WeChatDBReader,
    contact_name: str,
    start_date: Optional[str] = None,
    end_date: Optional[str] = None,
    limit: int = 100,
    offset: int = 0,
    output_format: str = "text",
) -> str:
    """
    获取与某联系人/群聊的聊天记录。

    :param contact_name: 联系人备注名、昵称或微信ID（支持模糊匹配）
    :param start_date: 开始日期，格式 YYYY-MM-DD（为空则不限）
    :param end_date:   结束日期，格式 YYYY-MM-DD（为空则不限）
    :param limit:      最多返回条数（默认100，最大500）
    :param offset:     分页偏移（默认0）
    :param output_format: 输出格式 "text"（可读文本）或 "json"
    :return: 聊天记录字符串
    """
    limit = min(limit, 500)

    try:
        messages = reader.get_messages(
            contact_name=contact_name,
            start_date=start_date,
            end_date=end_date,
            limit=limit,
            offset=offset,
        )
    except ValueError as e:
        return json.dumps({"error": str(e)}, ensure_ascii=False)

    if not messages:
        period = ""
        if start_date or end_date:
            period = f"（{start_date or '开始'} ~ {end_date or '至今'}）"
        return f"与「{contact_name}」{period}没有找到聊天记录。"

    contact = reader.get_contact(contact_name)
    chat_name = contact.display_name if contact else contact_name

    if output_format == "text":
        lines = [f"=== 与「{chat_name}」的聊天记录，共 {len(messages)} 条 ===\n"]
        for m in messages:
            lines.append(m.format_text())
        return "\n".join(lines)
    else:
        return json.dumps(
            {
                "chat_name": chat_name,
                "total": len(messages),
                "messages": [m.to_dict() for m in messages],
            },
            ensure_ascii=False,
            indent=2,
        )


def search_messages(
    reader: WeChatDBReader,
    keyword: str,
    contact_name: Optional[str] = None,
    start_date: Optional[str] = None,
    end_date: Optional[str] = None,
    limit: int = 50,
    output_format: str = "text",
) -> str:
    """
    在聊天记录中搜索关键词。

    :param keyword: 搜索关键词
    :param contact_name: 限定搜索范围到某联系人/群（为空则全局搜索）
    :param start_date: 开始日期 YYYY-MM-DD
    :param end_date:   结束日期 YYYY-MM-DD
    :param limit:      最多返回条数（默认50）
    :param output_format: "text" 或 "json"
    :return: 搜索结果字符串
    """
    limit = min(limit, 200)

    messages = reader.search_messages(
        keyword=keyword,
        contact_name=contact_name,
        start_date=start_date,
        end_date=end_date,
        limit=limit,
    )

    if not messages:
        scope = f"「{contact_name}」" if contact_name else "所有聊天"
        return f"在{scope}中未找到包含「{keyword}」的消息。"

    if output_format == "text":
        lines = [f"=== 搜索「{keyword}」，共找到 {len(messages)} 条 ===\n"]
        for m in messages:
            contact = reader.get_contact(m.chat_username)
            chat_name = contact.display_name if contact else m.chat_username
            time_str = m.datetime.strftime("%Y-%m-%d %H:%M:%S")
            who = "我" if m.is_from_me else (m.sender_name or "对方")
            lines.append(f"[{time_str}] [{chat_name}] {who}: {m.content}")
        return "\n".join(lines)
    else:
        result = []
        for m in messages:
            contact = reader.get_contact(m.chat_username)
            d = m.to_dict()
            d["chat_name"] = contact.display_name if contact else m.chat_username
            result.append(d)
        return json.dumps(
            {"keyword": keyword, "total": len(result), "messages": result},
            ensure_ascii=False,
            indent=2,
        )


def get_recent_messages(
    reader: WeChatDBReader,
    days: int = 7,
    group_only: bool = False,
    limit: int = 100,
    output_format: str = "text",
    chat_names: list[str] | None = None,
) -> str:
    """
    获取最近 N 天的聊天消息（跨所有会话）。

    :param days: 最近几天（默认7天）
    :param group_only: 只返回群消息
    :param limit: 最多返回条数（默认100）
    :param output_format: "text" 或 "json"
    :param chat_names: 指定会话清单（备注名/群名/群ID），传入时只查这些会话
    :return: 聊天记录字符串
    """
    limit = min(limit, 500)
    messages = reader.get_recent_messages(
        days=days, group_only=group_only, limit=limit, chat_names=chat_names
    )

    scope = "指定会话" if chat_names else ("群聊" if group_only else "所有会话")
    if not messages:
        return f"最近 {days} 天的{scope}中没有找到消息。"

    if output_format == "text":
        lines = [f"=== 最近 {days} 天 {scope} 消息，共 {len(messages)} 条（按时间倒序）===\n"]
        for m in messages:
            contact = reader.get_contact(m.chat_username)
            chat_name = contact.display_name if contact else m.chat_username
            time_str = m.datetime.strftime("%m-%d %H:%M")
            who = "我" if m.is_from_me else (m.sender_name or "对方")
            preview = m.content[:80].replace("\n", " ")
            lines.append(f"[{time_str}] [{chat_name}] {who}: {preview}")
        return "\n".join(lines)
    else:
        result = []
        for m in messages:
            contact = reader.get_contact(m.chat_username)
            d = m.to_dict()
            d["chat_name"] = contact.display_name if contact else m.chat_username
            result.append(d)
        return json.dumps(
            {"days": days, "total": len(result), "messages": result},
            ensure_ascii=False,
            indent=2,
        )

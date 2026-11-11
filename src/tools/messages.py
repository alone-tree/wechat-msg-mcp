"""
消息相关 MCP Tools
"""
import json
import os
from typing import Optional

from ..db.reader import WeChatDBReader
from ..db.models import Message


def _message_line(reader: WeChatDBReader, m: Message, chat_name: str = "", time_fmt: str = "%Y-%m-%d %H:%M:%S", content_limit: int = 0) -> str:
    """单条消息的文本渲染：语音实时转写直接出文字，图片用短标记，其余原文。"""
    contact = reader.get_contact(m.chat_username) if not chat_name else None
    name = chat_name or (contact.display_name if contact else m.chat_username)
    time_str = m.datetime.strftime(time_fmt)
    who = "我" if m.is_from_me else (m.sender_name or "对方")
    prefix = f"[{name}] " if chat_name else ""
    voice = _voice_display(reader, m)
    tag = voice or m.media_tag
    if tag:
        content = tag
    else:
        content = m.content
        if content_limit:
            content = content[:content_limit].replace("\n", " ")
    return f"[{time_str}] {prefix}{who}: {content}"


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


def _voice_display(reader: WeChatDBReader, m: Message) -> str | None:
    """语音消息的展示文本：本地转写直接出文字，失败才标注。非语音返回 None。"""
    if (m.local_type & 0xFFFF) != 34:
        return None
    from ..db.voice import transcribe_voice_blob

    blob = reader.get_voice_blob(m.chat_username, m.local_id)
    if not blob:
        return "（语音 转写失败：数据缺失）"
    work_dir = os.path.join(str(reader.root), "exports", "voice")
    text, duration = transcribe_voice_blob(blob, work_dir)
    dur = f"长{duration:.0f}秒" if duration else ""
    if text is None:
        return f"（语音 {dur} 转写失败）" if dur else "（语音 转写失败）"
    if text == "":
        return f"（语音 {dur} 无有效内容）" if dur else "（语音 无有效内容）"
    return f"（语音 {dur}）{text}"


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
            lines.append(_message_line(reader, m))
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
    contact_names: Optional[list[str]] = None,
) -> str:
    """
    在聊天记录中搜索关键词。

    :param keyword: 搜索关键词
    :param contact_name: 限定搜索范围到某联系人/群（为空则全局搜索）
    :param start_date: 开始日期 YYYY-MM-DD
    :param end_date:   结束日期 YYYY-MM-DD
    :param limit:      最多返回条数（默认50）
    :param output_format: "text" 或 "json"
    :param contact_names: 多会话清单（备注名/群名/群ID），与 contact_name 二选一，优先使用
    :return: 搜索结果字符串
    """
    limit = min(limit, 200)

    missing: list[str] = []
    if contact_names:
        usernames = []
        for n in contact_names:
            if not n:
                continue
            contact = reader.get_contact(n)
            if contact:
                usernames.append(contact.username)
            else:
                missing.append(n)
        if not usernames:
            scope = "、".join(contact_names)
            return f"名单中的会话均未找到：{scope}。"
        messages = reader.search_messages(
            keyword=keyword,
            start_date=start_date,
            end_date=end_date,
            limit=limit,
            contact_usernames=usernames,
        )
    else:
        messages = reader.search_messages(
            keyword=keyword,
            contact_name=contact_name,
            start_date=start_date,
            end_date=end_date,
            limit=limit,
        )

    if not messages:
        scope = f"「{contact_name}」" if contact_name else (
            f"名单（{len(contact_names)}个会话）" if contact_names else "所有聊天"
        )
        suffix = f"（注意：以下名字未匹配到会话：{'、'.join(missing)}）" if missing else ""
        return f"在{scope}中未找到包含「{keyword}」的消息。{suffix}"

    missing_note = f"\n\n注意：名单中以下项未匹配到会话，已跳过：{'、'.join(missing)}" if missing else ""

    if output_format == "text":
        scope_label = "、".join(contact_names) if contact_names else (contact_name or "所有聊天")
        lines = [f"=== 搜索「{keyword}」（范围：{scope_label}），共找到 {len(messages)} 条 ===\n"]
        for m in messages:
            contact = reader.get_contact(m.chat_username)
            chat_name = contact.display_name if contact else m.chat_username
            lines.append(_message_line(reader, m, chat_name=chat_name))
        return "\n".join(lines) + missing_note
    else:
        result = []
        for m in messages:
            contact = reader.get_contact(m.chat_username)
            d = m.to_dict()
            d["chat_name"] = contact.display_name if contact else m.chat_username
            result.append(d)
        payload = {"keyword": keyword, "total": len(result), "messages": result}
        if missing:
            payload["unmatched_names"] = missing
        return json.dumps(payload, ensure_ascii=False, indent=2)


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
            lines.append(_message_line(reader, m, chat_name=m.chat_username, time_fmt="%m-%d %H:%M", content_limit=80))
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


def export_voice(
    reader: WeChatDBReader,
    contact_name: str,
    local_id: int,
) -> str:
    """
    导出一条语音消息为 wav 临时文件，供音频转写工具转成文字。

    文件是临时缓存（同一语音重复导出会复用已生成的文件），可随时整体清空。

    :param contact_name: 会话名/群名/群ID
    :param local_id: 消息的 local_id（文本流里 [语音 local_id=N] 标记中的 N）
    :return: wav 路径与后续转写提示
    """
    contact = reader.get_contact(contact_name)
    username = contact.username if contact else contact_name
    result = reader.get_voice_wav(username, int(local_id))
    if not result:
        return (
            f"未找到语音：会话={contact_name}，local_id={local_id}。"
            "可能该消息不是语音，或语音数据尚未同步到本地库。"
        )
    return (
        f"语音已导出：{result['wav_path']}\n"
        f"时长：约{result['duration']:.0f}秒\n"
        f"下一步：用音频转写 CLI（D:\\HermesSync\\tools\\audio-transcribe）转写该 wav，"
        f"转写结果即语音的文字内容，看完后无需保留文件。"
    )

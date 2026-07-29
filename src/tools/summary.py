"""
聊天汇总 MCP Tools
提供按时间段、按联系人的统计和摘要功能
"""
import json
from collections import defaultdict
from datetime import datetime, timedelta
from typing import Optional

from ..db.reader import WeChatDBReader
from ..db.models import Message


def _date_range_for_period(period: str) -> tuple[str, str]:
    """将预设时间段转为 (start_date, end_date)"""
    today = datetime.now().date()
    if period == "today":
        return str(today), str(today)
    elif period == "yesterday":
        d = today - timedelta(days=1)
        return str(d), str(d)
    elif period == "this_week":
        start = today - timedelta(days=today.weekday())
        return str(start), str(today)
    elif period == "last_week":
        start = today - timedelta(days=today.weekday() + 7)
        end = start + timedelta(days=6)
        return str(start), str(end)
    elif period == "this_month":
        start = today.replace(day=1)
        return str(start), str(today)
    elif period == "last_7_days":
        start = today - timedelta(days=6)
        return str(start), str(today)
    elif period == "last_30_days":
        start = today - timedelta(days=29)
        return str(start), str(today)
    else:
        # 默认最近7天
        start = today - timedelta(days=6)
        return str(start), str(today)


def summarize_chat(
    reader: WeChatDBReader,
    contact_name: str,
    period: str = "last_7_days",
    start_date: Optional[str] = None,
    end_date: Optional[str] = None,
) -> str:
    """
    汇总与某联系人/群的对话统计信息。

    :param contact_name: 联系人备注名/昵称/微信ID
    :param period: 预设时间段：today / yesterday / this_week / last_week /
                   this_month / last_7_days / last_30_days
                   （start_date/end_date 有值时忽略此参数）
    :param start_date: 自定义开始日期 YYYY-MM-DD
    :param end_date:   自定义结束日期 YYYY-MM-DD
    :return: 可读的统计文本
    """
    if not start_date and not end_date:
        start_date, end_date = _date_range_for_period(period)

    try:
        messages = reader.get_messages(
            contact_name=contact_name,
            start_date=start_date,
            end_date=end_date,
            limit=2000,
        )
    except ValueError as e:
        return json.dumps({"error": str(e)}, ensure_ascii=False)

    contact = reader.get_contact(contact_name)
    chat_name = contact.display_name if contact else contact_name
    is_group = contact.is_group if contact else False

    if not messages:
        return f"「{chat_name}」在 {start_date} ~ {end_date} 期间没有聊天记录。"

    # ---- 统计 ----
    total = len(messages)
    my_count = sum(1 for m in messages if m.is_from_me)
    other_count = total - my_count

    # 按日期分组
    by_date: dict[str, list[Message]] = defaultdict(list)
    for m in messages:
        day = m.datetime.strftime("%Y-%m-%d")
        by_date[day].append(m)

    # 活跃时段分布（按小时）
    hour_dist: dict[int, int] = defaultdict(int)
    for m in messages:
        hour_dist[m.datetime.hour] += 1

    peak_hour = max(hour_dist, key=hour_dist.get) if hour_dist else 0

    # 仅统计文本消息做内容摘要
    text_msgs = [m for m in messages if m.is_text and m.content]

    # 群聊成员发言统计
    member_stats: dict[str, int] = defaultdict(int)
    if is_group:
        for m in messages:
            if m.is_from_me:
                member_stats["我"] += 1
            elif m.sender_name or m.sender_id:
                member_stats[m.sender_name or m.sender_id] += 1

    # ---- 输出 ----
    lines = [
        f"=== 与「{chat_name}」的聊天汇总 ({start_date} ~ {end_date}) ===",
        "",
        f"📊 消息统计",
        f"  总消息数：{total} 条",
        f"  我发送：{my_count} 条 ({my_count*100//total}%)" if total else "",
        f"  对方发送：{other_count} 条 ({other_count*100//total}%)" if total else "",
        f"  活跃天数：{len(by_date)} 天",
        f"  最活跃时段：{peak_hour:02d}:00 ~ {peak_hour:02d}:59",
        "",
        f"📅 每日消息量",
    ]

    for day in sorted(by_date.keys()):
        day_msgs = by_date[day]
        bar = "█" * min(len(day_msgs) // 5 + 1, 20)
        lines.append(f"  {day}: {len(day_msgs):4d} 条  {bar}")

    if is_group and member_stats:
        lines.extend([
            "",
            f"👥 群成员发言 Top 10",
        ])
        sorted_members = sorted(member_stats.items(), key=lambda x: x[1], reverse=True)
        for name, count in sorted_members[:10]:
            lines.append(f"  {name}: {count} 条")

    # 最近几条文本消息作为摘要
    if text_msgs:
        lines.extend([
            "",
            f"💬 最近消息摘要（最后5条文本）",
        ])
        for m in text_msgs[-5:]:
            who = "我" if m.is_from_me else (m.sender_name or "对方")
            time_str = m.datetime.strftime("%m-%d %H:%M")
            preview = m.content[:60].replace("\n", " ")
            lines.append(f"  [{time_str}] {who}: {preview}")

    return "\n".join(line for line in lines if line is not None)


def chat_stats_overview(
    reader: WeChatDBReader,
    period: str = "last_7_days",
    top_n: int = 10,
) -> str:
    """
    跨所有会话的聊天统计总览。

    :param period: 时间段（同 summarize_chat）
    :param top_n: 显示消息最多的前 N 个会话
    :return: 可读统计文本
    """
    start_date, end_date = _date_range_for_period(period)

    sessions = reader.get_sessions(limit=100)
    if not sessions:
        return "没有找到任何会话记录。"

    session_counts: list[tuple[str, str, int]] = []  # (username, display_name, count)

    for session in sessions[:50]:  # 最多查50个会话
        try:
            msgs = reader._query_messages_by_username(
                session.username,
                start_date=start_date,
                end_date=end_date,
                limit=2000,
            )
            if msgs:
                session_counts.append((
                    session.username,
                    session.display_name,
                    len(msgs),
                ))
        except Exception:
            continue

    if not session_counts:
        return f"{start_date} ~ {end_date} 期间没有找到聊天记录。"

    session_counts.sort(key=lambda x: x[2], reverse=True)
    total_messages = sum(c for _, _, c in session_counts)

    lines = [
        f"=== 聊天总览 ({start_date} ~ {end_date}) ===",
        "",
        f"📊 总体数据",
        f"  活跃会话数：{len(session_counts)} 个",
        f"  总消息数：{total_messages} 条",
        "",
        f"🏆 最活跃的 {min(top_n, len(session_counts))} 个会话",
    ]

    for i, (username, name, count) in enumerate(session_counts[:top_n], 1):
        pct = count * 100 // total_messages if total_messages else 0
        tag = "[群]" if username.endswith("@chatroom") else "    "
        lines.append(f"  {i:2d}. {tag} {name}: {count} 条 ({pct}%)")

    return "\n".join(lines)

"""年度报告的聚合指标与文本渲染。

纯计算模块：不打开数据库、不写文件，方便单测。
脱敏约定：对外只出现 sha1 前 8 位、地址簿显示名、聚合数字，
不出现 wxid、真实群名与聊天正文。
依据 docs/重构方案-2026-11-13.md 第 10 节与第 11 节。
"""
from __future__ import annotations

import hashlib
import os
import re
from collections import Counter
from datetime import date, datetime
from typing import Callable, Iterable, Optional

from src.db.types import appmsg_subtype, split_local_type, type_name

TOP_PEOPLE = 10
TOP_GROUPS = 10
TOP_SPEAKERS = 5
TOP_FILE_EXT = 10
LATE_NIGHT_START = 0
LATE_NIGHT_END = 6
SYSTEM_LABELS = {10000: "system", 10002: "revoked"}

_VOICE_LEN_RE = re.compile(r'length="(\d+)"')
_TITLE_RE = re.compile(r"<title>(.*?)</title>", re.S)


def hash8(value: str) -> str:
    """sha1 前 8 位，用作脱敏标识。"""
    return hashlib.sha1((value or "").encode("utf-8", "replace")).hexdigest()[:8]


def chat_kind(username: str) -> str:
    """会话类别：group / official / single。"""
    if not username:
        return "unknown"
    if username.endswith("@chatroom"):
        return "group"
    if username.startswith("gh_"):
        return "official"
    if username in {"notifymessage", "weixin", "newsapp", "floatbottle"}:
        return "official"
    return "single"


def describe_type(local_type, text: str = "") -> tuple[int, Optional[int], str, Optional[str]]:
    """返回 (主类型, 子类型, 细分标签, 应用子类名)。

    49 应用消息按正文 XML 的 appmsg type 再分，标签写成 app:link 这样的形式。
    """
    main, _sub = split_local_type(local_type)
    if main == 49:
        sub_name = appmsg_subtype(text or "")
        label = f"app:{sub_name}" if sub_name else "app"
        return main, _sub, label, sub_name
    return main, _sub, type_name(local_type), None


def longest_streak(day_ordinals: Iterable[int]) -> int:
    """最长连续天数：输入 date.toordinal 的集合或序列。"""
    days = sorted(set(day_ordinals))
    best = 0
    run = 0
    prev = None
    for d in days:
        run = run + 1 if prev is not None and d == prev + 1 else 1
        prev = d
        best = max(best, run)
    return best


def median_of(values: Iterable[float]) -> Optional[float]:
    data = sorted(values)
    if not data:
        return None
    n = len(data)
    mid = n // 2
    if n % 2:
        return float(data[mid])
    return (data[mid - 1] + data[mid]) / 2.0


def _ratio(part: int, whole: int) -> float:
    return round(part / whole, 4) if whole else 0.0


def _fmt_ts(ts: Optional[int]) -> str:
    if not ts:
        return ""
    return datetime.fromtimestamp(int(ts)).strftime("%Y-%m-%d %H:%M:%S")


def _fmt_day(ordinal: Optional[int]) -> str:
    if not ordinal:
        return ""
    return date.fromordinal(int(ordinal)).isoformat()


class AnnualAggregator:
    """把逐条消息折叠成年度聚合。单进程、单遍扫描、常数级内存外的两个累加器。"""

    def __init__(self, year: int):
        self.year = year
        self.total = 0
        self.chat_messages = 0
        self.system = 0
        self.revoked = 0
        self.sent = 0
        self.received = 0
        self.kind_messages: Counter[str] = Counter()
        self.types: Counter[str] = Counter()
        self.monthly: Counter[tuple[str, str]] = Counter()
        self.by_month: Counter[str] = Counter()
        self.by_week: Counter[str] = Counter()
        self.by_hour: Counter[int] = Counter()
        self.by_weekday: Counter[int] = Counter()
        self.by_day: Counter[int] = Counter()
        self.late_night = 0
        self.voice_seconds = 0.0
        self.voice_count = 0
        self.file_ext: Counter[str] = Counter()
        self.max_length = 0
        self.max_length_ts: Optional[int] = None
        self.chats: dict[str, dict] = {}
        self.reply_deltas: list[int] = []
        self._prev: dict[str, tuple[int, bool]] = {}

    def add(
        self,
        chat: str,
        kind: str,
        ts: int,
        local_type,
        sender: Optional[str],
        text: str,
        is_self: bool,
    ) -> tuple[str, str]:
        """记录一条消息，返回 (主类型名, 应用子类名) 供明细行复用。"""
        main, _sub, label, sub_name = describe_type(local_type, text)
        main_name = "app" if main == 49 else label
        moment = datetime.fromtimestamp(int(ts))
        month = f"{moment.year}-{moment.month:02d}"

        self.total += 1
        self.types[label] += 1
        self.monthly[(month, label)] += 1

        stat = self.chats.get(chat)
        if stat is None:
            stat = {
                "kind": kind,
                "total": 0,
                "sent": 0,
                "received": 0,
                "system": 0,
                "revoked": 0,
                "first_ts": int(ts),
                "last_ts": int(ts),
                "days": set(),
                "speakers": Counter(),
            }
            self.chats[chat] = stat

        if main in SYSTEM_LABELS:
            if main == 10000:
                self.system += 1
                stat["system"] += 1
            else:
                self.revoked += 1
                stat["revoked"] += 1
            return main_name, sub_name or ""

        self.chat_messages += 1
        if is_self:
            self.sent += 1
            stat["sent"] += 1
        else:
            self.received += 1
            stat["received"] += 1
        self.kind_messages[kind] += 1
        self.by_month[month] += 1
        iso = moment.isocalendar()
        self.by_week[f"{iso[0]}-W{iso[1]:02d}"] += 1
        self.by_hour[moment.hour] += 1
        self.by_weekday[moment.weekday()] += 1
        day = moment.date().toordinal()
        self.by_day[day] += 1
        if LATE_NIGHT_START <= moment.hour < LATE_NIGHT_END:
            self.late_night += 1

        stat["total"] += 1
        stat["first_ts"] = min(stat["first_ts"], int(ts))
        stat["last_ts"] = max(stat["last_ts"], int(ts))
        stat["days"].add(day)
        stat["speakers"][sender or ""] += 1

        length = len(text or "")
        if main == 1 and length > self.max_length:
            # 只统计文本消息：合并转发与引用消息的正文是 XML，长度没有意义
            self.max_length = length
            self.max_length_ts = int(ts)

        if main == 34:
            m = _VOICE_LEN_RE.search(text or "")
            if m:
                self.voice_seconds += int(m.group(1)) / 1000.0
                self.voice_count += 1
        elif main == 49 and sub_name == "file":
            m = _TITLE_RE.search(text or "")
            if m:
                ext = os.path.splitext(m.group(1).strip())[1].lower().lstrip(".")
                if ext and ext.isalnum():
                    self.file_ext[ext] += 1

        prev = self._prev.get(chat)
        if prev is not None and prev[1] != is_self and int(ts) >= prev[0]:
            self.reply_deltas.append(int(ts) - prev[0])
        self._prev[chat] = (int(ts), is_self)
        return main_name, sub_name or ""

    # ------------------------------------------------------------------ #
    # 汇总                                                                 #
    # ------------------------------------------------------------------ #

    def finish(
        self,
        name_of: Callable[[str], str],
        extra: Optional[dict] = None,
    ) -> dict:
        people: list[dict] = []
        groups: list[dict] = []
        official_total = 0
        unknown_total = 0
        for chat, st in self.chats.items():
            if st["kind"] == "single":
                people.append(
                    {
                        "chat_hash": hash8(chat),
                        "name": name_of(chat),
                        "total": st["total"],
                        "received": st["received"],
                        "sent": st["sent"],
                        "share": _ratio(st["total"], self.chat_messages),
                        "my_ratio": _ratio(st["sent"], st["total"]),
                        "first_time": _fmt_ts(st["first_ts"]),
                        "last_time": _fmt_ts(st["last_ts"]),
                        "active_days": len(st["days"]),
                    }
                )
            elif st["kind"] == "group":
                speakers = [
                    {
                        "name": "我" if speaker == "" else ("未知成员" if speaker == "unknown" else name_of(speaker)),
                        "hash": "self" if speaker == "" else hash8(speaker),
                        "count": count,
                    }
                    for speaker, count in st["speakers"].most_common(TOP_SPEAKERS)
                ]
                groups.append(
                    {
                        "chat_hash": hash8(chat),
                        "label": "群-" + hash8(chat),
                        "total": st["total"],
                        "mine": st["sent"],
                        "my_ratio": _ratio(st["sent"], st["total"]),
                        "active_days": len(st["days"]),
                        "top_speakers": speakers,
                    }
                )
            elif st["kind"] == "official":
                official_total += st["total"]
            else:
                unknown_total += st["total"]

        people.sort(key=lambda r: (-r["total"], r["name"]))
        groups.sort(key=lambda r: (-r["total"], r["label"]))

        day_ordinals = sorted(self.by_day)
        busiest = max(self.by_day.items(), key=lambda kv: kv[1]) if self.by_day else None
        reply_median = median_of(self.reply_deltas)

        months = sorted(self.by_month)
        monthly = [
            {"month": month, "type": label, "count": count}
            for (month, label), count in sorted(self.monthly.items())
        ]
        types = [
            {"type": label, "count": count, "share": _ratio(count, self.total)}
            for label, count in self.types.most_common()
        ]

        summary = {
            "year": self.year,
            "overview": {
                "messages": self.total,
                "chat_messages": self.chat_messages,
                "sent": self.sent,
                "received": self.received,
                "sent_ratio": _ratio(self.sent, self.chat_messages),
                "system": self.system,
                "revoked": self.revoked,
                "conversations": len(self.chats),
                "single_chats": sum(1 for s in self.chats.values() if s["kind"] == "single"),
                "group_chats": sum(1 for s in self.chats.values() if s["kind"] == "group"),
                "official_chats": sum(1 for s in self.chats.values() if s["kind"] == "official"),
                "single_messages": self.kind_messages.get("single", 0),
                "group_messages": self.kind_messages.get("group", 0),
                "official_messages": official_total,
                "unknown_messages": unknown_total,
                "single_ratio": _ratio(self.kind_messages.get("single", 0), self.chat_messages),
                "group_ratio": _ratio(self.kind_messages.get("group", 0), self.chat_messages),
                "official_ratio": _ratio(official_total, self.chat_messages),
                "active_days": len(day_ordinals),
                "first_time": _fmt_day(day_ordinals[0]) if day_ordinals else "",
                "last_time": _fmt_day(day_ordinals[-1]) if day_ordinals else "",
            },
            "people": people,
            "groups": groups,
            "types": types,
            "monthly": monthly,
            "time": {
                "by_month": dict(zip(months, (self.by_month[m] for m in months))),
                "by_week": dict(sorted(self.by_week.items())),
                "by_hour": [self.by_hour.get(h, 0) for h in range(24)],
                "by_weekday": [self.by_weekday.get(d, 0) for d in range(7)],
                "busiest_day": (
                    {"date": _fmt_day(busiest[0]), "count": busiest[1]} if busiest else {}
                ),
                "late_night": {
                    "hour_from": LATE_NIGHT_START,
                    "hour_to": LATE_NIGHT_END,
                    "count": self.late_night,
                    "ratio": _ratio(self.late_night, self.chat_messages),
                },
                "longest_streak_days": longest_streak(day_ordinals),
                "active_days": len(day_ordinals),
            },
            "types_extra": {
                "voice_seconds": round(self.voice_seconds, 1),
                "voice_count": self.voice_count,
                "file_ext_top": self.file_ext.most_common(TOP_FILE_EXT),
            },
            "extremes": {
                "longest_message_length": self.max_length,
                "longest_message_time": _fmt_ts(self.max_length_ts),
                "single_day_peak": (
                    {"date": _fmt_day(busiest[0]), "count": busiest[1]} if busiest else {}
                ),
                "reply_interval_median_seconds": (
                    round(reply_median, 1) if reply_median is not None else None
                ),
                "reply_samples": len(self.reply_deltas),
            },
            "relations": extra or {},
        }
        return summary


# ---------------------------------------------------------------------- #
# markdown 渲染                                                            #
# ---------------------------------------------------------------------- #

_WEEKDAYS = ["周一", "周二", "周三", "周四", "周五", "周六", "周日"]


def _pct(value: float) -> str:
    return f"{value * 100:.1f}%"


def _cell(value) -> str:
    """表格单元格清洗：显示名里可能出现竖线、星号等，会破坏 markdown 表格。"""
    text = str(value or "")
    for char in ("|", "*", "~", "$", "\n", "\r"):
        text = text.replace(char, " ")
    return text.strip()


def render_markdown(summary: dict, meta: dict) -> str:
    """一页中文摘要。不使用加粗、波浪号与美元符号，避免 md 渲染问题。"""
    ov = summary["overview"]
    time_ = summary["time"]
    ext = summary["types_extra"]
    extremes = summary["extremes"]
    relations = summary.get("relations") or {}

    lines: list[str] = []
    lines.append(f"# {summary['year']} 年微信聊天年度报告")
    lines.append("")
    lines.append(
        f"数据截止 {meta.get('data_cutoff', '')}，生成于 {meta.get('generated_at', '')}。"
        "全部为聚合结果，不含正文与真实 ID；群用 sha1 前 8 位标识，单聊用地址簿显示名。"
    )
    lines.append("")

    lines.append("## 总览")
    lines.append("")
    lines.append("| 指标 | 数值 |")
    lines.append("|---|---|")
    lines.append(f"| 消息总条数 | {ov['messages']:,} |")
    lines.append(f"| 聊天句数（不含系统与撤回） | {ov['chat_messages']:,} |")
    lines.append(f"| 发送 / 接收 | {ov['sent']:,} / {ov['received']:,} |")
    lines.append(f"| 我的发送占比 | {_pct(ov['sent_ratio'])} |")
    lines.append(f"| 系统消息 / 撤回 | {ov['system']:,} / {ov['revoked']:,} |")
    lines.append(
        f"| 会话数（单聊 / 群 / 公众号） | {ov['conversations']:,}"
        f"（{ov['single_chats']} / {ov['group_chats']} / {ov['official_chats']}） |"
    )
    lines.append(
        f"| 聊天句数（单聊 / 群 / 公众号） | {ov['chat_messages']:,}"
        f"（{ov['single_messages']:,} / {ov['group_messages']:,} / {ov['official_messages']:,}） |"
    )
    lines.append(f"| 活跃天数 | {ov['active_days']} |")
    lines.append(f"| 首末消息日期 | {ov['first_time']} 至 {ov['last_time']} |")
    lines.append("")

    lines.append(f"## 按人 Top{TOP_PEOPLE}（单聊）")
    lines.append("")
    if summary["people"]:
        lines.append("| # | 联系人 | 总条数 | 接收 | 发送 | 我的占比 | 活跃天数 | 首次 | 末次 |")
        lines.append("|---|---|---|---|---|---|---|---|---|")
        for i, row in enumerate(summary["people"][:TOP_PEOPLE], 1):
            lines.append(
                f"| {i} | {_cell(row['name'])} | {row['total']:,} | {row['received']:,} |"
                f" {row['sent']:,} | {_pct(row['my_ratio'])} | {row['active_days']} |"
                f" {row['first_time'][:10]} | {row['last_time'][:10]} |"
            )
    else:
        lines.append("本年度没有单聊消息。")
    lines.append("")

    lines.append(f"## 按群 Top{TOP_GROUPS}")
    lines.append("")
    if summary["groups"]:
        lines.append("| # | 群 | 总条数 | 我的条数 | 我的占比 | 活跃天数 | Top 发言人 |")
        lines.append("|---|---|---|---|---|---|---|")
        for i, row in enumerate(summary["groups"][:TOP_GROUPS], 1):
            speakers = "、".join(
                f"{_cell(s['name'])} {s['count']:,}" for s in row["top_speakers"]
            )
            lines.append(
                f"| {i} | {row['label']} | {row['total']:,} | {row['mine']:,} |"
                f" {_pct(row['my_ratio'])} | {row['active_days']} | {speakers} |"
            )
    else:
        lines.append("本年度没有群聊消息。")
    lines.append("")

    lines.append("## 类型分布")
    lines.append("")
    lines.append("| 类型 | 条数 | 占比 |")
    lines.append("|---|---|---|")
    for row in summary["types"][:12]:
        lines.append(f"| {row['type']} | {row['count']:,} | {_pct(row['share'])} |")
    if ext.get("voice_count"):
        lines.append(
            f"| 语音总时长 | {ext['voice_seconds']:.0f} 秒（{ext['voice_count']:,} 条） | - |"
        )
    if ext.get("file_ext_top"):
        top_ext = "、".join(f".{name} {count}" for name, count in ext["file_ext_top"])
        lines.append(f"| 文件扩展名 Top | {top_ext} | - |")
    lines.append("")

    lines.append("## 时间分布")
    lines.append("")
    months = list(time_["by_month"].items())
    month_cells = " | ".join(f"{m} {c:,}" for m, c in months)
    lines.append(f"按月（聊天句数）：{month_cells}")
    lines.append("")
    hours = time_["by_hour"]
    top_hours = sorted(range(24), key=lambda h: -hours[h])[:5]
    hour_text = "、".join(f"{h:02d} 点 {hours[h]:,}" for h in top_hours if hours[h])
    lines.append(f"按小时最忙：{hour_text}")
    lines.append("")
    weekday_text = "、".join(
        f"{_WEEKDAYS[i]} {count:,}" for i, count in enumerate(time_["by_weekday"])
    )
    lines.append(f"按星期：{weekday_text}")
    lines.append("")
    busiest = time_.get("busiest_day") or {}
    late = time_["late_night"]
    lines.append(
        f"最忙一天：{busiest.get('date', '')}，{busiest.get('count', 0):,} 条；"
        f"深夜（{late['hour_from']} 点至 {late['hour_to']} 点）占比 {_pct(late['ratio'])}；"
        f"最长连续聊天 {time_['longest_streak_days']} 天。"
    )
    lines.append("")

    lines.append("## 新增关系（近似口径）")
    lines.append("")
    new_friends = relations.get("new_friends") or {}
    lines.append(
        f"新增好友（近似）：{new_friends.get('count', 0)}，口径为 general.db "
        "FMessageTable 里 type=37 的当年好友申请条数，不等于最终通过数；"
        "contact.extra_buffer 未发现可靠的添加时间字段，无法做精确统计。"
    )
    new_chats = relations.get("new_chats") or {}
    lines.append(
        f"新出现会话（近似）：{new_chats.get('count', 0)}，其中群 {new_chats.get('groups', 0)} 个，"
        "口径为全量数据中首条消息落在本年度。"
    )
    lines.append("")

    lines.append("## 极端值")
    lines.append("")
    lines.append(
        f"最长文本消息 {extremes['longest_message_length']:,} 字，"
        f"出现于 {extremes['longest_message_time'] or '无'}；"
        f"单日峰值 {(extremes.get('single_day_peak') or {}).get('count', 0):,} 条；"
        f"回复间隔中位数 {extremes['reply_interval_median_seconds']} 秒"
        f"（样本 {extremes['reply_samples']:,} 个）。"
    )
    lines.append("")

    lines.append("## 口径与限制")
    lines.append("")
    lines.append("- 群名与 wxid 不落盘：群用 sha1 前 8 位，发送者用 sha1 前 8 位，我发送的记作 self。")
    lines.append("- 系统消息 10000 与撤回 10002 单列统计，不计入聊天句数与时间分布。")
    lines.append("- monthly.csv 是月份与细分类型的交叉表，含系统与撤回消息，按月合计等于消息总条数。")
    lines.append("- 群内非好友成员本地没有备注与昵称，Top 发言人只能显示成员哈希。")
    lines.append("- 会话按 Msg_ 分表归属，同一会话可能分布在不同分库，明细按分库顺序输出。")
    return "\n".join(lines) + "\n"

"""年度报告纯计算层的单元测试。

用 python -m unittest discover -s tests -p "test_report_*.py" 运行。
"""
from __future__ import annotations

import unittest
from datetime import datetime

from src.report.metrics import (
    AnnualAggregator,
    chat_kind,
    describe_type,
    hash8,
    longest_streak,
    median_of,
    render_markdown,
)


class HelperTests(unittest.TestCase):
    def test_hash8_is_short_and_stable(self):
        value = hash8("wxid_example")
        self.assertEqual(len(value), 8)
        self.assertEqual(value, hash8("wxid_example"))
        self.assertNotEqual(value, hash8("wxid_other"))

    def test_chat_kind(self):
        self.assertEqual(chat_kind("123@chatroom"), "group")
        self.assertEqual(chat_kind("gh_abc123"), "official")
        self.assertEqual(chat_kind("wxid_alice"), "single")
        self.assertEqual(chat_kind("custom_alias_01"), "single")
        self.assertEqual(chat_kind(""), "unknown")

    def test_describe_type_plain(self):
        self.assertEqual(describe_type(1, "hi")[2], "text")
        self.assertEqual(describe_type(3)[2], "image")
        self.assertEqual(describe_type(10000)[2], "system")
        self.assertEqual(describe_type(10002)[2], "revoked")

    def test_describe_type_appmsg(self):
        link = (5 << 32) | 49
        main, _sub, label, sub_name = describe_type(link, "<msg><appmsg><type>5</type></appmsg></msg>")
        self.assertEqual(main, 49)
        self.assertEqual(label, "app:link")
        self.assertEqual(sub_name, "link")
        # 没有 XML 或没有 type 时退化为 app
        self.assertEqual(describe_type(link, "")[2], "app")

    def test_longest_streak(self):
        self.assertEqual(longest_streak([]), 0)
        self.assertEqual(longest_streak([1, 2, 3, 5, 6]), 3)
        self.assertEqual(longest_streak([10, 12, 14]), 1)

    def test_median(self):
        self.assertIsNone(median_of([]))
        self.assertEqual(median_of([3]), 3.0)
        self.assertEqual(median_of([1, 2, 3, 4]), 2.5)
        self.assertEqual(median_of([4, 1, 3]), 3.0)


def _ts(y, m, d, h=0, mi=0, s=0):
    return int(datetime(y, m, d, h, mi, s).timestamp())


class AggregatorTests(unittest.TestCase):
    def _agg(self):
        agg = AnnualAggregator(2025)
        # 单聊：对方一条、我一条
        agg.add("wxid_alice", "single", _ts(2025, 1, 1, 10), 1, "wxid_alice", "你好", False)
        agg.add("wxid_alice", "single", _ts(2025, 1, 1, 10, 0, 30), 1, "self", "在", True)
        # 系统消息与撤回：不计入聊天句数
        agg.add("wxid_alice", "single", _ts(2025, 1, 2, 9), 10000, "wxid_alice", "<sysmsg/>", False)
        agg.add("wxid_alice", "single", _ts(2025, 1, 3, 9), 10002, "wxid_alice", "", False)
        return agg

    def test_system_and_revoked_are_separate(self):
        agg = self._agg()
        self.assertEqual(agg.total, 4)
        self.assertEqual(agg.chat_messages, 2)
        self.assertEqual(agg.system, 1)
        self.assertEqual(agg.revoked, 1)

    def test_direction_and_reply_interval(self):
        agg = self._agg()
        self.assertEqual(agg.sent, 1)
        self.assertEqual(agg.received, 1)
        self.assertEqual(agg.reply_deltas, [30])

    def test_finish_structure(self):
        agg = self._agg()
        summary = agg.finish(lambda name: "联系人-" + hash8(name), {"new_friends": {"count": 1}})
        self.assertEqual(summary["year"], 2025)
        self.assertEqual(summary["overview"]["messages"], 4)
        self.assertEqual(len(summary["people"]), 1)
        self.assertEqual(summary["people"][0]["total"], 2)
        self.assertEqual(summary["types"][0]["type"], "text")
        self.assertEqual(sum(row["count"] for row in summary["monthly"]), 4)
        self.assertEqual(summary["relations"]["new_friends"]["count"], 1)
        self.assertEqual(summary["extremes"]["reply_interval_median_seconds"], 30.0)

    def test_render_markdown_sanitizes_names(self):
        agg = self._agg()
        summary = agg.finish(lambda name: "名|字*号", {})
        text = render_markdown(summary, {"data_cutoff": "2026-01-01 00:00:00", "generated_at": "x"})
        self.assertNotIn("名|字", text)
        self.assertNotIn("**", text)

    def test_render_markdown_has_no_forbidden_markup(self):
        agg = self._agg()
        summary = agg.finish(lambda name: "联系人-" + hash8(name), {})
        text = render_markdown(summary, {"data_cutoff": "2026-01-01 00:00:00", "generated_at": "x"})
        self.assertIn("2025 年微信聊天年度报告", text)
        self.assertNotIn("**", text)
        self.assertNotIn("~", text)
        self.assertNotIn("$", text)


if __name__ == "__main__":
    unittest.main()

"""年度报告端到端测试：造一个迷你 decrypted 目录，跑 run() 校验六份产物。

用 python -m unittest discover -s tests -p "test_report_*.py" 运行。
测试数据全部是虚构内容，不含真实聊天数据。
"""
from __future__ import annotations

import csv
import hashlib
import json
import sqlite3
import tempfile
import unittest
from datetime import datetime
from pathlib import Path

import zstandard as zstd

from src.db.locator import LocalSource
from src.report.annual import run
from src.report.metrics import hash8

OWNER = "wxid_owner"
ALICE = "wxid_alice"
BOB = "wxid_bob"
CAROL = "wxid_carol"
GROUP1 = "10000000001@chatroom"
GROUP2 = "10000000002@chatroom"
OFFICIAL = "gh_official_account"

FRIEND_2024 = "wxid_old_friend"


def _ts(y, m, d, h=0, mi=0, s=0):
    return int(datetime(y, m, d, h, mi, s).timestamp())


def _md5(value):
    return hashlib.md5(value.encode()).hexdigest()


def _create_msg_table(conn, username, rows):
    table = "Msg_" + _md5(username)
    conn.execute(
        f'CREATE TABLE "{table}" (local_id INTEGER, server_id INTEGER,'
        " local_type INTEGER, real_sender_id INTEGER, create_time INTEGER,"
        " message_content TEXT, WCDB_CT_message_content INTEGER)"
    )
    conn.executemany(f'INSERT INTO "{table}" VALUES (?,?,?,?,?,?,?)', rows)
    return table


def _build_source(root: Path) -> None:
    for sub in ("message", "contact", "session", "general"):
        (root / sub).mkdir(parents=True, exist_ok=True)

    conn = sqlite3.connect(root / "message" / "message_0.db")
    conn.execute("CREATE TABLE Name2Id (user_name TEXT, is_session INTEGER)")
    conn.executemany(
        "INSERT INTO Name2Id (user_name, is_session) VALUES (?,?)",
        [
            (OWNER, 1),
            (ALICE, 1),
            (BOB, 1),
            (CAROL, 1),
            (GROUP1, 1),
            (GROUP2, 1),
            (OFFICIAL, 0),
        ],
    )

    # 单聊：对方两条、我一条，2024 年那条不应计入
    _create_msg_table(
        conn,
        ALICE,
        [
            (1, 11, 1, 2, _ts(2025, 1, 5, 10, 0, 0), "hello", 0),
            (2, 12, 1, 1, _ts(2025, 1, 5, 10, 0, 30), "hi there", 0),
            (3, 13, 1, 2, _ts(2024, 12, 31, 23, 0, 0), "old year", 0),
            (
                4,
                14,
                34,
                2,
                _ts(2025, 3, 5, 21, 0, 0),
                zstd.ZstdCompressor().compress(b'<msg><voicemsg length="3000"/></msg>'),
                4,
            ),
        ],
    )

    # 单聊：图片与文本
    _create_msg_table(
        conn,
        CAROL,
        [
            (1, 21, 3, 4, _ts(2025, 2, 1, 8, 0, 0), "<msg><img md5=1/></msg>", 0),
            (2, 22, 1, 1, _ts(2025, 2, 1, 8, 5, 0), "ok", 0),
        ],
    )

    # 群：三人发言、系统、应用链接、撤回
    _create_msg_table(
        conn,
        GROUP1,
        [
            (1, 31, 1, 2, _ts(2025, 1, 10, 9, 0, 0), "group hello", 0),
            (2, 32, 1, 3, _ts(2025, 1, 10, 9, 1, 0), "group reply", 0),
            (3, 33, 1, 1, _ts(2025, 1, 10, 9, 2, 0), "mine", 0),
            (4, 34, 10000, 2, _ts(2025, 1, 10, 9, 3, 0), "<sysmsg/>", 0),
            (
                5,
                35,
                (5 << 32) | 49,
                1,
                _ts(2025, 1, 11, 22, 30, 0),
                "<msg><appmsg><type>5</type><title>t</title></appmsg></msg>",
                0,
            ),
            (6, 36, 10002, 2, _ts(2025, 1, 11, 23, 0, 0), "", 0),
        ],
    )

    # 群：两人发言，含一个文件应用消息
    _create_msg_table(
        conn,
        GROUP2,
        [
            (1, 41, 1, 3, _ts(2025, 1, 12, 12, 0, 0), "gm", 0),
            (2, 42, 1, 1, _ts(2025, 1, 12, 12, 1, 0), "yo", 0),
            (
                3,
                43,
                (6 << 32) | 49,
                1,
                _ts(2025, 1, 20, 12, 2, 0),
                "<msg><appmsg><type>6</type><title>report.pdf</title></appmsg></msg>",
                0,
            ),
        ],
    )

    # 公众号
    _create_msg_table(
        conn,
        OFFICIAL,
        [(1, 51, 1, 7, _ts(2025, 1, 13, 7, 0, 0), "news", 0)],
    )
    conn.commit()
    conn.close()

    contact = sqlite3.connect(root / "contact" / "contact.db")
    contact.execute(
        "CREATE TABLE contact (id INTEGER PRIMARY KEY, username TEXT, local_type INTEGER,"
        " alias TEXT, remark TEXT, nick_name TEXT)"
    )
    contact.executemany(
        "INSERT INTO contact (username, local_type, remark, nick_name) VALUES (?,?,?,?)",
        [
            (ALICE, 1, "Alice", ""),
            (BOB, 3, "", "BobNick"),
            (CAROL, 1, "", "卡罗"),
            (FRIEND_2024, 1, "老朋友", ""),
        ],
    )
    contact.commit()
    contact.close()

    session = sqlite3.connect(root / "session" / "session.db")
    session.execute("CREATE TABLE SessionTable (username TEXT, sort_timestamp INTEGER)")
    session.execute("INSERT INTO SessionTable VALUES (?,?)", (ALICE, _ts(2025, 1, 5)))
    session.commit()
    session.close()

    general = sqlite3.connect(root / "general" / "general.db")
    general.execute(
        "CREATE TABLE FMessageTable (user_name_ TEXT, type_ INTEGER, timestamp_ INTEGER,"
        " is_sender_ INTEGER)"
    )
    general.executemany(
        "INSERT INTO FMessageTable VALUES (?,?,?,?)",
        [
            ("wxid_new1", 37, _ts(2025, 2, 1), 0),
            ("wxid_new2", 37, _ts(2025, 3, 1), 0),
            ("wxid_old1", 37, _ts(2024, 12, 1), 0),
        ],
    )
    general.commit()
    general.close()


class AnnualReportTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        cls.root = Path(cls.tmp.name) / "decrypted"
        _build_source(cls.root)
        cls.out = Path(cls.tmp.name) / "reports" / "2025"
        cls.result = run(LocalSource(cls.root), 2025, str(cls.out))

    @classmethod
    def tearDownClass(cls):
        cls.tmp.cleanup()

    def _read(self, name):
        return (self.out / name).read_text(encoding="utf-8-sig")

    def _report(self):
        return json.loads((self.out / "report.json").read_text(encoding="utf-8"))

    def test_all_six_files_exist(self):
        for name in (
            "report.json",
            "people.csv",
            "groups.csv",
            "monthly.csv",
            "messages.csv",
            "report.md",
        ):
            self.assertTrue((self.out / name).exists(), name)
        self.assertEqual(len(self.result["files"]), 6)
        self.assertTrue(self.result["ok"])

    def test_overview_numbers(self):
        ov = self._report()["overview"]
        self.assertEqual(ov["messages"], 15)
        self.assertEqual(ov["chat_messages"], 13)
        self.assertEqual(ov["system"], 1)
        self.assertEqual(ov["revoked"], 1)
        self.assertEqual(ov["sent"], 6)
        self.assertEqual(ov["received"], 7)
        self.assertEqual(ov["single_chats"], 2)
        self.assertEqual(ov["group_chats"], 2)
        self.assertEqual(ov["official_chats"], 1)
        self.assertEqual(ov["single_messages"], 5)
        self.assertEqual(ov["group_messages"], 7)
        self.assertEqual(ov["official_messages"], 1)

    def test_self_detected(self):
        self.assertTrue(self.result["stats"]["self_detected"])
        self.assertTrue(self._report()["meta"]["self_detected"])

    def test_people_csv(self):
        rows = list(csv.DictReader(self._read("people.csv").splitlines()))
        self.assertEqual(len(rows), 2)
        by_name = {row["name"]: row for row in rows}
        self.assertIn("Alice", by_name)
        self.assertEqual(int(by_name["Alice"]["total"]), 3)
        self.assertEqual(int(by_name["Alice"]["sent"]), 1)
        self.assertAlmostEqual(float(by_name["Alice"]["share"]), round(3 / 13, 4), places=4)
        self.assertAlmostEqual(float(by_name["Alice"]["my_ratio"]), round(1 / 3, 4), places=4)
        self.assertEqual(by_name["Alice"]["chat_hash"], hash8(ALICE))
        self.assertEqual(int(by_name["卡罗"]["total"]), 2)

    def test_groups_csv(self):
        rows = list(csv.DictReader(self._read("groups.csv").splitlines()))
        self.assertEqual(len(rows), 2)
        first = rows[0]
        self.assertEqual(int(first["total"]), 4)
        self.assertEqual(int(first["mine"]), 2)
        self.assertEqual(first["top1_name"], "我")
        self.assertEqual(int(first["top1_count"]), 2)
        self.assertEqual(first["label"], "群-" + hash8(GROUP1))

    def test_monthly_cross_table(self):
        rows = list(csv.DictReader(self._read("monthly.csv").splitlines()))
        self.assertEqual(sum(int(row["count"]) for row in rows), 15)
        self.assertIn(
            {"month": "2025-01", "type": "app:link", "count": 1},
            [{"month": r["month"], "type": r["type"], "count": int(r["count"])} for r in rows],
        )

    def test_messages_csv(self):
        rows = list(csv.DictReader(self._read("messages.csv").splitlines()))
        self.assertEqual(len(rows), 15)
        self.assertEqual(
            list(rows[0].keys()),
            [
                "time",
                "direction",
                "chat_kind",
                "chat_hash",
                "sender_hash",
                "type",
                "subtype",
                "text_length",
            ],
        )
        kinds = {row["chat_kind"] for row in rows}
        self.assertEqual(kinds, {"single", "group", "official"})
        self.assertIn("self", {row["sender_hash"] for row in rows})
        app_rows = [row for row in rows if row["type"] == "app"]
        self.assertEqual(sorted(row["subtype"] for row in app_rows), ["file", "link"])

    def test_report_md_sections(self):
        text = self._read("report.md")
        for section in ("总览", "按人 Top10", "按群 Top10", "类型分布", "时间分布", "极端值"):
            self.assertIn(section, text)
        self.assertNotIn("**", text)
        self.assertNotIn("~", text)
        self.assertNotIn("$", text)

    def test_no_sensitive_values_leak(self):
        """正文、wxid、群名不得出现在任何产物里。"""
        secrets = ["wxid_", "@chatroom", "group hello", "group reply", "hello", "hi there"]
        for name in (
            "report.json",
            "people.csv",
            "groups.csv",
            "monthly.csv",
            "messages.csv",
            "report.md",
        ):
            text = self._read(name)
            for secret in secrets:
                self.assertNotIn(secret, text, f"{name} 泄露 {secret}")

    def test_relations(self):
        relations = self._report()["relations"]
        self.assertEqual(relations["new_friends"]["count"], 2)
        self.assertEqual(relations["new_chats"]["count"], 4)
        self.assertEqual(relations["new_chats"]["groups"], 2)

    def test_extremes_and_types(self):
        report = self._report()
        self.assertEqual(report["types_extra"]["voice_seconds"], 3.0)
        self.assertEqual(report["types_extra"]["file_ext_top"], [["pdf", 1]])
        self.assertGreater(report["extremes"]["longest_message_length"], 0)
        self.assertIsNotNone(report["extremes"]["reply_interval_median_seconds"])

    def test_time_sections(self):
        time_ = self._report()["time"]
        self.assertEqual(len(time_["by_hour"]), 24)
        self.assertEqual(len(time_["by_weekday"]), 7)
        self.assertEqual(time_["busiest_day"]["date"], "2025-01-10")
        self.assertEqual(time_["busiest_day"]["count"], 3)
        self.assertGreaterEqual(time_["longest_streak_days"], 1)


class EmptyYearTests(unittest.TestCase):
    def test_year_without_messages_still_writes_files(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "decrypted"
            _build_source(root)
            out = Path(tmp) / "reports" / "1999"
            result = run(LocalSource(root), 1999, str(out))
            self.assertTrue(result["ok"])
            self.assertEqual(result["stats"]["messages"], 0)
            self.assertTrue((out / "report.json").exists())
            lines = (out / "messages.csv").read_text(encoding="utf-8-sig").splitlines()
            self.assertEqual(len(lines), 1)
            with open(out / "people.csv", encoding="utf-8-sig") as handle:
                self.assertEqual(len(list(csv.reader(handle))), 1)


if __name__ == "__main__":
    unittest.main()

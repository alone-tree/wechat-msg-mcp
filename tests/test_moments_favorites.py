"""朋友圈、收藏与联系人地区的单元测试。

用临时目录造最小库，验证解析、筛选、分页与评论挂载。
"""
from __future__ import annotations

import sqlite3
import tempfile
import unittest
from pathlib import Path

from src.db.locator import LocalSource
from src.entities import _extra_fields, load_contacts
from src.favorites import _parse as parse_favorite
from src.favorites import list_favorites
from src.moments import _parse as parse_moment
from src.moments import list_moments


def _make_db(path: Path, schema: str, rows: list[tuple]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(path))
    conn.executescript(schema)
    if rows:
        conn.executemany(
            "INSERT INTO t VALUES (" + ",".join("?" * len(rows[0])) + ")", rows
        )
    conn.commit()
    conn.close()


MOMENT_XML = (
    "<SnsDataItem><TimelineObject><username>wxid_a</username>"
    "<createTime>1700000000</createTime><contentDesc>今天签了合同</contentDesc>"
    "<mediaList><media><type>2</type><thumb>http://img/x.jpg</thumb></media></mediaList>"
    "<like_user_list><user_comment><nickname>小李</nickname></user_comment></like_user_list>"
    "</TimelineObject></SnsDataItem>"
)

FAV_XML = (
    '<favitem type="5"><title>一份文档</title><desc>描述</desc>'
    "<source><fromusr>wxid_b</fromusr><link>https://example.com/a</link></source>"
    "</favitem>"
)


class MomentParseTests(unittest.TestCase):
    def test_parse_extracts_fields(self):
        parsed = parse_moment(MOMENT_XML)
        self.assertEqual(parsed["time"], 1700000000)
        self.assertIn("合同", parsed["text"])
        self.assertEqual(parsed["media"][0]["url"], "http://img/x.jpg")
        self.assertEqual(parsed["likes"], ["小李"])

    def test_parse_handles_broken_xml(self):
        parsed = parse_moment("<broken><createTime>1700000000</createTime>")
        self.assertEqual(parsed["time"], 1700000000)


class FavoriteParseTests(unittest.TestCase):
    def test_parse_link(self):
        parsed = parse_favorite(FAV_XML)
        self.assertEqual(parsed["title"], "一份文档")
        self.assertEqual(parsed["url"], "https://example.com/a")
        self.assertEqual(parsed["source_user"], "wxid_b")


class ExtraFieldTests(unittest.TestCase):
    @staticmethod
    def _varint(n: int) -> bytes:
        out = bytearray()
        while True:
            b = n & 0x7F
            n >>= 7
            out.append(b | (0x80 if n else 0))
            if not n:
                return bytes(out)

    def _field_text(self, fn: int, text: str) -> bytes:
        raw = text.encode()
        return self._varint((fn << 3) | 2) + self._varint(len(raw)) + raw

    def test_region_and_add_time(self):
        blob = (
            self._field_text(5, "CN")
            + self._field_text(6, "Zhejiang")
            + self._field_text(7, "Hangzhou")
            + self._varint((41 << 3) | 0)
            + self._varint(1710000000)
        )
        out = _extra_fields(blob)
        self.assertEqual(out["region"], "Zhejiang Hangzhou")
        self.assertTrue(out["add_time"].startswith("2024-03"))


class QueryTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        conn = sqlite3.connect(str(root / "contact" / "contact.db")) if (root / "contact").mkdir(parents=True) is None else None
        conn.executescript(
            "CREATE TABLE contact(id INTEGER PRIMARY KEY, username TEXT, local_type INT,"
            " remark TEXT, nick_name TEXT, alias TEXT, description TEXT);"
            "INSERT INTO contact VALUES (1,'wxid_a',1,'好友甲','甲','jia','');"
            "INSERT INTO contact VALUES (2,'wxid_b',1,'好友乙','乙','yi','');"
        )
        conn.commit()
        conn.close()
        sns = root / "sns" / "sns.db"
        sns.parent.mkdir(parents=True, exist_ok=True)
        c2 = sqlite3.connect(str(sns))
        c2.executescript(
            "CREATE TABLE SnsTimeLine(tid INTEGER PRIMARY KEY, user_name TEXT, content TEXT);"
            "CREATE TABLE SnsMessage_tmp3(local_id INTEGER, create_time INT, type INT, feed_id TEXT,"
            " from_username TEXT, from_nickname TEXT, to_username TEXT, to_nickname TEXT, content TEXT);"
        )
        c2.execute("INSERT INTO SnsTimeLine VALUES (?,?,?)", (1, "wxid_a", MOMENT_XML))
        c2.execute(
            "INSERT INTO SnsMessage_tmp3 VALUES (1,1700000100,2,'1','wxid_b','乙','','','好看')"
        )
        c2.commit()
        c2.close()
        fav = root / "favorite" / "favorite.db"
        fav.parent.mkdir(parents=True, exist_ok=True)
        c3 = sqlite3.connect(str(fav))
        c3.executescript(
            "CREATE TABLE fav_db_item(local_id INTEGER PRIMARY KEY, type INT, content TEXT,"
            " fromusr TEXT, realchatname TEXT, update_time INT);"
            "CREATE TABLE fav_tag_db_item(local_id INTEGER PRIMARY KEY, name TEXT);"
            "CREATE TABLE fav_bind_tag_db_item(tag_local_id INT, fav_local_id INT);"
        )
        c3.execute("INSERT INTO fav_db_item VALUES (1,5,?, 'wxid_b','',1700000200)", (FAV_XML,))
        c3.execute("INSERT INTO fav_tag_db_item VALUES (9,'学习')")
        c3.execute("INSERT INTO fav_bind_tag_db_item VALUES (9,1)")
        c3.commit()
        c3.close()
        self.src = LocalSource(root)

    def tearDown(self):
        self.tmp.cleanup()

    def test_list_moments_with_comments(self):
        out = list_moments(self.src)
        self.assertEqual(out["total"], 1)
        self.assertEqual(out["moments"][0]["comment_count"], 1)
        self.assertEqual(out["moments"][0]["likes"], ["小李"])

    def test_moments_author_filter(self):
        self.assertEqual(list_moments(self.src, author="好友乙")["total"], 0)
        self.assertEqual(list_moments(self.src, author="好友甲")["total"], 1)

    def test_favorites_filters(self):
        self.assertEqual(list_favorites(self.src)["total"], 1)
        self.assertEqual(list_favorites(self.src, type="link")["total"], 1)
        self.assertEqual(list_favorites(self.src, type="image")["total"], 0)
        self.assertEqual(list_favorites(self.src, tag="学习")["total"], 1)
        self.assertEqual(list_favorites(self.src, keyword="文档")["total"], 1)

    def test_region_from_contact(self):
        contacts = load_contacts(self.src)
        self.assertEqual({c["username"] for c in contacts}, {"wxid_a", "wxid_b"})


if __name__ == "__main__":
    unittest.main()

"""媒体定位与解密占用回归测试。

覆盖 2026-11-14 的线上问题：图片 read 不到时只返回 kind 标记，原因看不出；
根子是 image.py 长期持有 hardlink.db 连接，Windows 下让解密换不掉副本。
"""
import os
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src import decrypt  # noqa: E402
from src.db import image  # noqa: E402

SQLITE_MAGIC = b"SQLite format 3\x00"


class HardlinkLocateTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.db = self.root / "hardlink.db"
        conn = sqlite3.connect(self.db)
        conn.execute(
            "CREATE TABLE image_hardlink_info_v4 (md5 TEXT, file_name TEXT, dir1 INT, dir2 INT)"
        )
        conn.execute("CREATE TABLE dir2id (username TEXT)")
        conn.execute("INSERT INTO dir2id (rowid, username) VALUES (1, 'chatdir')")
        conn.execute("INSERT INTO dir2id (rowid, username) VALUES (2, '2026-11')")
        conn.execute(
            "INSERT INTO image_hardlink_info_v4 VALUES (?, ?, 1, 2)",
            ("a" * 32, "b" * 32 + ".dat"),
        )
        conn.commit()
        conn.close()

        self.attach = self.root / "attach" / "chatdir" / "2026-11" / "Img"
        self.attach.mkdir(parents=True)
        self.dat = self.attach / ("b" * 32 + ".dat")
        self.dat.write_bytes(b"fake")

        self._old_db = image.HARDLINK_DB
        self._old_sig = image._IMG_MD5_RE
        self._old_dirs = image._account_dirs
        image.HARDLINK_DB = str(self.db)
        image._dir_cache = None
        # 只认 <md5>…</md5>，避免属性写法把测试意图搅混
        import re

        image._IMG_MD5_RE = re.compile(r"<md5>([0-9a-f]{32})</md5>")
        image._account_dirs = lambda: [self.root / "attach"]

    def tearDown(self):
        image.HARDLINK_DB = self._old_db
        image._IMG_MD5_RE = self._old_sig
        image._account_dirs = self._old_dirs
        image._dir_cache = None
        self.tmp.cleanup()

    def test_hit(self):
        paths, reason = image.image_locate(f"<md5>{'a' * 32}</md5>")
        self.assertEqual(reason, "")
        self.assertEqual([p.name for p in paths], ["b" * 32 + ".dat"])

    def test_row_missing(self):
        paths, reason = image.image_locate(f"<md5>{'c' * 32}</md5>")
        self.assertEqual(paths, [])
        self.assertEqual(reason, "hardlink_row_missing")

    def test_no_md5(self):
        self.assertEqual(image.image_locate("hello"), ([], "no_md5"))

    def test_file_not_on_disk(self):
        self.dat.unlink()
        _, reason = image.image_locate(f"<md5>{'a' * 32}</md5>")
        self.assertEqual(reason, "file_not_on_disk")

    @unittest.skipUnless(sys.platform == "win32", "Windows 才限制替换被打开的文件")
    def test_no_lingering_handle(self):
        """查完之后副本必须能被替换，否则解密刷新永远失败。"""
        image.image_locate(f"<md5>{'a' * 32}</md5>")
        tmp = self.root / "new.db"
        tmp.write_bytes(self.db.read_bytes())
        os.replace(tmp, self.db)  # 不抛异常即为通过


class DecryptReasonTest(unittest.TestCase):
    def test_locked_target(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            src = root / "src.db"
            conn = sqlite3.connect(src)
            conn.execute("CREATE TABLE t (x INT)")
            conn.commit()
            conn.close()
            self.assertTrue(src.read_bytes().startswith(SQLITE_MAGIC))

            dst = root / "dst.db"
            dst.write_bytes(b"")
            ok, reason = decrypt.decrypt_to(src, "00" * 32, dst)
            self.assertTrue(ok, reason)

            # 只在 Windows 上验证被占用时的原因
            if sys.platform == "win32":
                conn = sqlite3.connect(f"file:{dst.as_posix()}?mode=ro", uri=True)
                try:
                    conn.execute("SELECT count(*) FROM sqlite_master").fetchone()
                    ok2, reason2 = decrypt.decrypt_to(src, "00" * 32, dst)
                    self.assertFalse(ok2)
                    self.assertEqual(reason2, "locked")
                finally:
                    conn.close()

    def test_too_small(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            src = root / "tiny.db"
            src.write_bytes(b"x" * 10)
            ok, reason = decrypt.decrypt_to(src, "00" * 32, root / "out.db")
            self.assertFalse(ok)
            self.assertEqual(reason, "too_small")


if __name__ == "__main__":
    unittest.main()

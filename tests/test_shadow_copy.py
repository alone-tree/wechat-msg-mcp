"""解密刷新与「副本被占用」回归测试。

2026-11-14 线上问题：长驻进程持有 decrypted/hardlink/hardlink.db 句柄，
Windows 下 os.replace 失败，hardlink 副本永久停旧，收到的图片读不到。
现在的行为：正本换不掉时刷新写 <名>.new，读取方按 mtime 取最新的一个。
"""
import os
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src import decrypt  # noqa: E402
from src.db.locator import latest_copy  # noqa: E402


def _make_db(path: Path, rows: int) -> None:
    conn = sqlite3.connect(path)
    conn.execute("PRAGMA journal_mode=DELETE")
    conn.execute("CREATE TABLE IF NOT EXISTS t (x INT)")
    conn.execute("DELETE FROM t")
    conn.executemany("INSERT INTO t VALUES (?)", [(i,) for i in range(rows)])
    conn.commit()
    conn.close()


def _rows(path: Path) -> int:
    conn = sqlite3.connect(f"file:{path.as_posix()}?mode=ro&immutable=1", uri=True)
    try:
        return conn.execute("SELECT count(*) FROM t").fetchone()[0]
    finally:
        conn.close()


@unittest.skipUnless(sys.platform == "win32", "只有 Windows 会因句柄占用而无法替换")
class ShadowCopyTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        base = Path(self.tmp.name)
        self.src_root = base / "src"
        self.dst_root = base / "dst"
        (self.src_root / "hardlink").mkdir(parents=True)
        self.src = self.src_root / "hardlink" / "hardlink.db"
        self.dst = self.dst_root / "hardlink" / "hardlink.db"
        self.keys = base / "keys.json"
        self.keys.write_text('{"k": "x\'%s\'"}' % ("00" * 32), encoding="utf-8")
        _make_db(self.src, 2)

    def tearDown(self):
        self.tmp.cleanup()

    def _refresh(self):
        return decrypt.refresh(self.src_root, self.dst_root, self.keys, only_changed=True)

    def test_first_refresh_writes_primary(self):
        r = self._refresh()
        self.assertEqual(r["changed"], ["hardlink/hardlink.db"])
        self.assertTrue(self.dst.exists())
        self.assertEqual(_rows(self.dst), 2)

    def test_locked_primary_falls_back_to_new(self):
        self._refresh()
        _make_db(self.src, 3)
        os.utime(self.src, (os.path.getmtime(self.src) + 5, os.path.getmtime(self.src) + 5))

        holder = sqlite3.connect(f"file:{self.dst.as_posix()}?mode=ro&immutable=1", uri=True)
        try:
            r = self._refresh()
        finally:
            holder.close()

        self.assertEqual(r["failed"], [])
        self.assertEqual(r["shadow"], ["hardlink/hardlink.db"])
        alt = self.dst.with_name(self.dst.name + ".new")
        self.assertTrue(alt.exists())
        self.assertEqual(_rows(alt), 3)
        # 读取方取最新：应是旁路副本
        self.assertEqual(latest_copy(self.dst), alt)

    def test_shadow_cleaned_once_primary_writable(self):
        self._refresh()
        _make_db(self.src, 3)
        os.utime(self.src, (os.path.getmtime(self.src) + 5, os.path.getmtime(self.src) + 5))
        holder = sqlite3.connect(f"file:{self.dst.as_posix()}?mode=ro&immutable=1", uri=True)
        try:
            self._refresh()
        finally:
            holder.close()
        alt = self.dst.with_name(self.dst.name + ".new")
        self.assertTrue(alt.exists())

        _make_db(self.src, 4)
        os.utime(self.src, (os.path.getmtime(self.src) + 10, os.path.getmtime(self.src) + 10))
        r = self._refresh()
        self.assertEqual(r["shadow"], [])
        self.assertEqual(_rows(self.dst), 4)
        self.assertFalse(alt.exists())


if __name__ == "__main__":
    unittest.main()

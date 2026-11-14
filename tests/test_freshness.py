"""刷新链的单元测试：TTL 解析、状态落盘、互斥锁、无数据源时的降级。

用 python -m unittest discover -s tests -v 运行。
"""
from __future__ import annotations

import os
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

from src import freshness
from src.db.locator import LocalSource


class TtlTests(unittest.TestCase):
    def test_default_is_sixty(self):
        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop("WECHAT_REFRESH_TTL", None)
            self.assertEqual(freshness.ttl_seconds(), 60)

    def test_zero_means_always(self):
        with mock.patch.dict(os.environ, {"WECHAT_REFRESH_TTL": "0"}):
            self.assertEqual(freshness.ttl_seconds(), 0)

    def test_negative_disables(self):
        with mock.patch.dict(os.environ, {"WECHAT_REFRESH_TTL": "-1"}):
            self.assertEqual(freshness.ttl_seconds(), -1)

    def test_invalid_falls_back(self):
        with mock.patch.dict(os.environ, {"WECHAT_REFRESH_TTL": "abc"}):
            self.assertEqual(freshness.ttl_seconds(), 60)


class StateAndLockTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.src = LocalSource(self.tmp.name)

    def tearDown(self):
        freshness._release(self.src)
        self.tmp.cleanup()

    def test_state_roundtrip(self):
        freshness._write_state(self.src, {"changed": ["a.db"], "seconds": 1.2})
        state = freshness._read_state(self.src)
        self.assertEqual(state["changed"], ["a.db"])

    def test_missing_state_is_empty(self):
        self.assertEqual(freshness._read_state(self.src), {})

    def test_lock_is_exclusive(self):
        self.assertTrue(freshness._acquire(self.src))
        self.assertFalse(freshness._acquire(self.src))
        freshness._release(self.src)
        self.assertTrue(freshness._acquire(self.src))

    def test_stale_lock_is_reclaimed(self):
        self.assertTrue(freshness._acquire(self.src))
        lock = freshness.lock_path(self.src)
        old = time.time() - freshness.LOCK_STALE - 10
        os.utime(lock, (old, old))
        self.assertTrue(freshness._acquire(self.src))

    def test_peek_ttl_left_none_when_disabled(self):
        with mock.patch.dict(os.environ, {"WECHAT_REFRESH_TTL": "-1"}):
            self.assertIsNone(freshness.peek_ttl_left(self.src))


class EnsureTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.src = LocalSource(self.tmp.name)

    def tearDown(self):
        freshness._release(self.src)
        self.tmp.cleanup()

    def test_disabled_skips(self):
        with mock.patch.dict(os.environ, {"WECHAT_REFRESH_TTL": "-1"}):
            result = freshness.ensure(self.src)
        self.assertFalse(result["refreshed"])
        self.assertEqual(result["skipped"], "disabled")

    def test_ttl_skips_without_touching_disk(self):
        freshness._write_state(self.src, {"last_attempt": time.time(), "changed": ["x.db"]})
        with mock.patch.dict(os.environ, {"WECHAT_REFRESH_TTL": "60"}):
            result = freshness.ensure(self.src)
        self.assertFalse(result["refreshed"])
        self.assertEqual(result["skipped"], "ttl")
        self.assertGreater(result["ttl_left"], 0)
        self.assertEqual(result["changed_dbs"], ["x.db"])

    def test_no_source_degrades(self):
        with mock.patch.dict(os.environ, {"WECHAT_REFRESH_TTL": "0", "WECHAT_FILES_DIR": str(Path(self.tmp.name) / "nope")}):
            result = freshness.ensure(self.src)
        self.assertFalse(result["refreshed"])
        self.assertEqual(result["skipped"], "no_source")

    def test_locked_degrades(self):
        self.assertTrue(freshness._acquire(self.src))
        with mock.patch.dict(os.environ, {"WECHAT_REFRESH_TTL": "0"}):
            result = freshness.ensure(self.src)
        self.assertFalse(result["refreshed"])
        self.assertEqual(result["skipped"], "locked")


if __name__ == "__main__":
    unittest.main()

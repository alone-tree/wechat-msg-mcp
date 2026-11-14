"""message_resource 定位与缩略图兜底回归测试。

2026-11-14 验收发现：图片只查 hardlink 登记表，登记表没记的新文件全都读不到，
而磁盘上其实有缩略图甚至原图。现在先按 message_resource 的落盘基名找，
找不到再按消息自带的长度与宽高认文件。
"""
import sys
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.db import image, resource  # noqa: E402


class ResourcePbTest(unittest.TestCase):
    def test_parse_name(self):
        blob = b"\x12\x22\x0a\x20" + b"04fa8bdfc2ea714e61abc1794b12ed4b"
        self.assertEqual(resource._pb_str(blob), "04fa8bdfc2ea714e61abc1794b12ed4b")

    def test_parse_bad(self):
        self.assertEqual(resource._pb_str(b""), "")
        self.assertEqual(resource._pb_str(b"\x08\x01"), "")


class ImageLocateOrderTest(unittest.TestCase):
    def test_resource_hit_without_hardlink(self):
        fake = [Path("C:/x/base_t.dat")]
        with mock.patch.object(resource, "media_paths", return_value=fake), mock.patch.object(
            image, "_hardlink_conn", return_value=None
        ):
            paths, reason = image.image_locate(
                '<img md5="' + "a" * 32 + '">', "123@chatroom", 42, 1700000000
            )
        self.assertEqual(paths, fake)
        self.assertEqual(reason, "")

    def test_hardlink_original_beats_resource_thumb(self):
        """resource 只给出缩略图时，还要拿 hardlink 里的原图，原图排前面。"""
        thumb = Path("C:/x/base_t.dat")
        dat = Path("C:/x/base.dat")
        with mock.patch.object(resource, "media_paths", return_value=[thumb]), mock.patch.object(
            image, "_hardlink_conn"
        ) as conn_mock, mock.patch.object(
            image, "_dir_map", return_value={1: "chatdir", 2: "2026-11"}
        ), mock.patch.object(
            image, "_account_dirs", return_value=[Path("C:/x")]
        ), mock.patch.object(
            Path, "exists", return_value=True
        ):
            conn = conn_mock.return_value
            conn.execute.return_value.fetchone.return_value = ("base_h.dat", 1, 2)
            paths, reason = image.image_locate(
                '<img md5="' + "a" * 32 + '">', "123@chatroom", 42, 1700000000
            )
        self.assertEqual(reason, "")
        self.assertFalse(paths[0].stem.endswith("_t"), paths)

    def test_no_md5(self):
        self.assertEqual(image.image_locate("hello"), ([], "no_md5"))

    def test_meta_parse(self):
        xml = '<img length="900" cdnthumblength="120" cdnthumbwidth="21" cdnthumbheight="34">'
        self.assertEqual(image._meta_of(xml), (900, 120, (21, 34)))


class TallImageTest(unittest.TestCase):
    def test_long_screenshot_keeps_width(self):
        """长截图不能按长边压成几十像素宽，否则一个字也看不清。"""
        from io import BytesIO

        from PIL import Image as PILImage

        buf = BytesIO()
        PILImage.new("RGB", (800, 8000), (255, 255, 255)).save(buf, format="PNG")
        out = image.normalize_to_jpeg(buf.getvalue(), max_side=1600)
        self.assertIsNotNone(out)
        with PILImage.open(BytesIO(out)) as im:
            w, h = im.size
        self.assertGreaterEqual(w, 600)
        self.assertLessEqual(h, 12000)


if __name__ == "__main__":
    unittest.main()

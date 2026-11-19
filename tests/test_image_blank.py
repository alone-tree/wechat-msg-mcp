"""图片空白占位与降级顺序回归。

微信对 HEVC 长图会把普通 .dat 存成纯白占位：合法 JPEG、尺寸也对，只有内容全白。
旧实现只看能不能解码，于是把占位图当成功结果交出去。这里锁死三件事：
纯色图判定、纯色时继续降级（优先高清 _h）、全部纯色时仍返回图不算失败。
"""
import io
import sys
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from PIL import Image  # noqa: E402

from src.db import image  # noqa: E402


def _flat_jpeg(color):
    buf = io.BytesIO()
    Image.new("RGB", (40, 20), color).save(buf, "JPEG", quality=90)
    return buf.getvalue()


def _content_jpeg():
    img = Image.new("RGB", (40, 20), (250, 250, 250))
    px = img.load()
    for x in range(0, 40, 4):
        for y in range(0, 20, 4):
            px[x, y] = (20, 80, 200)
    buf = io.BytesIO()
    img.save(buf, "JPEG", quality=90)
    return buf.getvalue()


class BlankDetectTests(unittest.TestCase):
    def test_white_is_blank(self):
        self.assertTrue(image.is_blank_image(_flat_jpeg((255, 255, 255))))

    def test_real_content_is_not_blank(self):
        self.assertFalse(image.is_blank_image(_content_jpeg()))

    def test_garbage_is_not_blank(self):
        self.assertFalse(image.is_blank_image(b"not an image"))


class FallbackTests(unittest.TestCase):
    def setUp(self):
        self.high = Path("img") / "aa_h.dat"
        self.base = Path("img") / "aa.dat"
        self.thumb = Path("img") / "aa_t.dat"
        self.white = _flat_jpeg((255, 255, 255))
        self.real = _content_jpeg()

    def _extract(self, decoded):
        def fake_decode(path, *_args, **_kwargs):
            return decoded.get(path.name)

        with mock.patch.object(
            image, "load_keys", return_value={"image_aes_key": "k" * 16, "image_xor_key": 1}
        ), mock.patch.object(
            image, "image_locate", return_value=([self.high, self.base, self.thumb], "")
        ), mock.patch.object(
            image, "_decode_dat", side_effect=fake_decode
        ), mock.patch.object(
            image, "find_render_cache_image", return_value=None
        ), mock.patch.object(
            image, "_meta_of", return_value=(0, 0, None)
        ):
            return image.extract_image_meta("<img md5='a'/>", "chat", 0, 0)

    def test_blank_base_uses_high(self):
        data, source, reason = self._extract(
            {self.high.name: self.real, self.base.name: self.white, self.thumb.name: self.real}
        )
        self.assertEqual(data, self.real)
        self.assertEqual(source, "dat")
        self.assertEqual(reason, "")

    def test_blank_all_but_thumb_marks_thumb(self):
        data, source, reason = self._extract(
            {self.high.name: None, self.base.name: self.white, self.thumb.name: self.real}
        )
        self.assertEqual(data, self.real)
        self.assertEqual(source, "thumb")
        self.assertEqual(reason, "")

    def test_all_blank_still_returns_image(self):
        data, source, reason = self._extract(
            {self.high.name: self.white, self.base.name: self.white, self.thumb.name: self.white}
        )
        self.assertEqual(data, self.white)
        self.assertEqual(source, "dat")
        self.assertEqual(reason, "")

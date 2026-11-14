"""长截图切段回归测试。

客户端会把单张图压到长边 1600 再看，长截图整张给过去等于缩略图；
所以按 1600 高切段，段间留 80 像素重叠，避免正好切在一行字上。
"""
import io
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from PIL import Image  # noqa: E402

from src.mcp_tools import _tiles_for  # noqa: E402


def _long_png(w: int, h: int) -> bytes:
    im = Image.new("RGB", (w, h), (255, 255, 255))
    px = im.load()
    for y in range(0, h, 100):
        for x in range(w):
            px[x, y] = (0, 0, 0)
    buf = io.BytesIO()
    im.save(buf, format="PNG")
    return buf.getvalue()


def _sizes(tiles) -> list:
    out = []
    for t in tiles:
        with Image.open(io.BytesIO(t)) as im:
            out.append(im.size)
    return out


class TilingTest(unittest.TestCase):
    def test_long_screenshot_has_overlap(self):
        tiles = _tiles_for(_long_png(800, 8000), tile_height=1600, overlap=80)
        sizes = _sizes(tiles)
        self.assertGreater(len(tiles), 1)
        heights = [h for _, h in sizes]
        self.assertEqual(sum(heights), 8000 + (len(tiles) - 1) * 80)
        self.assertTrue(all(h <= 1600 for h in heights))
        self.assertTrue(all(w == 800 for w, _ in sizes))

    def test_normal_image_not_tiled(self):
        self.assertEqual(_tiles_for(_long_png(800, 1200)), [])

    def test_wide_image_not_tiled(self):
        self.assertEqual(_tiles_for(_long_png(1600, 1200)), [])

    def test_tile_cap(self):
        tiles = _tiles_for(_long_png(400, 20000), tile_height=1600, max_tiles=6)
        self.assertLessEqual(len(tiles), 6)
        self.assertGreater(len(tiles), 1)


if __name__ == "__main__":
    unittest.main()

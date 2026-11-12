"""
图片提取与内联。

链路：消息 content 里的 <md5> → hardlink 登记库（image_hardlink_info_v4）
→ dir2id 还原目录 → msg/attach/<聊天目录hash>/<年月>/Img/<文件名>.dat
→ 按 V2 格式解密（账号级 AES 钥匙 + 尾部 XOR 钥匙）→ 图片字节。

数量限制：单次查询最多内联 N 张（默认 5），超出只给文字标记，防止上下文爆炸。
密钥：decrypted/media_keys.json（scripts/extract_image_key.py 生成，管理员跑一次）。
"""
import json
import os
import re
import struct
import threading
import time
from pathlib import Path
from typing import Optional

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
MEDIA_KEYS_PATH = os.path.join(PROJECT_ROOT, "decrypted", "media_keys.json")
HARDLINK_DB = os.path.join(PROJECT_ROOT, "decrypted", "hardlink", "hardlink.db")

_IMG_MD5_RE = re.compile(r"<md5>([0-9a-f]{32})</md5>")
_IMG_ATTR_MD5_RE = re.compile(r'<img[^>]*\bmd5="([0-9a-f]{32})"')
_V2_SIG = b"\x07\x08V2\x08\x07"

_lock = threading.Lock()
_keys_cache: Optional[dict] = None
_dir_cache: Optional[dict] = None
_conn = None


def load_keys() -> dict:
    global _keys_cache
    # 空缓存时每次都尝试重读（服务启动时密钥文件可能尚未生成）
    if _keys_cache:
        return _keys_cache
    try:
        with open(MEDIA_KEYS_PATH, encoding="utf-8") as f:
            _keys_cache = json.load(f)
    except Exception:
        _keys_cache = {}
    return _keys_cache


def _get_conn():
    global _conn
    if _conn is None:
        import sqlite3

        if not os.path.exists(HARDLINK_DB):
            return None
        _conn = sqlite3.connect(f"file:{HARDLINK_DB}?mode=ro", uri=True, check_same_thread=False)
    return _conn


def _dir_map(conn) -> dict:
    global _dir_cache
    if _dir_cache is not None:
        return _dir_cache
    try:
        _dir_cache = dict(conn.execute("SELECT rowid, username FROM dir2id").fetchall())
    except Exception:
        _dir_cache = {}
    return _dir_cache


def _account_dirs() -> list[Path]:
    base = Path(os.environ.get("WECHAT_FILES_DIR") or r"F:\xwechat_files")
    out = []
    if base.exists():
        for p in base.iterdir():
            if (p / "msg" / "attach").exists():
                out.append(p / "msg" / "attach")
    return out


def find_image_dat_bases(content: str) -> list[Path]:
    """消息XML -> 该消息的图片文件候选路径（原图、高清、缩略图按优先级）。"""
    if not content:
        return []
    m = _IMG_MD5_RE.search(content) or _IMG_ATTR_MD5_RE.search(content)
    if not m:
        return []
    md5 = m.group(1)
    conn = _get_conn()
    if conn is None:
        return []
    try:
        with _lock:
            row = conn.execute(
                "SELECT file_name, dir1, dir2 FROM image_hardlink_info_v4 WHERE md5=? LIMIT 1",
                (md5,),
            ).fetchone()
            if not row:
                return []
            dmap = _dir_map(conn)
    except Exception:
        return []
    file_name, dir1, dir2 = row
    d1, d2 = dmap.get(dir1), dmap.get(dir2)
    if not d1 or not d2:
        return []
    base = file_name[:-4] if file_name.endswith(".dat") else file_name
    # 数据库里的 file_name 可能自带变体后缀（_h / _t），去掉以免拼出 _h_h.dat 这种空名
    for vsuf in ("_h", "_t"):
        if base.endswith(vsuf):
            base = base[: -len(vsuf)]
            break
    out = []
    for attach in _account_dirs():
        img_dir = attach / d1 / d2 / "Img"
        for suffix in ("", "_h", "_t"):
            p = img_dir / f"{base}{suffix}.dat"
            if p.exists():
                out.append(p)
        if out:
            break
    return out


def find_image_dat(content: str) -> Optional[Path]:
    """兼容旧接口：返回第一个存在的图片文件。"""
    hits = find_image_dat_bases(content)
    return hits[0] if hits else None


def _xor_bytes(data: bytes, key: int) -> bytes:
    return bytes(b ^ key for b in data)


def _guess_xor_key(tail: bytes) -> Optional[int]:
    if len(tail) >= 2:
        k = tail[-1] ^ 0xD9
        if tail[-2] ^ k == 0xFF:
            return k
    if len(tail) >= 4:
        k = tail[-1] ^ 0x82
        if all(tail[-4 + i] ^ k == b"\xae\x42\x60\x82"[i] for i in range(4)):
            return k
    return None


def _strip_pkcs7(data: bytes) -> bytes:
    if not data:
        return data
    pad = data[-1]
    if 1 <= pad <= 16 and data[-pad:] == bytes([pad]) * pad:
        return data[:-pad]
    return data


def decrypt_v2_dat(data: bytes, aes_key_str: str, xor_key: Optional[int]) -> Optional[bytes]:
    """V2 格式：15字节头 + AES-ECB 段 + 16字节填充段（跳过）+ XOR 尾段。"""
    if data[:6] != _V2_SIG:
        return None
    try:
        sig, aes_size, xor_size = struct.unpack("<6sLL", data[:14])
    except struct.error:
        return None
    offset = 15
    aes_size_p = aes_size + (16 - aes_size % 16) % 16
    aes_data = data[offset : offset + aes_size_p]
    xor_data = data[-xor_size:] if xor_size > 0 else b""

    key = aes_key_str.encode("ascii", "ignore")[:16]
    try:
        try:
            from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

            d = Cipher(algorithms.AES(key), modes.ECB()).decryptor()
            aes_plain = d.update(aes_data) + d.finalize()
        except ImportError:
            from Crypto.Cipher import AES

            aes_plain = AES.new(key, AES.MODE_ECB).decrypt(aes_data)
        aes_plain = _strip_pkcs7(aes_plain)
    except Exception:
        return None

    if xor_data:
        if xor_key is None:
            xor_key = _guess_xor_key(xor_data)
            if xor_key is None:
                return None
        xored = _xor_bytes(xor_data, xor_key)
    else:
        xored = b""

    # 注意：AES 段与 XOR 段之间的 16 字节填充段不属于图像内容，直接跳过。
    out = aes_plain + xored
    if out[:3] == b"\xff\xd8\xff":
        return out
    if out[:4] in (b"\x89PNG", b"RIFF", b"GIF8"):
        return out
    return None


_EMOJI_AESKEY_RE = re.compile(r'aeskey\s*=\s*"([0-9a-fA-F]{32})"')
_EMOJI_URL_RES = [
    re.compile(r'encrypturl\s*=\s*"([^"]+)"'),  # 优先：解出来是 GIF/PNG 原始表情
    re.compile(r'cdnurl\s*=\s*"([^"]+)"'),
]
_EMOJI_MD5_RE = re.compile(r'\bmd5\s*=\s*"([0-9a-f]{32})"')
EMOTICON_CACHE_DIR = os.path.join(PROJECT_ROOT, "decrypted", "exports", "emoticon")


def _parse_emoji_xml(content: str):
    """表情消息XML -> (md5, aeskey, 下载链接列表)"""
    a = _EMOJI_AESKEY_RE.search(content)
    if not a:
        return None
    urls = []
    for pat in _EMOJI_URL_RES:
        m = pat.search(content)
        if m:
            urls.append(m.group(1).replace("&amp;", "&"))
    if not urls:
        return None
    m = _EMOJI_MD5_RE.search(content)
    return (m.group(1) if m else ""), a.group(1).lower(), urls


def _decrypt_emoticon(data: bytes, aeskey_hex: str) -> Optional[bytes]:
    """微信表情：AES-128-CBC，IV=密钥本身，PKCS7。"""
    import re as _re

    if not _re.fullmatch(r"[0-9a-f]{32}", aeskey_hex) or len(data) % 16 != 0:
        return None
    key = bytes.fromhex(aeskey_hex)
    try:
        try:
            from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

            d = Cipher(algorithms.AES(key), modes.CBC(key)).decryptor()
            out = d.update(data) + d.finalize()
        except ImportError:
            from Crypto.Cipher import AES

            out = AES.new(key, AES.MODE_CBC, key).decrypt(data)
        return _strip_pkcs7(out)
    except Exception:
        return None


def extract_emoticon_bytes(content: str) -> Optional[bytes]:
    """表情消息 -> 标准化 JPEG（首帧）。失败（无链接/过期/未知格式）返回 None。"""
    parsed = _parse_emoji_xml(content)
    if not parsed:
        return None
    md5, aeskey, urls = parsed
    os.makedirs(EMOTICON_CACHE_DIR, exist_ok=True)

    cache_path = os.path.join(EMOTICON_CACHE_DIR, (md5 or "unknown") + ".bin")
    raw = None
    try:
        if os.path.isfile(cache_path):
            with open(cache_path, "rb") as f:
                raw = f.read()
    except OSError:
        raw = None

    if raw is None:
        raw = b""
        import urllib.request

        for url in urls:
            try:
                with urllib.request.urlopen(url, timeout=15) as resp:
                    blob = resp.read(5 * 1024 * 1024 + 1)
                if len(blob) > 5 * 1024 * 1024:
                    continue
                dec = _decrypt_emoticon(blob, aeskey)
                if dec and (dec[:3] == b"\xff\xd8\xff" or dec[:4] in (b"GIF8", b"\x89PNG", b"RIFF")):
                    raw = dec
                    break
            except Exception:
                continue
        if raw:
            try:
                with open(cache_path, "wb") as f:
                    f.write(raw)
            except OSError:
                pass

    if not raw:
        return None
    return normalize_to_jpeg(raw)


def _cache_roots() -> list[Path]:
    """账号目录下的 cache 根（微信把渲染过的图片临时缓存成 wxgf 放这里）。"""
    base = Path(os.environ.get("WECHAT_FILES_DIR") or r"F:\xwechat_files")
    out = []
    if base.exists():
        for p in base.iterdir():
            if (p / "cache").exists():
                out.append(p / "cache")
    return out


def decode_wxgf(data: bytes) -> Optional[bytes]:
    """wxgf 容器 -> 首帧 JPEG 字节。

    wxgf = 微信自定义图片容器，头部以 b"wxgf" 开头，随后是 HEVC Annex-B 裸流
    （00 00 00 01 起始码起）。用 PyAV（自带 ffmpeg）解第一帧。
    """
    if not data:
        return None
    start = data.find(b"\x00\x00\x00\x01")
    if start < 0 or b"wxgf" not in data[:16]:
        return None
    stream = data[start:]
    try:
        import io

        import av
        from PIL import Image as PILImage
    except ImportError:
        return None
    try:
        container = av.open(io.BytesIO(stream), format="hevc")
        for frame in container.decode(video=0):
            img = frame.to_image()
            buf = io.BytesIO()
            img.convert("RGB").save(buf, "JPEG", quality=88)
            return buf.getvalue()
    except Exception:
        return None
    return None


def find_render_cache_image(chat_username: str, create_time: int, window: int = 3) -> Optional[bytes]:
    """微信渲染缓存兜底：cache/<年月>/Message/<md5(会话)>/ImageTemp/<类型>_<时间戳>_mid_temp_convert。

    这是客户端把图片转换后留下的临时缓存（wxgf/HEVC），分辨率通常是完整的，
    图片未落到 msg/attach 目录时可用。缓存本身会被微信清理，命中率取决于最近是否在客户端看过。
    文件名里的时间戳等于消息 create_time，允许 window 秒误差。
    """
    if not chat_username or not create_time:
        return None
    import hashlib
    import glob

    chat_md5 = hashlib.md5(chat_username.encode("utf-8", "ignore")).hexdigest()
    for root in _cache_roots():
        for ts in range(int(create_time) - window, int(create_time) + window + 1):
            pattern = os.path.join(str(root), "*", "Message", chat_md5, "ImageTemp", f"*_{ts}_*")
            for path in glob.glob(pattern):
                try:
                    with open(path, "rb") as f:
                        data = f.read()
                except OSError:
                    continue
                jpeg = decode_wxgf(data)
                if jpeg:
                    return jpeg
    return None


def _decode_dat(dat: Path, aes_key: str, xor_key) -> Optional[bytes]:
    try:
        data = dat.read_bytes()
    except OSError:
        return None
    raw = decrypt_v2_dat(data, aes_key, xor_key)
    if not raw:
        return None
    return normalize_to_jpeg(raw)


def extract_image_bytes(
    content: str, chat_username: str = "", create_time: int = 0
) -> Optional[bytes]:
    """消息内容 -> 标准化 JPEG 字节（长边压到 1600px）。

    降级顺序按清晰度排：原图/高清 → 微信渲染缓存（wxgf，通常是完整分辨率）
    → 缩略图（很小的 _t，只能看个大概）。统一转标准 JPEG，控制上下文成本。
    """
    keys = load_keys()
    aes_key = keys.get("image_aes_key")
    xor_key = keys.get("image_xor_key")
    if xor_key is not None:
        try:
            xor_key = int(xor_key)
        except (TypeError, ValueError):
            xor_key = None

    variants = find_image_dat_bases(content)
    if aes_key:
        for dat in variants:
            if dat.stem.endswith("_t"):
                continue
            jpeg = _decode_dat(dat, aes_key, xor_key)
            if jpeg:
                return jpeg
    cached = find_render_cache_image(chat_username, create_time)
    if cached:
        jpeg = normalize_to_jpeg(cached)
        if jpeg:
            return jpeg
    if aes_key:
        for dat in variants:
            if not dat.stem.endswith("_t"):
                continue
            jpeg = _decode_dat(dat, aes_key, xor_key)
            if jpeg:
                return jpeg
    return None


def normalize_to_jpeg(raw: bytes, max_side: int = 1600) -> Optional[bytes]:
    """任意格式图片字节 -> 标准 JPEG 字节（长边不超过 max_side，保持比例）。"""
    import io

    try:
        from PIL import Image as PILImage
    except ImportError:
        # 没有 Pillow 时原样返回（可能因格式变体被客户端拒收）
        return raw
    try:
        im = PILImage.open(io.BytesIO(raw))
        im.load()
        if im.mode in ("RGBA", "LA", "P"):
            im = im.convert("RGBA")
            bg = PILImage.new("RGB", im.size, (255, 255, 255))
            bg.paste(im, mask=im.split()[-1])
            im = bg
        elif im.mode != "RGB":
            im = im.convert("RGB")
        w, h = im.size
        if max(w, h) > max_side:
            scale = max_side / max(w, h)
            im = im.resize((max(1, int(w * scale)), max(1, int(h * scale))), PILImage.LANCZOS)
        buf = io.BytesIO()
        im.save(buf, format="JPEG", quality=82)
        return buf.getvalue()
    except Exception:
        return None


def image_media_type(data: bytes) -> str:
    if data[:3] == b"\xff\xd8\xff":
        return "image/jpeg"
    if data[:4] == b"\x89PNG":
        return "image/png"
    if data[:4] == b"RIFF":
        return "image/webp"
    if data[:3] == b"GIF":
        return "image/gif"
    return "image/jpeg"

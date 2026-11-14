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
import time
from pathlib import Path
from typing import Optional

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
MEDIA_KEYS_PATH = os.path.join(PROJECT_ROOT, "decrypted", "media_keys.json")
HARDLINK_DB = os.path.join(PROJECT_ROOT, "decrypted", "hardlink", "hardlink.db")

_IMG_MD5_RE = re.compile(r"<md5>([0-9a-f]{32})</md5>")
_IMG_LEN_RE = re.compile(r'\blength="(\d+)"')
_THUMB_LEN_RE = re.compile(r'cdnthumblength="(\d+)"')
_THUMB_WH_RE = re.compile(r'cdnthumbwidth="(\d+)"[^>]*cdnthumbheight="(\d+)"')
# V2 容器体积 = 明文长度 + 15 字节头 + AES 填充块 + 16 字节偏移段，
# 实测文件比明文长 31 到 47 字节；先用这个窗口筛，命中就不必再解别的文件。
_SIZE_MIN, _SIZE_MAX = 31, 47
_dir_listing: dict = {}
_IMG_ATTR_MD5_RE = re.compile(r'<img[^>]*\bmd5="([0-9a-f]{32})"')
_V2_SIG = b"\x07\x08V2\x08\x07"

_keys_cache: Optional[dict] = None
_dir_cache: Optional[tuple] = None


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


def _hardlink_conn():
    """每次查询新开连接，查完立即关闭，不缓存。

    Windows 下 sqlite 打开的文件不共享 DELETE，进程长期持有连接会让解密流程
    无法用 os.replace 替换 decrypted/hardlink/hardlink.db：hardlink 副本永久
    停在旧版本，登记之后收到的图片就只剩 kind 标记。2026-11-14 实测确认。
    """
    import sqlite3

    from .locator import latest_copy

    target = latest_copy(Path(HARDLINK_DB))
    if not target.exists():
        return None
    # immutable=1：不生成 -wal / -shm 旁文件，也不留下写句柄
    return sqlite3.connect(f"file:{target.as_posix()}?mode=ro&immutable=1", uri=True)


def _dir_map(conn) -> dict:
    """dir2id 映射，按副本 mtime 缓存；副本被替换后自动重读。"""
    global _dir_cache
    try:
        stamp = os.stat(HARDLINK_DB + ".new").st_mtime
    except OSError:
        stamp = 0.0
    try:
        stamp = max(stamp, os.stat(HARDLINK_DB).st_mtime)
    except OSError:
        pass
    if _dir_cache and _dir_cache[0] == stamp:
        return _dir_cache[1]
    try:
        mapping = dict(conn.execute("SELECT rowid, username FROM dir2id").fetchall())
    except Exception:
        mapping = {}
    _dir_cache = (stamp, mapping)
    return mapping


def _month_dirs(create_time: int) -> list[str]:
    """消息所在年月，前后各补一个月，兼容跨月下载。"""
    if not create_time:
        return []
    out = []
    for delta in (0, -1, 1):
        ts = int(create_time) + delta * 30 * 86400
        out.append(time.strftime("%Y-%m", time.localtime(ts)))
    return out


def _attach_img_dir(chat_id: str, month: str) -> Optional[Path]:
    import hashlib

    if not chat_id:
        return None
    chat_md5 = hashlib.md5(chat_id.encode("utf-8", "ignore")).hexdigest()
    for attach in _account_dirs():
        d = attach / chat_md5 / month / "Img"
        if d.exists():
            return d
    return None


def _list_sized(directory: Path, pattern: str) -> list:
    """目录内容按 (路径, 体积) 缓存，目录 mtime 变则重扫。"""
    try:
        stamp = directory.stat().st_mtime
    except OSError:
        return []
    key = (str(directory), pattern)
    cached = _dir_listing.get(key)
    if cached and cached[0] == stamp:
        return cached[1]
    items = []
    for p in directory.glob(pattern):
        try:
            items.append((p, p.stat().st_size))
        except OSError:
            continue
    _dir_listing[key] = (stamp, items)
    return items


def _dims_of(raw: bytes) -> Optional[tuple]:
    """图片字节的宽高；wxgf 容器先解 HEVC 首帧。"""
    import io

    if raw[:4] == b"wxgf":
        raw = decode_wxgf(raw) or b""
    try:
        from PIL import Image as PILImage

        with PILImage.open(io.BytesIO(raw)) as im:
            return im.size
    except Exception:
        return None


def _pick_by_meta(
    directories: list,
    pattern: str,
    expect_len: int,
    expect_wh: Optional[tuple],
    aes_key: Optional[str],
    xor_key,
    tol: int = 0,
) -> Optional[bytes]:
    """按消息自带的体积与尺寸在本地找文件：不依赖 hardlink 登记表。

    微信 4.x 的登记表可能滞后或改命名策略，但消息 XML 里带着缩略图与原图的
    明文长度、宽高，拿来和磁盘文件逐一比对就能认出来。
    """
    if not expect_len:
        return None
    window = (tol, tol) if tol else (_SIZE_MIN, _SIZE_MAX)
    best = None
    for directory in directories:
        if directory is None:
            continue
        for path, size in _list_sized(directory, pattern):
            delta = size - expect_len
            if delta < window[0] or delta > window[1]:
                continue
            try:
                blob = path.read_bytes()
            except OSError:
                continue
            if aes_key is not None:
                blob = decrypt_v2_dat(blob, aes_key, xor_key) or b""
            if len(blob) != expect_len:
                continue
            if expect_wh:
                dims = _dims_of(blob)
                if dims and tuple(expect_wh) != dims:
                    continue
            if best is None or len(blob) > len(best):
                best = blob
    return best


def _meta_of(content: str) -> tuple:
    """消息 XML -> (原图长度, 缩略图长度, 缩略图宽高)。"""
    m_len = _IMG_LEN_RE.search(content or "")
    m_tlen = _THUMB_LEN_RE.search(content or "")
    m_wh = _THUMB_WH_RE.search(content or "")
    length = int(m_len.group(1)) if m_len else 0
    thumb_len = int(m_tlen.group(1)) if m_tlen else 0
    wh = (int(m_wh.group(1)), int(m_wh.group(2))) if m_wh else None
    return length, thumb_len, wh


def _account_dirs() -> list[Path]:
    base = Path(os.environ.get("WECHAT_FILES_DIR") or r"F:\xwechat_files")
    out = []
    if base.exists():
        for p in base.iterdir():
            if (p / "msg" / "attach").exists():
                out.append(p / "msg" / "attach")
    return out


def image_locate(
    content: str, chat_id: str = "", local_id: int = 0, create_time: int = 0
) -> tuple[list[Path], str]:
    """消息XML -> (本地 .dat 候选路径, 找不到的原因)。

    两条来源互相补充：微信 4.x 的新文件只写 message_resource，老文件只写 hardlink，
    任何一条给出原图都要用上。候选按原图、高清、缩略图的顺序返回。
    原因取值：no_md5 / hardlink_row_missing / dir_map_missing / file_not_on_disk，成功为空串。
    """
    if not content:
        return [], "no_content"
    m = _IMG_MD5_RE.search(content) or _IMG_ATTR_MD5_RE.search(content)
    if not m:
        return [], "no_md5"

    from . import resource as resource_mod

    found: list[Path] = list(resource_mod.media_paths(chat_id, local_id, create_time, "img"))
    reason = ""

    conn = _hardlink_conn()
    row = None
    dmap: dict = {}
    if conn is not None:
        try:
            row = conn.execute(
                "SELECT file_name, dir1, dir2 FROM image_hardlink_info_v4 WHERE md5=? LIMIT 1",
                (m.group(1),),
            ).fetchone()
            dmap = _dir_map(conn) if row else {}
        except Exception:
            row = None
        finally:
            conn.close()
    if row:
        file_name, dir1, dir2 = row
        d1, d2 = dmap.get(dir1), dmap.get(dir2)
        base = file_name[:-4] if file_name.endswith(".dat") else file_name
        # 登记名可能自带变体后缀（_h / _t），去掉以免拼出 _h_h.dat 这种空名
        for vsuf in ("_h", "_t"):
            if base.endswith(vsuf):
                base = base[: -len(vsuf)]
                break
        if not d1 or not d2:
            reason = "dir_map_missing"
        else:
            for attach in _account_dirs():
                img_dir = attach / d1 / d2 / "Img"
                got = False
                for suffix in ("", "_h", "_t"):
                    p = img_dir / f"{base}{suffix}.dat"
                    if p.exists():
                        if p not in found:
                            found.append(p)
                        got = True
                if got:
                    break
            if not got:
                reason = "file_not_on_disk"
    elif not found:
        reason = "hardlink_row_missing"

    if not found:
        return [], reason or "file_not_on_disk"
    found.sort(key=lambda p: (p.stem.endswith("_t"), str(p)))
    return found, ""


def find_image_dat_bases(content: str) -> list[Path]:
    """消息XML -> 该消息的图片文件候选路径（原图、高清、缩略图按优先级）。"""
    return image_locate(content)[0]


def find_image_dat(content: str) -> Optional[Path]:
    """兼容旧接口：返回第一个存在的图片文件。"""
    hits = find_image_dat_bases(content)
    return hits[0] if hits else None


_XOR_TABLES: dict = {}


def _xor_bytes(data: bytes, key: int) -> bytes:
    """整段异或。用 translate 表走 C 实现，纯 Python 逐字节在几百 KB 的图上很慢。"""
    table = _XOR_TABLES.get(key)
    if table is None:
        table = bytes(i ^ key for i in range(256))
        _XOR_TABLES[key] = table
    return data.translate(table)


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
    # 新版本微信把原图存成 wxgf 容器（HEVC 裸流），需解出首帧。
    # 之前这里直接返回 None，导致 wxgf 格式的本地原图全部取不到。
    if out[:4] == b"wxgf":
        return decode_wxgf(out)
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


def extract_image_meta(
    content: str, chat_username: str = "", create_time: int = 0, local_id: int = 0
) -> tuple[Optional[bytes], str, str]:
    """消息内容 -> (标准化 JPEG 字节, 来源标记, 失败原因)。

    来源标记：dat（原图/高清）、cache（微信渲染缓存）、thumb（小缩略图）。
    降级顺序按清晰度排：原图/高清 → 渲染缓存 → 缩略图。
    失败原因原样透出，交给上层写进 media.missing_reason，不再静默返回空。
    """
    keys = load_keys()
    aes_key = keys.get("image_aes_key")
    xor_key = keys.get("image_xor_key")
    if xor_key is not None:
        try:
            xor_key = int(xor_key)
        except (TypeError, ValueError):
            xor_key = None

    variants, reason = image_locate(content, chat_username, local_id, create_time)
    if aes_key:
        for dat in variants:
            if dat.stem.endswith("_t"):
                continue
            jpeg = _decode_dat(dat, aes_key, xor_key)
            if jpeg:
                return jpeg, "dat", ""
    cached = find_render_cache_image(chat_username, create_time)
    if cached:
        jpeg = normalize_to_jpeg(cached)
        if jpeg:
            return jpeg, "cache", ""
    if aes_key:
        for dat in variants:
            if not dat.stem.endswith("_t"):
                continue
            jpeg = _decode_dat(dat, aes_key, xor_key)
            if jpeg:
                return jpeg, "thumb", ""

        # 登记表没有时按消息自带长度与宽高在盘上认文件：先原图，再缩略图。
        # 微信 4.x 的 image_hardlink_info_v4 会滞后，磁盘文件却一直在。
        length, thumb_len, wh = _meta_of(content)
        dirs = [_attach_img_dir(chat_username, m) for m in _month_dirs(create_time)]
        for pattern, expect_len, expect_wh, src in (
            ("*.dat", length, None, "dat"),
            ("*_t.dat", thumb_len, wh, "thumb"),
        ):
            if not expect_len:
                continue
            blob = _pick_by_meta(dirs, pattern, expect_len, expect_wh, aes_key, xor_key)
            if blob:
                jpeg = normalize_to_jpeg(blob)
                if jpeg:
                    return jpeg, src, ""
    if not aes_key:
        return None, "", reason or "no_image_key"
    if variants:
        return None, "", "decode_failed"
    return None, "", "no_local_image"


def extract_image_bytes(
    content: str, chat_username: str = "", create_time: int = 0, local_id: int = 0
) -> Optional[bytes]:
    """兼容接口：只要图片字节。"""
    return extract_image_meta(content, chat_username, create_time, local_id)[0]


_VIDEO_MD5_RE = re.compile(r'<videomsg[^>]*\bmd5="([0-9a-f]{32})"')
_VIDEO_DIRS = "video"


def extract_video_cover(
    content: str, create_time: int = 0, chat_id: str = "", local_id: int = 0
) -> Optional[bytes]:
    """视频消息（type 43）-> 封面 JPEG 字节。

    本地封面是明文：msg/video/<年月>/<文件基名>.jpg（另有 _thumb.jpg 更小）。
    优先按 hardlink 的 video_hardlink_info_v4 查文件名；查不到时按消息 XML 里的
    cdnthumblength 与 cdnthumbwidth/height 在目录里比对，不依赖登记表。
    """
    if not content:
        return None
    from . import resource as resource_mod

    for p in resource_mod.media_paths(chat_id, local_id, create_time, "video"):
        try:
            jpeg = normalize_to_jpeg(p.read_bytes())
        except OSError:
            jpeg = None
        if jpeg:
            return jpeg
    m = _VIDEO_MD5_RE.search(content)
    if not m:
        return _video_cover_by_meta(content, create_time)
    conn = _hardlink_conn()
    if conn is None:
        return _video_cover_by_meta(content, create_time)
    try:
        row = conn.execute(
            "SELECT file_name, dir1 FROM video_hardlink_info_v4 WHERE md5=? LIMIT 1",
            (m.group(1),),
        ).fetchone()
        dmap = _dir_map(conn) if row else {}
    except Exception:
        return _video_cover_by_meta(content, create_time)
    finally:
        conn.close()
    if not row:
        return _video_cover_by_meta(content, create_time)
    file_name, dir1 = row
    base = file_name
    for suffix in (".mp4", ".mp4_raw", "_raw.mp4"):
        if base.endswith(suffix):
            base = base[: -len(suffix)]
            break
    if base.endswith("_raw"):
        base = base[: -4]
    month = dmap.get(dir1)
    cand_dirs = []
    for acc in Path(os.environ.get("WECHAT_FILES_DIR") or r"F:\xwechat_files").iterdir():
        vroot = acc / "msg" / _VIDEO_DIRS
        if not vroot.exists():
            continue
        if month:
            cand_dirs.append(vroot / month)
        cand_dirs.append(vroot)
    for d in cand_dirs:
        for name in (f"{base}.jpg", f"{base}_thumb.jpg"):
            p = d / name
            if p.exists():
                try:
                    jpeg = normalize_to_jpeg(p.read_bytes())
                except OSError:
                    continue
                if jpeg:
                    return jpeg
    return _video_cover_by_meta(content, create_time)


def _video_dirs(create_time: int) -> list[Path]:
    base = Path(os.environ.get("WECHAT_FILES_DIR") or r"F:\xwechat_files")
    if not base.exists():
        return []
    out = []
    for acc in base.iterdir():
        vroot = acc / "msg" / _VIDEO_DIRS
        if not vroot.exists():
            continue
        for month in _month_dirs(create_time):
            d = vroot / month
            if d.exists():
                out.append(d)
        out.append(vroot)
    return out


def _video_cover_by_meta(content: str, create_time: int) -> Optional[bytes]:
    """封面不依赖登记表：按 cdnthumblength 与宽高在 msg/video 目录里比对。"""
    _, thumb_len, wh = _meta_of(content)
    if not thumb_len:
        return None
    blob = _pick_by_meta(_video_dirs(create_time), "*.jpg", thumb_len, wh, None, None, tol=8)
    return normalize_to_jpeg(blob) if blob else None


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
        scale = 1.0
        if max(w, h) > max_side:
            scale = max_side / max(w, h)
        if h > 3 * w:
            # 长截图按长边压会把宽度压到几十像素，直接不可读。
            # 改成保底宽 600、总高不超过 12000，交给上层切段。
            tall = min(1.0, 12000 / h)
            scale = max(scale, min(tall, max(600 / w, tall)))
        if scale < 1.0:
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

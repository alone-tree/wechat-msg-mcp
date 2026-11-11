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
    if _keys_cache is not None:
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


def find_image_dat(content: str) -> Optional[Path]:
    """消息XML(或含md5的文本) -> 图片 dat 文件路径。"""
    if not content:
        return None
    m = _IMG_MD5_RE.search(content) or _IMG_ATTR_MD5_RE.search(content)
    if not m:
        return None
    md5 = m.group(1)
    conn = _get_conn()
    if conn is None:
        return None
    try:
        with _lock:
            row = conn.execute(
                "SELECT file_name, dir1, dir2 FROM image_hardlink_info_v4 WHERE md5=? LIMIT 1",
                (md5,),
            ).fetchone()
            if not row:
                return None
            dmap = _dir_map(conn)
    except Exception:
        return None
    file_name, dir1, dir2 = row
    d1, d2 = dmap.get(dir1), dmap.get(dir2)
    if not d1 or not d2:
        return None
    for attach in _account_dirs():
        p = attach / d1 / d2 / "Img" / file_name
        if p.exists():
            return p
    return None


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
    """V2 格式：15字节头 + AES-ECB 段 + 明文中段 + XOR 尾段。"""
    if data[:6] != _V2_SIG:
        return None
    try:
        sig, aes_size, xor_size = struct.unpack("<6sLL", data[:14])
    except struct.error:
        return None
    offset = 15
    aes_size_p = aes_size + (16 - aes_size % 16) % 16
    aes_data = data[offset : offset + aes_size_p]
    rest = data[offset + aes_size_p :]
    if xor_size > 0:
        xor_data = data[-xor_size:]
        raw_mid = rest[: len(rest) - xor_size] if len(rest) >= xor_size else b""
    else:
        xor_data = b""
        raw_mid = rest

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

    out = aes_plain + raw_mid + xored
    # 魔数确认是图片
    if out[:3] == b"\xff\xd8\xff":
        return out
    if out[:4] in (b"\x89PNG", b"RIFF", b"GIF8"):
        return out
    return None


def extract_image_bytes(content: str) -> Optional[bytes]:
    """消息内容 -> 解密后的图片字节。失败（无钥匙/无文件/非V2）返回 None。"""
    keys = load_keys()
    aes_key = keys.get("image_aes_key")
    if not aes_key:
        return None
    dat = find_image_dat(content)
    if not dat:
        return None
    try:
        data = dat.read_bytes()
    except OSError:
        return None
    xor_key = keys.get("image_xor_key")
    if xor_key is not None:
        try:
            xor_key = int(xor_key)
        except (TypeError, ValueError):
            xor_key = None
    return decrypt_v2_dat(data, aes_key, xor_key)


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

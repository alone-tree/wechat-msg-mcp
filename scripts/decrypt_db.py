#!/usr/bin/env python3
"""
微信数据库解密脚本（Mac & Windows 通用）。

从 keys.json 读取密钥，将加密的 SQLCipher 4 数据库解密为标准 SQLite 文件，
输出到 decrypted/ 目录，供 MCP Server 读取。

支持平台：
  - macOS：~/Library/Containers/.../xwechat_files（新版）或 2.0b4.0.9/（旧版）
  - Windows：~/Documents/xwechat_files（微信 4.x Windows 版，结构与 Mac 新版相同）

使用方法：
  python3 scripts/decrypt_db.py   # Mac
  uv run python scripts/decrypt_db.py  # Windows（通过 uv）

依赖：
  pip install pycryptodome  (或通过 uv sync 安装)
"""
import hashlib
import json
import os
import re
import shutil
import sqlite3
import struct
import sys
from pathlib import Path

try:
    from Crypto.Cipher import AES
except ImportError:
    print("❌ 缺少依赖 pycryptodome，请运行：")
    print("   uv sync  或  pip install pycryptodome")
    sys.exit(1)

PROJECT_ROOT = Path(__file__).parent.parent
KEYS_FILE = PROJECT_ROOT / "keys.json"
OUTPUT_DIR = PROJECT_ROOT / "decrypted"

# ── 平台路径配置 ─────────────────────────────────────────────────────────────

# macOS 旧版（2.0b4.0.9/<account_hash>/）
WECHAT_BASE = Path(
    "~/Library/Containers/com.tencent.xinWeChat/Data/Library/"
    "Application Support/com.tencent.xinWeChat/2.0b4.0.9"
).expanduser()

# macOS 新版（xwechat_files/<wxid>/db_storage/）
WECHAT_DOCS_BASE = Path(
    "~/Library/Containers/com.tencent.xinWeChat/Data/Documents/xwechat_files"
).expanduser()

# Windows 微信 4.x（~/Documents/xwechat_files/<wxid>/db_storage/）
# 结构与 macOS 新版完全相同，复用相同解密逻辑
def _get_windows_xwechat_base() -> Path:
    """获取 Windows 版 xwechat_files 目录（支持注册表和默认路径）"""
    if sys.platform != "win32":
        return Path("/nonexistent")
    # 优先从注册表读取自定义保存路径
    try:
        import winreg
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, r"Software\Tencent\WeChat") as key:
            val, _ = winreg.QueryValueEx(key, "FileSavePath")
            if val and val != "MyDocument:":
                candidate = Path(val) / "xwechat_files"
                if candidate.exists():
                    return candidate
    except Exception:
        pass
    # 默认路径：~/Documents/xwechat_files
    return Path.home() / "Documents" / "xwechat_files"

WECHAT_WIN_BASE = _get_windows_xwechat_base()

# SQLCipher 4 参数
PAGE_SIZE = 4096
SALT_SIZE = 16
KEY_SIZE = 32
HMAC_KEY_SIZE = 32
IV_SIZE = 16
HMAC_SIZE = 64       # SHA-512
PBKDF2_ITER = 256000
PBKDF2_HASH = "sha512"
RESERVED_SIZE = IV_SIZE + HMAC_SIZE  # 每页末尾保留区域大小


def derive_keys(passphrase: bytes, salt: bytes) -> tuple[bytes, bytes]:
    """
    SQLCipher 4 密钥派生（旧版）：
    使用 PBKDF2-HMAC-SHA512 生成 64 字节，
    前 32 字节为加密 key，后 32 字节为 HMAC key。
    """
    derived = hashlib.pbkdf2_hmac(PBKDF2_HASH, passphrase, salt, PBKDF2_ITER, dklen=64)
    return derived[:KEY_SIZE], derived[KEY_SIZE:]


def _try_direct_key(src_path: Path, key_hex: bytes, dst_path: Path) -> bool:
    """
    新版微信 (xwechat_files) 用法：
    enc_key 直接是 32 字节 AES key（不经过 PBKDF2）。
    页面布局（参考 WxEcho decrypt_db.ts）：
      Page 1: [salt(16)] [encrypted(4000)] [iv(16)] [hmac(64)]
        → 解密后重建: SQLiteHeader(16) + decrypted(3984) + zeros(80)
      Page N: [encrypted(4016)] [iv(16)] [hmac(64)]
        → 解密后: decrypted(4016) + zeros(80)
    """
    SQLITE_HDR = b"SQLite format 3\x00"
    RESERVE_SZ = IV_SIZE + HMAC_SIZE  # 80

    try:
        with open(src_path, "rb") as f:
            raw = f.read()
    except OSError:
        return False

    if len(raw) < PAGE_SIZE:
        return False

    page1 = raw[:PAGE_SIZE]
    # Page 1: iv at [PAGE_SIZE - RESERVE_SZ : PAGE_SIZE - RESERVE_SZ + IV_SIZE]
    iv1 = page1[PAGE_SIZE - RESERVE_SZ : PAGE_SIZE - RESERVE_SZ + IV_SIZE]
    enc1 = page1[SALT_SIZE : PAGE_SIZE - RESERVE_SZ]  # [16:4016]

    try:
        cipher = AES.new(key_hex, AES.MODE_CBC, iv1)
        dec1 = cipher.decrypt(enc1)
    except Exception:
        return False

    # Rebuild page 1: SQLite header + decrypted + zero reserve
    page1_out = bytearray(PAGE_SIZE)
    page1_out[:16] = SQLITE_HDR
    page1_out[16 : 16 + len(dec1)] = dec1
    # reserve bytes stay zero

    if bytes(page1_out[:16]) != SQLITE_HDR:
        return False

    total_pages = len(raw) // PAGE_SIZE
    dst_path.parent.mkdir(parents=True, exist_ok=True)

    with open(dst_path, "wb") as out:
        out.write(bytes(page1_out))
        for page_no in range(1, total_pages):
            offset = page_no * PAGE_SIZE
            page_data = raw[offset : offset + PAGE_SIZE]
            if len(page_data) < PAGE_SIZE:
                out.write(page_data)
                continue
            # Page N: iv at [PAGE_SIZE - RESERVE_SZ : ...]
            ivN = page_data[PAGE_SIZE - RESERVE_SZ : PAGE_SIZE - RESERVE_SZ + IV_SIZE]
            encN = page_data[: PAGE_SIZE - RESERVE_SZ]
            try:
                cipher = AES.new(key_hex, AES.MODE_CBC, ivN)
                decN = cipher.decrypt(encN)
                page_out = bytearray(PAGE_SIZE)
                page_out[: len(decN)] = decN
                out.write(bytes(page_out))
            except Exception:
                out.write(page_data)

    try:
        uri = f"file:{dst_path}?mode=ro&immutable=1"
        conn = sqlite3.connect(uri, uri=True)
        conn.execute("SELECT count(*) FROM sqlite_master").fetchone()
        conn.close()
        return True
    except sqlite3.DatabaseError:
        dst_path.unlink(missing_ok=True)
        return False


def _match_key_by_salt(src_path: Path, keys: dict) -> bytes | None:
    """
    读取 db 文件前 16 字节作为 salt，
    在 keys.json 的 x'<64hex_key><32hex_salt>' 格式里找匹配项，
    返回 32 字节的 enc_key，找不到返回 None。
    """
    try:
        with open(src_path, "rb") as f:
            db_salt = f.read(16)
    except OSError:
        return None

    if len(db_salt) < 16:
        return None

    db_salt_hex = db_salt.hex().lower()

    for key_hex_str in keys.values():
        m = re.match(r"x'([0-9a-fA-F]+)'", key_hex_str)
        if not m:
            continue
        hex_data = m.group(1).lower()
        if len(hex_data) < 96:
            continue
        salt_hex = hex_data[64:96]  # 后 32 hex = 16 字节 salt
        if salt_hex == db_salt_hex:
            return bytes.fromhex(hex_data[:64])  # 前 64 hex = 32 字节 key

    return None


def decrypt_page(page_data: bytes, enc_key: bytes, page_no: int) -> bytes | None:
    """
    解密一页数据（SQLCipher 4 格式）。
    页结构：[payload_encrypted | iv(16) | hmac(64)]
    """
    if len(page_data) < RESERVED_SIZE + 16:
        return None

    payload_size = PAGE_SIZE - RESERVED_SIZE
    reserved = page_data[payload_size:]
    iv = reserved[:IV_SIZE]
    payload = page_data[:payload_size]

    try:
        cipher = AES.new(enc_key, AES.MODE_CBC, iv)
        decrypted = cipher.decrypt(payload)
        return decrypted + reserved
    except Exception:
        return None


def decrypt_database(src_path: Path, key_hex_str: str, dst_path: Path, keys: dict | None = None, silent: bool = False) -> bool:
    """
    解密一个 SQLCipher 4 数据库文件。

    :param src_path: 加密数据库路径
    :param key_hex_str: 格式 "x'<64hex><32hex>'" 或 "<64hex>"
    :param dst_path: 输出解密后的数据库路径
    :param keys: 完整 keys dict，用于 salt 匹配（新版微信直接 key 模式）
    :return: 是否成功
    """
    # 新版微信：先尝试用 salt 匹配直接 key 解密
    if keys is not None:
        direct_key = _match_key_by_salt(src_path, keys)
        if direct_key is not None:
            if _try_direct_key(src_path, direct_key, dst_path):
                return True
            # salt 匹配但直接解密失败，继续尝试旧式 PBKDF2 方式
    # 解析密钥
    m = re.match(r"x'([0-9a-fA-F]+)'", key_hex_str)
    if m:
        hex_data = m.group(1)
    elif re.match(r"[0-9a-fA-F]+", key_hex_str):
        hex_data = key_hex_str
    else:
        if not silent:
            print(f"  ❌ 密钥格式无法识别: {key_hex_str[:20]}...")
        return False

    if len(hex_data) < 64:
        if not silent:
            print(f"  ❌ 密钥太短: {len(hex_data)} hex chars")
        return False

    passphrase = bytes.fromhex(hex_data[:64])

    try:
        with open(src_path, "rb") as f:
            raw = f.read()
    except OSError as e:
        if not silent:
            print(f"  ❌ 读取文件失败: {e}")
        return False

    if len(raw) < PAGE_SIZE:
        if not silent:
            print(f"  ❌ 文件过小: {len(raw)} 字节")
        return False

    # 第一页：前16字节是 salt
    salt = raw[:SALT_SIZE]
    enc_key, hmac_key = derive_keys(passphrase, salt)

    # 验证：解密第一页并检查 SQLite magic header
    first_page_encrypted = raw[SALT_SIZE:PAGE_SIZE]
    # SQLCipher 4 第一页结构（去掉 salt 后）：
    # [payload(PAGE_SIZE - SALT_SIZE - RESERVED) | iv | hmac]
    first_payload_size = PAGE_SIZE - SALT_SIZE - RESERVED_SIZE
    first_payload = first_page_encrypted[:first_payload_size]
    first_reserved = first_page_encrypted[first_payload_size:]
    first_iv = first_reserved[:IV_SIZE]

    try:
        cipher = AES.new(enc_key, AES.MODE_CBC, first_iv)
        first_decrypted_payload = cipher.decrypt(first_payload)
    except Exception as e:
        if not silent:
            print(f"  ❌ 解密第一页失败: {e}")
        return False

    # 检查 SQLite magic header
    sqlite_magic = b"SQLite format 3\x00"
    if not first_decrypted_payload.startswith(sqlite_magic):
        if not silent:
            print(f"  ❌ 密钥不正确（magic header 校验失败）")
        return False

    # 解密所有页
    dst_path.parent.mkdir(parents=True, exist_ok=True)
    total_pages = len(raw) // PAGE_SIZE

    with open(dst_path, "wb") as out:
        # 第一页：写入解密内容（带上 sqlite header）
        # 第一页解密后的完整内容
        out.write(first_decrypted_payload + first_reserved)

        # 后续页
        for page_no in range(1, total_pages):
            offset = page_no * PAGE_SIZE
            page_data = raw[offset: offset + PAGE_SIZE]

            if len(page_data) < RESERVED_SIZE:
                out.write(page_data)
                continue

            payload_size = PAGE_SIZE - RESERVED_SIZE
            reserved = page_data[payload_size:]
            payload = page_data[:payload_size]
            iv = reserved[:IV_SIZE]

            try:
                cipher = AES.new(enc_key, AES.MODE_CBC, iv)
                decrypted_payload = cipher.decrypt(payload)
                out.write(decrypted_payload + reserved)
            except Exception:
                # 写入原始数据（部分页可能不加密）
                out.write(page_data)

    # 验证解密结果
    try:
        uri = f"file:{dst_path}?mode=ro&immutable=1"
        conn = sqlite3.connect(uri, uri=True)
        conn.execute("SELECT count(*) FROM sqlite_master").fetchone()
        conn.close()
        return True
    except sqlite3.DatabaseError as e:
        if not silent:
            print(f"  ❌ 解密后验证失败: {e}")
        dst_path.unlink(missing_ok=True)
        return False


def find_db_for_key(key_label: str) -> list[Path]:
    """
    找到所有需要解密的数据库文件。

    支持三种路径结构：
      旧版 Mac：.../2.0b4.0.9/<account_hash>/Message/msg_*.db
      新版 Mac：.../xwechat_files/<wxid>/db_storage/message/message_*.db
      Windows：~/Documents/xwechat_files/<wxid>/db_storage/message/message_*.db
    """
    db_files = []

    # --- 旧版 Mac 路径 ---
    if WECHAT_BASE.exists():
        for account_dir in WECHAT_BASE.iterdir():
            if not account_dir.is_dir():
                continue
            for pattern in [
                "Contact/wccontact_new2.db",
                "Session/session_new.db",
                "Message/msg_*.db",
                "Group/group_new.db",
            ]:
                db_files.extend(account_dir.glob(pattern))

    # --- 新版 Mac / Windows 共用路径（xwechat_files 结构相同）---
    for xwechat_base in [WECHAT_DOCS_BASE, WECHAT_WIN_BASE]:
        if not xwechat_base.exists():
            continue
        for wxid_dir in xwechat_base.iterdir():
            if not wxid_dir.is_dir():
                continue
            db_storage = wxid_dir / "db_storage"
            if not db_storage.exists():
                continue
            for pattern in [
                "contact/contact.db",
                "session/session.db",
                "message/message_*.db",
                "message/biz_message_*.db",
                "group/group.db",
            ]:
                db_files.extend(db_storage.glob(pattern))

    return db_files


def build_output_path(src_path: Path) -> Path:
    """
    将源数据库路径映射到解密输出路径。

    旧版：.../Message/msg_0.db        -> decrypted/message/message_0.db
    新版：.../db_storage/message/message_0.db -> decrypted/message/message_0.db
         .../db_storage/contact/contact.db   -> decrypted/contact/contact.db
    """
    name = src_path.name
    parent = src_path.parent.name.lower()

    # 统一化目录名
    dir_map = {
        "message": "message",
        "contact": "contact",
        "session": "session",
        "group": "group",
        "favorites": "favorites",
    }
    out_dir = dir_map.get(parent, parent)

    # 统一化文件名（旧版 msg_N.db -> message_N.db，新版保持不变）
    name_out = re.sub(r"^msg_(\d+)\.db$", r"message_\1.db", name)
    name_out = re.sub(r"^wccontact_new2\.db$", "contact.db", name_out)
    name_out = re.sub(r"^session_new\.db$", "session.db", name_out)
    name_out = re.sub(r"^group_new\.db$", "group.db", name_out)

    return OUTPUT_DIR / out_dir / name_out


def main():
    print("=" * 60)
    print("  微信数据库解密工具（Mac & Windows）")
    print("=" * 60)
    print()

    if not KEYS_FILE.exists():
        print(f"❌ 找不到密钥文件: {KEYS_FILE}")
        print("Mac 用户请先运行:     python3 scripts/extract_key.py")
        print("Windows 用户请先运行: uv run python scripts/extract_key_windows.py")
        sys.exit(1)

    with open(KEYS_FILE) as f:
        keys: dict = json.load(f)

    if not keys:
        print("❌ keys.json 为空，请重新运行 extract_key.py")
        sys.exit(1)

    print(f"📋 加载密钥: {len(keys)} 个")
    print(f"📂 输出目录: {OUTPUT_DIR}")
    print()

    # 判断 keys.json 是否已匹配到路径
    has_matched_paths = any("/" in k or "\\" in k for k in keys.keys())

    success_count = 0
    fail_count = 0

    if has_matched_paths:
        # keys 的 key 是数据库相对路径，需要找到对应的绝对路径
        # 搜索顺序：
        #   1. 直接拼 xwechat_files 父目录（Windows / Mac 新版）
        #   2. 在 WECHAT_BASE 旧版目录里搜索文件名（Mac 旧版）
        #   3. 直接把 key 当绝对路径尝试
        xwechat_parent = WECHAT_WIN_BASE.parent if WECHAT_WIN_BASE.exists() \
                         else WECHAT_DOCS_BASE.parent

        for rel_path, key_hex in keys.items():
            src_found = None

            # 方式1：相对路径拼接 xwechat_files 上级（适用新版 Mac/Windows）
            candidate = xwechat_parent / rel_path
            if candidate.exists():
                src_found = candidate

            # 方式2：Mac 旧版目录搜索文件名
            if not src_found and WECHAT_BASE.exists():
                fname = Path(rel_path).name
                for account_dir in WECHAT_BASE.iterdir():
                    if not account_dir.is_dir():
                        continue
                    for db_file in account_dir.rglob(fname):
                        src_found = db_file
                        break
                    if src_found:
                        break

            # 方式3：把 rel_path 当绝对路径
            if not src_found:
                p = Path(rel_path)
                if p.is_absolute() and p.exists():
                    src_found = p

            if not src_found:
                print(f"  ⚠️  找不到数据库文件: {rel_path}，跳过")
                continue

            dst_path = build_output_path(src_found)
            try:
                rel_display = dst_path.relative_to(OUTPUT_DIR)
            except ValueError:
                rel_display = dst_path
            print(f"  解密: {src_found.name} -> {rel_display}")

            try:
                ok = decrypt_database(src_found, key_hex, dst_path, keys)
            except Exception as e:
                import traceback
                print(f"  ❌ 异常: {e}")
                traceback.print_exc()
                fail_count += 1
                continue

            if ok:
                print(f"  ✅ 成功")
                success_count += 1
            else:
                fail_count += 1

    else:
        # keys 是 key_0, key_1 格式，用单一密钥尝试所有 db
        # 取第一个密钥尝试（通常所有 db 用同一密钥）
        primary_key = list(keys.values())[0]
        print(f"  使用密钥: {list(keys.keys())[0]}")
        print()

        all_dbs = find_db_for_key("")
        if not all_dbs:
            print("❌ 找不到微信数据库文件")
            sys.exit(1)

        print(f"  找到 {len(all_dbs)} 个数据库文件")
        print()

        for src_path in all_dbs:
            dst_path = build_output_path(src_path)
            rel = src_path.name
            print(f"  解密: {rel}", end=" ... ", flush=True)

            # 对每个 db 尝试所有密钥
            decrypted = False
            for key_label, key_hex in keys.items():
                if decrypt_database(src_path, key_hex, dst_path, keys):
                    print(f"✅ ({key_label})")
                    success_count += 1
                    decrypted = True
                    break

            if not decrypted:
                print("❌ 所有密钥均失败")
                fail_count += 1

    print()
    print(f"{'='*60}")
    print(f"解密完成: ✅ {success_count} 个成功，❌ {fail_count} 个失败")

    if success_count > 0:
        print(f"\n解密数据库位于: {OUTPUT_DIR}")
        print("\n下一步：")
        print("  1. 在 ~/.kiro/settings/mcp.json 中配置 MCP Server")
        print("  2. 或直接运行: uv run python -m src.server")
    else:
        print("\n❌ 所有解密均失败，请检查：")
        print("  1. keys.json 中的密钥是否正确")
        print("  2. 重新运行 extract_key.py 获取新密钥")


if __name__ == "__main__":
    main()

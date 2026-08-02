#!/usr/bin/env python3
"""
针对微信 4.x Mac 版的数据库解密脚本
数据库路径：~/Library/Containers/com.tencent.xinWeChat/Data/Documents/
            xwechat_files/{wxid}/db_storage/
"""
import hashlib
import json
import os
import re
import sqlite3
import sys
from pathlib import Path

try:
    from Crypto.Cipher import AES
except ImportError:
    print("❌ 缺少依赖，请运行：uv sync")
    sys.exit(1)

PROJECT_ROOT = Path(__file__).parent.parent
KEYS_FILE = PROJECT_ROOT / "keys.json"
OUTPUT_DIR = PROJECT_ROOT / "decrypted"

def _get_db_base() -> Path:
    """同时支持 Mac 和 Windows 的 xwechat_files 路径"""
    mac_path = Path(
        "~/Library/Containers/com.tencent.xinWeChat/Data/Documents/xwechat_files"
    ).expanduser()
    if mac_path.exists():
        return mac_path
    try:
        import winreg
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, r"Software\Tencent\WeChat") as key:
            val, _ = winreg.QueryValueEx(key, "FileSavePath")
            if val and val != "MyDocument:":
                c = Path(val) / "xwechat_files"
                if c.exists():
                    return c
    except Exception:
        pass
    return Path.home() / "Documents" / "xwechat_files"

DB_BASE = _get_db_base()

# SQLCipher 4 参数
PAGE_SIZE = 4096
SALT_SIZE = 16
IV_SIZE = 16
HMAC_SIZE = 64
RESERVED_SIZE = IV_SIZE + HMAC_SIZE
PBKDF2_ITER = 256000
PBKDF2_HASH = "sha512"


def derive_key(passphrase: bytes, salt: bytes) -> bytes:
    derived = hashlib.pbkdf2_hmac(PBKDF2_HASH, passphrase, salt, PBKDF2_ITER, dklen=64)
    return derived[:32]


def try_decrypt(src: Path, passphrase: bytes, dst: Path) -> bool:
    """尝试用指定密钥解密数据库，成功返回 True"""
    try:
        with open(src, "rb") as f:
            raw = f.read()
    except OSError:
        return False

    if len(raw) < PAGE_SIZE:
        return False

    salt = raw[:SALT_SIZE]
    enc_key = derive_key(passphrase, salt)

    # 验证第一页
    first_page = raw[SALT_SIZE:PAGE_SIZE]
    payload_size = PAGE_SIZE - SALT_SIZE - RESERVED_SIZE
    payload = first_page[:payload_size]
    reserved = first_page[payload_size:]
    iv = reserved[:IV_SIZE]

    try:
        cipher = AES.new(enc_key, AES.MODE_CBC, iv)
        decrypted_payload = cipher.decrypt(payload)
    except Exception:
        return False

    if not decrypted_payload.startswith(b"SQLite format 3\x00"):
        return False

    # 解密所有页
    dst.parent.mkdir(parents=True, exist_ok=True)
    total_pages = len(raw) // PAGE_SIZE

    with open(dst, "wb") as out:
        # 第一页
        out.write(decrypted_payload + reserved)

        # 后续页
        for page_no in range(1, total_pages):
            offset = page_no * PAGE_SIZE
            page_data = raw[offset: offset + PAGE_SIZE]
            if len(page_data) < RESERVED_SIZE:
                out.write(page_data)
                continue

            p_size = PAGE_SIZE - RESERVED_SIZE
            p_reserved = page_data[p_size:]
            p_payload = page_data[:p_size]
            p_iv = p_reserved[:IV_SIZE]

            try:
                cipher = AES.new(enc_key, AES.MODE_CBC, p_iv)
                out.write(cipher.decrypt(p_payload) + p_reserved)
            except Exception:
                out.write(page_data)

    # 验证
    try:
        conn = sqlite3.connect(str(dst))
        conn.execute("SELECT count(*) FROM sqlite_master").fetchone()
        conn.close()
        return True
    except Exception:
        dst.unlink(missing_ok=True)
        return False


def main():
    print("=" * 60)
    print("  微信 4.x Mac 数据库解密")
    print("=" * 60)

    if not KEYS_FILE.exists():
        print(f"❌ 找不到 keys.json: {KEYS_FILE}")
        sys.exit(1)

    with open(KEYS_FILE) as f:
        keys: dict = json.load(f)

    print(f"加载密钥：{len(keys)} 个")

    # 解析密钥为字节列表（keys.json 格式：x'<64hex_key><32hex_salt>'）
    passphrases = []
    for k, v in keys.items():
        if isinstance(v, str):
            m = re.match(r"x'([0-9a-fA-F]+)'", v)
            if m:
                passphrases.append(bytes.fromhex(m.group(1)[:64]))
    # 去重
    passphrases = list(dict.fromkeys(passphrases))

    print(f"有效密钥：{len(passphrases)} 个\n")

    # 找到微信数据库目录
    if not DB_BASE.exists():
        print(f"❌ 找不到微信数据目录: {DB_BASE}")
        sys.exit(1)

    # 找所有 wxid 账号目录
    wxid_dirs = [d for d in DB_BASE.iterdir() if d.is_dir() and (d / "db_storage").exists()]
    if not wxid_dirs:
        print(f"❌ 找不到账号目录")
        sys.exit(1)

    print(f"找到账号目录：{[d.name for d in wxid_dirs]}\n")

    total_ok = 0
    total_fail = 0

    for wxid_dir in wxid_dirs:
        db_storage = wxid_dir / "db_storage"
        print(f"📂 账号: {wxid_dir.name}")

        # 找所有 db 文件（跳过 fts 全文索引）
        db_files = [
            p for p in db_storage.rglob("*.db")
            if "fts" not in p.name and "biz_message" not in p.name
        ]

        for src in sorted(db_files):
            rel     = src.relative_to(db_storage)
            rel_key = str(rel).replace("\\", "/")
            dst     = OUTPUT_DIR / rel
            print(f"  解密: {rel}", end=" ... ", flush=True)

            # keys.json 的 key 格式：xwechat_files/<wxid>/db_storage/<rel>
            # 找到该文件对应的专属 passphrase
            dedicated: bytes | None = None
            for k, v in keys.items():
                if k.endswith(rel_key):
                    m = re.match(r"x'([0-9a-fA-F]+)'", v)
                    if m:
                        dedicated = bytes.fromhex(m.group(1)[:64])
                    break

            # 优先用专属密钥，找不到再遍历所有
            candidates = [dedicated] if dedicated else passphrases

            success = False
            for pp in candidates:
                if try_decrypt(src, pp, dst):
                    success = True
                    break

            if success:
                print("✅")
                total_ok += 1
            else:
                print("❌")
                total_fail += 1

    print(f"\n{'='*60}")
    print(f"完成: ✅ {total_ok} 个成功  ❌ {total_fail} 个失败")

    if total_ok > 0:
        print(f"\n解密文件位于: {OUTPUT_DIR}")

        # 更新 config.py 中的路径指向新的数据库结构
        print("\n提示：message 目录下的文件名与旧版不同（message_N.db）")
        print("MCP Server 会自动适配，无需额外配置。")
    else:
        print("\n❌ 所有解密失败，请重新运行 wxecho keys 提取密钥")


if __name__ == "__main__":
    main()

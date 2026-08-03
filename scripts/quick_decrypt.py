#!/usr/bin/env python3
"""
快速解密脚本：直接用 keys.json 里已有的密钥解密，不需要管理员权限，不扫描内存。
适用于密钥已知、只需要更新解密文件的场景。
"""
import hashlib
import hmac
import json
import re
import sqlite3
import struct
import sys
from pathlib import Path

try:
    from Crypto.Cipher import AES
except ImportError:
    print("❌ 缺少 pycryptodome，请运行：uv sync")
    sys.exit(1)

PROJECT_ROOT = Path(__file__).parent.parent
KEYS_FILE    = PROJECT_ROOT / "keys.json"
OUTPUT_DIR   = PROJECT_ROOT / "decrypted"

PAGE_SIZE    = 4096
SALT_SIZE    = 16
KEY_SIZE     = 32
IV_SIZE      = 16
HMAC_SIZE    = 64
RESERVE_SIZE = IV_SIZE + HMAC_SIZE
PBKDF2_ITER  = 256000
SQLITE_MAGIC = b"SQLite format 3\x00"


def decrypt_file(src: Path, key_hex: str, dst: Path) -> bool:
    key_bytes = bytes.fromhex(key_hex)
    data = src.read_bytes()

    if len(data) < PAGE_SIZE:
        return False
    if data.startswith(SQLITE_MAGIC):
        dst.parent.mkdir(parents=True, exist_ok=True)
        dst.write_bytes(data)
        return True

    salt     = data[:SALT_SIZE]
    mac_salt = bytes(b ^ 0x3A for b in salt)
    page1    = data[:PAGE_SIZE]
    data_end = PAGE_SIZE - RESERVE_SIZE + IV_SIZE
    stored   = page1[PAGE_SIZE - HMAC_SIZE: PAGE_SIZE]

    # 试 raw enc_key
    mac_key = hashlib.pbkdf2_hmac("sha512", key_bytes, mac_salt, 2, dklen=KEY_SIZE)
    h = hmac.new(mac_key, page1[SALT_SIZE: data_end], hashlib.sha512)
    h.update(struct.pack("<I", 1))
    if hmac.compare_digest(h.digest(), stored):
        enc_key = key_bytes
    else:
        # passphrase 方式
        enc_key  = hashlib.pbkdf2_hmac("sha512", key_bytes, salt, PBKDF2_ITER, dklen=KEY_SIZE)
        mac_key2 = hashlib.pbkdf2_hmac("sha512", enc_key, mac_salt, 2, dklen=KEY_SIZE)
        h2 = hmac.new(mac_key2, page1[SALT_SIZE: data_end], hashlib.sha512)
        h2.update(struct.pack("<I", 1))
        if not hmac.compare_digest(h2.digest(), stored):
            return False

    dst.parent.mkdir(parents=True, exist_ok=True)
    total_pages = len(data) // PAGE_SIZE

    with open(dst, "wb") as out:
        for page_no in range(total_pages):
            page  = data[page_no * PAGE_SIZE: (page_no + 1) * PAGE_SIZE]
            offset = SALT_SIZE if page_no == 0 else 0
            iv    = page[PAGE_SIZE - RESERVE_SIZE: PAGE_SIZE - RESERVE_SIZE + IV_SIZE]
            enc   = page[offset: PAGE_SIZE - RESERVE_SIZE]
            dec   = AES.new(enc_key, AES.MODE_CBC, iv).decrypt(enc)
            if page_no == 0:
                out.write(SQLITE_MAGIC)
                out.write(dec)
            else:
                out.write(dec)
            out.write(b"\x00" * RESERVE_SIZE)

    try:
        conn = sqlite3.connect(f"file:{dst}?mode=ro&immutable=1", uri=True)
        conn.execute("SELECT count(*) FROM sqlite_master").fetchone()
        conn.close()
        return True
    except Exception:
        try:
            dst.unlink(missing_ok=True)
        except Exception:
            pass
        return False


def main():
    if not KEYS_FILE.exists():
        print(f"❌ 找不到 {KEYS_FILE}，请先运行 decrypt_standalone.py 生成密钥")
        sys.exit(1)

    with open(KEYS_FILE, encoding="utf-8") as f:
        keys = json.load(f)

    print(f"使用 keys.json（{len(keys)} 条密钥）")
    print(f"输出目录: {OUTPUT_DIR}\n")

    # 找 xwechat_files
    xwechat_base = Path.home() / "Documents" / "xwechat_files"
    if not xwechat_base.exists():
        print(f"❌ 找不到 xwechat_files: {xwechat_base}")
        sys.exit(1)

    wxid_dirs = sorted(
        [d for d in xwechat_base.iterdir() if d.is_dir() and (d / "db_storage").exists()],
        key=lambda p: p.stat().st_mtime, reverse=True
    )
    if not wxid_dirs:
        print("❌ 找不到账号目录")
        sys.exit(1)

    db_storage = wxid_dirs[0] / "db_storage"
    print(f"账号目录: {wxid_dirs[0].name}")

    dbs = [
        p for p in db_storage.rglob("*.db")
        if p.stat().st_size >= PAGE_SIZE
        and p.name not in {"key_info.db"}
        and "fts" not in p.name.lower()
    ]
    print(f"找到 {len(dbs)} 个数据库\n")

    success = fail = 0
    for src in sorted(dbs):
        rel = src.relative_to(db_storage)
        dst = OUTPUT_DIR / rel

        # 找对应密钥 - 所有 db 用同一个 passphrase（key 前64位），只是 salt 不同
        # 直接取第一个 key 的前64位作为 passphrase
        key_hex = None
        for v in keys.values():
            m = re.match(r"x'([0-9a-fA-F]{64})", str(v))
            if m:
                key_hex = m.group(1)
                break

        if not key_hex:
            print(f"  ⚠️  {rel}: 没有对应密钥，跳过")
            continue

        size_kb = src.stat().st_size // 1024
        print(f"  {str(rel):45s} ({size_kb:>6} KB) ...", end=" ", flush=True)
        ok = decrypt_file(src, key_hex, dst)
        print("✅" if ok else "❌")
        if ok:
            success += 1
        else:
            fail += 1

    print(f"\n完成：✅{success}  ❌{fail}")
    print("\n重启 MCP Server 即可读取最新数据")


if __name__ == "__main__":
    main()

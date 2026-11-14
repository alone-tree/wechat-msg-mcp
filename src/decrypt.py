"""页级解密与增量刷新。

从 scripts/quick_decrypt.py 抽出可复用实现，供服务进程直接调用：
解到临时文件、校验可读、原子替换，失败不动既有副本。
依据 docs/重构方案-2026-11-13.md 第 19 节。
"""
from __future__ import annotations

import hashlib
import hmac
import json
import os
import re
import sqlite3
import struct
import time
from pathlib import Path

from Crypto.Cipher import AES

PAGE_SIZE = 4096
SALT_SIZE = 16
KEY_SIZE = 32
IV_SIZE = 16
HMAC_SIZE = 64
RESERVE_SIZE = IV_SIZE + HMAC_SIZE
PBKDF2_ITER = 256000
SQLITE_MAGIC = b"SQLite format 3\x00"

SKIP_NAMES = {"key_info.db"}


def load_key_hex(keys_file: Path) -> str:
    """所有库共用同一 passphrase，取 keys.json 里第一条有效密钥的前 64 位。"""
    with open(keys_file, encoding="utf-8") as f:
        keys = json.load(f)
    for v in keys.values():
        m = re.match(r"x'([0-9a-fA-F]{64})", str(v))
        if m:
            return m.group(1)
    raise ValueError(f"keys.json 里没有可用密钥: {keys_file}")


def list_source_dbs(db_storage: Path) -> list[Path]:
    """与 scripts/quick_decrypt.py 保持同一筛选口径。"""
    return [
        p
        for p in db_storage.rglob("*.db")
        if p.stat().st_size >= PAGE_SIZE
        and p.name not in SKIP_NAMES
        and "fts" not in p.name.lower()
    ]


def decrypt_to(src: Path, key_hex: str, dst: Path) -> bool:
    """解密单库，写入 dst。返回是否成功；失败不删除 dst 的既有内容。"""
    key_bytes = bytes.fromhex(key_hex)
    data = src.read_bytes()
    if len(data) < PAGE_SIZE:
        return False

    if data.startswith(SQLITE_MAGIC):
        payload = data
    else:
        salt = data[:SALT_SIZE]
        mac_salt = bytes(b ^ 0x3A for b in salt)
        page1 = data[:PAGE_SIZE]
        data_end = PAGE_SIZE - RESERVE_SIZE + IV_SIZE
        stored = page1[PAGE_SIZE - HMAC_SIZE: PAGE_SIZE]

        mac_key = hashlib.pbkdf2_hmac("sha512", key_bytes, mac_salt, 2, dklen=KEY_SIZE)
        h = hmac.new(mac_key, page1[SALT_SIZE:data_end], hashlib.sha512)
        h.update(struct.pack("<I", 1))
        if hmac.compare_digest(h.digest(), stored):
            enc_key = key_bytes
        else:
            enc_key = hashlib.pbkdf2_hmac("sha512", key_bytes, salt, PBKDF2_ITER, dklen=KEY_SIZE)
            mac_key2 = hashlib.pbkdf2_hmac("sha512", enc_key, mac_salt, 2, dklen=KEY_SIZE)
            h2 = hmac.new(mac_key2, page1[SALT_SIZE:data_end], hashlib.sha512)
            h2.update(struct.pack("<I", 1))
            if not hmac.compare_digest(h2.digest(), stored):
                return False

        total_pages = len(data) // PAGE_SIZE
        chunks = []
        for page_no in range(total_pages):
            page = data[page_no * PAGE_SIZE:(page_no + 1) * PAGE_SIZE]
            offset = SALT_SIZE if page_no == 0 else 0
            iv = page[PAGE_SIZE - RESERVE_SIZE:PAGE_SIZE - RESERVE_SIZE + IV_SIZE]
            enc = page[offset:PAGE_SIZE - RESERVE_SIZE]
            dec = AES.new(enc_key, AES.MODE_CBC, iv).decrypt(enc)
            if page_no == 0:
                chunks.append(SQLITE_MAGIC)
            chunks.append(dec)
            chunks.append(b"\x00" * RESERVE_SIZE)
        payload = b"".join(chunks)

    dst.parent.mkdir(parents=True, exist_ok=True)
    tmp = dst.with_name(dst.name + ".tmp")
    tmp.write_bytes(payload)

    try:
        conn = sqlite3.connect(f"file:{tmp.as_posix()}?mode=ro&immutable=1", uri=True)
        conn.execute("SELECT count(*) FROM sqlite_master").fetchone()
        conn.close()
    except Exception:
        tmp.unlink(missing_ok=True)
        return False

    for attempt in range(3):
        try:
            os.replace(tmp, dst)
            return True
        except PermissionError:
            if attempt == 2:
                tmp.unlink(missing_ok=True)
                return False
            time.sleep(0.1)
    return False


def refresh(
    db_storage: Path,
    decrypted_root: Path,
    keys_file: Path,
    only_changed: bool = True,
) -> dict:
    """解密到 decrypted_root。only_changed 为真时只处理源库比副本新的。"""
    t0 = time.time()
    key_hex = load_key_hex(keys_file)
    dbs = list_source_dbs(db_storage)

    changed: list[str] = []
    failed: list[str] = []
    checked = 0
    for src in sorted(dbs):
        rel = src.relative_to(db_storage)
        dst = decrypted_root / rel
        checked += 1
        try:
            src_stamp = src.stat().st_mtime
        except OSError:
            continue
        if only_changed and dst.exists():
            try:
                if src_stamp <= dst.stat().st_mtime:
                    continue
            except OSError:
                pass
        if decrypt_to(src, key_hex, dst):
            # 副本 mtime 对齐源库，既表达数据版本，也让下次比较判断更准：
            # 解密期间源库再变时，下一次仍然会被识别为需要重解。
            try:
                os.utime(dst, (src_stamp, src_stamp))
            except OSError:
                pass
            changed.append(str(rel).replace("\\", "/"))
        else:
            failed.append(str(rel).replace("\\", "/"))

    return {
        "ok": not failed,
        "checked": checked,
        "changed": changed,
        "failed": failed,
        "seconds": round(time.time() - t0, 2),
        "root": str(decrypted_root),
    }

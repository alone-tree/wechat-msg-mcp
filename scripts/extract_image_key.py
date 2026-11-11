"""从微信进程内存提取 4.x V2 图片 AES 密钥 + XOR 密钥，保存到 decrypted/media_keys.json

用法（需管理员权限，微信已登录）：
    python scripts/extract_image_key.py

若首次未找到：先在微信里点开 2-3 张图片（大图），再立即重跑本脚本。
"""
import glob
import json
import os
import re
import struct
import subprocess
import sys
import time
from ctypes import wintypes

import ctypes

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
KEYS_OUT = os.path.join(PROJECT_ROOT, "decrypted", "media_keys.json")

# 微信数据根（start.ps1 同源约定；也读环境变量）
WECHAT_FILES_DIR = os.environ.get("WECHAT_FILES_DIR") or "F:\\xwechat_files"

kernel32 = ctypes.windll.kernel32
PROCESS_VM_READ = 0x0010
PROCESS_QUERY_INFORMATION = 0x0400
MEM_COMMIT = 0x1000
PAGE_NOACCESS = 0x01
PAGE_GUARD = 0x100
PAGE_READWRITE = 0x04
PAGE_WRITECOPY = 0x08
PAGE_EXECUTE_READWRITE = 0x40
PAGE_EXECUTE_WRITECOPY = 0x80


class MEMORY_BASIC_INFORMATION(ctypes.Structure):
    _fields_ = [
        ("BaseAddress", ctypes.c_void_p),
        ("AllocationBase", ctypes.c_void_p),
        ("AllocationProtect", wintypes.DWORD),
        ("RegionSize", ctypes.c_size_t),
        ("State", wintypes.DWORD),
        ("Protect", wintypes.DWORD),
        ("Type", wintypes.DWORD),
    ]


RE_KEY32 = re.compile(rb"(?<![a-zA-Z0-9])[a-zA-Z0-9]{32}(?![a-zA-Z0-9])")
RE_KEY16 = re.compile(rb"(?<![a-zA-Z0-9])[a-zA-Z0-9]{16}(?![a-zA-Z0-9])")

try:
    from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

    def ecb_decrypt(key: bytes, block: bytes) -> bytes:
        d = Cipher(algorithms.AES(key), modes.ECB()).decryptor()
        return d.update(block) + d.finalize()
except ImportError:
    from Crypto.Cipher import AES  # type: ignore

    def ecb_decrypt(key: bytes, block: bytes) -> bytes:
        return AES.new(key, AES.MODE_ECB).decrypt(block)


def log(msg: str) -> None:
    print(msg, flush=True)


def get_wechat_pids():
    result = subprocess.run(
        ["tasklist.exe", "/FI", "IMAGENAME eq Weixin.exe", "/FO", "CSV", "/NH"],
        capture_output=True,
        text=True,
    )
    pids = []
    for line in result.stdout.strip().splitlines():
        if "Weixin.exe" in line:
            parts = line.strip('"').split('","')
            if len(parts) >= 2:
                pids.append(int(parts[1]))
    return pids


def find_attach_dirs():
    """只取 keys.json 里登记了密钥的账号（主账号），避免拿别的账号的图做验证。"""
    key_suffixes = []
    keys_file = os.path.join(PROJECT_ROOT, "keys.json")
    try:
        with open(keys_file, encoding="utf-8") as f:
            key_suffixes = [k.replace("\\", "/") for k in json.load(f).keys()]
    except Exception:
        pass
    dirs = []
    for wxid_dir in glob.glob(os.path.join(WECHAT_FILES_DIR, "*")):
        attach = os.path.join(wxid_dir, "msg", "attach")
        if not os.path.isdir(attach):
            continue
        pretty = attach.replace("\\", "/")
        if key_suffixes and not any(
            pretty.split("/xwechat_files/")[-1].startswith(
                s.split("xwechat_files/")[-1].split("/")[0]
            )
            for s in key_suffixes
        ):
            continue
        dirs.append(attach)
    return dirs


def latest_v2_thumbs(attach_dirs, limit=100):
    files = []
    for attach in attach_dirs:
        files.extend(glob.glob(os.path.join(attach, "*", "*", "Img", "*_t.dat")))
    files.sort(key=os.path.getmtime, reverse=True)
    out = []
    for f in files[:limit]:
        try:
            with open(f, "rb") as fp:
                head = fp.read(31)
            if head[:6] == b"\x07\x08V2\x08\x07" and len(head) >= 31:
                out.append((f, head))
        except OSError:
            continue
    return out


def guess_xor_key(attach_dirs):
    """从多张缩略图结尾推 XOR key（JPEG 以 FF D9 结尾）"""
    files = []
    for attach in attach_dirs:
        files.extend(glob.glob(os.path.join(attach, "*", "*", "Img", "*_t.dat")))
    files.sort(key=os.path.getmtime, reverse=True)
    counts = {}
    for f in files[:32]:
        try:
            sz = os.path.getsize(f)
            with open(f, "rb") as fp:
                head = fp.read(6)
                fp.seek(sz - 2)
                tail = fp.read(2)
            if head == b"\x07\x08V2\x08\x07" and len(tail) == 2:
                counts[(tail[0], tail[1])] = counts.get((tail[0], tail[1]), 0) + 1
        except OSError:
            continue
    if not counts:
        return None
    x, y = max(counts, key=counts.get)
    return x ^ 0xFF


def try_key(key_bytes: bytes, ciphertext: bytes):
    try:
        dec = ecb_decrypt(key_bytes, ciphertext)
    except Exception:
        return None
    if dec[:3] == b"\xff\xd8\xff":
        return "JPEG"
    if dec[:4] == b"\x89PNG":
        return "PNG"
    if dec[:4] == b"RIFF":
        return "WEBP"
    if dec[:3] == b"GIF":
        return "GIF"
    return None


def is_rw(protect):
    flags = PAGE_READWRITE | PAGE_WRITECOPY | PAGE_EXECUTE_READWRITE | PAGE_EXECUTE_WRITECOPY
    return (protect & flags) != 0


def scan_pid(pid, ciphertext):
    access = PROCESS_VM_READ | PROCESS_QUERY_INFORMATION
    h = kernel32.OpenProcess(access, False, pid)
    if not h:
        log(f"  无法打开进程 {pid}（需要管理员权限）")
        return None
    try:
        mbi = MEMORY_BASIC_INFORMATION()
        address = 0
        regions = []
        while address < 0x7FFFFFFFFFFF:
            ok = kernel32.VirtualQueryEx(
                h, ctypes.c_void_p(address), ctypes.byref(mbi), ctypes.sizeof(mbi)
            )
            if ok == 0:
                break
            if (
                mbi.State == MEM_COMMIT
                and mbi.Protect != PAGE_NOACCESS
                and (mbi.Protect & PAGE_GUARD) == 0
                and mbi.RegionSize <= 50 * 1024 * 1024
            ):
                regions.append((mbi.BaseAddress, mbi.RegionSize, mbi.Protect))
            nxt = address + mbi.RegionSize
            if nxt <= address:
                break
            address = nxt

        rw = [r for r in regions if is_rw(r[2])]
        other = [r for r in regions if not is_rw(r[2])]
        log(f"  区域: RW {len(rw)} 个 / 其他 {len(other)} 个")

        for phase, region_list in (("RW", rw), ("ALL", other)):
            log(f"  Phase {phase}: 扫描中...")
            t0 = time.time()
            for i, (base, size, _p) in enumerate(region_list):
                if i % 200 == 0 and time.time() - t0 > 180:
                    log("  超过180s，跳过该阶段")
                    break
                buf = ctypes.create_string_buffer(size)
                read = ctypes.c_size_t(0)
                ok = kernel32.ReadProcessMemory(h, ctypes.c_void_p(base), buf, size, ctypes.byref(read))
                if not ok or read.value < 32:
                    continue
                data = buf.raw[: read.value]
                for m in RE_KEY32.finditer(data):
                    kb = m.group()
                    fmt = try_key(kb[:16], ciphertext) or try_key(kb, ciphertext)
                    if fmt:
                        return kb.decode("ascii")
                for m in RE_KEY16.finditer(data):
                    kb = m.group()
                    if try_key(kb, ciphertext):
                        return kb.decode("ascii")
        return None
    finally:
        kernel32.CloseHandle(h)


def main():
    if os.name != "posix":
        try:
            is_admin = ctypes.windll.shell32.IsUserAnAdmin()
        except Exception:
            is_admin = False
        if not is_admin:
            log("警告：未以管理员运行，可能无法读取微信进程内存")

    attach_dirs = find_attach_dirs()
    if not attach_dirs:
        log("未找到 msg/attach 目录：" + WECHAT_FILES_DIR)
        sys.exit(1)
    log(f"attach 目录: {attach_dirs}")

    xor_key = guess_xor_key(attach_dirs)
    log(f"XOR key 推断: 0x{xor_key:02x}" if xor_key is not None else "XOR key 未能推断")

    thumbs = latest_v2_thumbs(attach_dirs)
    if not thumbs:
        log("没有找到 V2 格式的 dat 文件")
        sys.exit(1)

    # 依次尝试多张缩略图的首块密文（兼容个别图非 JPEG 头的情况）
    pids = get_wechat_pids()
    log(f"微信进程: {pids}")
    aes_key = None
    for f, head in thumbs[:3]:
        ciphertext = head[15:31]
        log(f"用 {os.path.basename(f)} 的密文尝试...")
        for pid in pids:
            aes_key = scan_pid(pid, ciphertext)
            if aes_key:
                break
        if aes_key:
            break

    if aes_key:
        payload = {"image_aes_key": aes_key}
        if xor_key is not None:
            payload["image_xor_key"] = xor_key
        os.makedirs(os.path.dirname(KEYS_OUT), exist_ok=True)
        with open(KEYS_OUT, "w", encoding="utf-8") as fp:
            json.dump(payload, fp, indent=2)
        log(f"已保存: {KEYS_OUT}")
        log(f"AES key: {aes_key}")
    else:
        log("未找到图片密钥。请先在微信里点开 2-3 张图片（点开大图），然后立即重跑本脚本。")


if __name__ == "__main__":
    main()

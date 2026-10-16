#!/usr/bin/env python3
"""
微信 Windows 4.x 独立解密工具（不依赖 WeChatDataAnalysis）

流程：
  1. 扫描 Weixin.dll，用特征码提取 internal_db_key（32字节XOR掩码）
  2. 用 YARA 规则 + pymem 扫描微信进程内存，找候选 key
  3. 候选 key XOR internal_db_key，PBKDF2-SHA512 验证 HMAC，确认真实密钥
  4. 批量解密所有数据库到 decrypted/ 目录
  5. MCP Server 直接读取本地解密文件（与 Mac 模式一致）

注意：WAL 合并功能暂时禁用（_read_with_wal 直接返回主 db），
避免损坏部分数据库文件。最新消息依赖微信自动 WAL checkpoint 后再解密。
如需获取最新消息，可在微信里切换聊天窗口触发 checkpoint 后重新运行此脚本，
或直接运行 quick_decrypt.py（无需管理员权限）。

依赖（先安装）：
  uv add yara-python pymem pefile

运行方式（需要管理员权限）：
  uv run python scripts\\decrypt_standalone.py

环境变量：
  WEIXIN_DLL=C:\\path\\to\\Weixin.dll   手动指定 DLL 路径（自动检测失败时使用）
"""
import ctypes
import hashlib
import hmac
import multiprocessing
import os
import re
import sqlite3
import struct
import sys
from pathlib import Path

# 检查依赖
missing = []
try:
    import pymem
except ImportError:
    missing.append("pymem")
try:
    import yara
except ImportError:
    missing.append("yara-python")
try:
    import pefile
except ImportError:
    missing.append("pefile")
try:
    from Crypto.Cipher import AES
except ImportError:
    missing.append("pycryptodome")

if missing:
    print(f"❌ 缺少依赖，请运行：uv add {' '.join(missing)}")
    sys.exit(1)

# ── 常量 ─────────────────────────────────────────────────────────────────────
PAGE_SIZE     = 4096
SALT_SIZE     = 16
KEY_SIZE      = 32
IV_SIZE       = 16
HMAC_SIZE     = 64   # SHA-512
RESERVE_SIZE  = IV_SIZE + HMAC_SIZE   # = 80
PBKDF2_ITER   = 256000
SQLITE_MAGIC  = b"SQLite format 3\x00"

# ── Step 1：扫描 Weixin.dll 提取 internal_db_key ────────────────────────────

def find_weixin_dll() -> Path:
    """自动定位 Weixin.dll，支持多版本安装路径"""
    import string
    import winreg

    registry_keys = [
        (winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\WOW6432Node\Tencent\WeChat"),
        (winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\Tencent\WeChat"),
        (winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\WOW6432Node\Tencent\Weixin"),
        (winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\Tencent\Weixin"),
        (winreg.HKEY_CURRENT_USER, r"SOFTWARE\Tencent\Weixin"),
        (winreg.HKEY_CURRENT_USER, r"SOFTWARE\Tencent\WeChat"),
    ]
    install_dir = None
    for hive, subkey in registry_keys:
        try:
            with winreg.OpenKey(hive, subkey) as k:
                install_dir = Path(winreg.QueryValueEx(k, "InstallPath")[0])
                break
        except Exception:
            continue

    candidates = []
    if install_dir and install_dir.exists():
        # 新版微信安装在 install/<version>/Weixin.dll
        for dll in install_dir.rglob("Weixin.dll"):
            candidates.append(dll)
        for dll in install_dir.glob("Weixin.dll"):
            candidates.append(dll)

    # 常见默认路径：穷举所有盘符（微信 4.x 可自定义安装到任意盘，如 D:\Program Files (x86)\Tencent\Weixin）
    subdir_names = [
        "Program Files/Tencent/Weixin",
        "Program Files (x86)/Tencent/Weixin",
        "Program Files/Tencent/WeChat",
        "Program Files (x86)/Tencent/WeChat",
    ]
    bases = []
    for drive in string.ascii_uppercase:
        for sub in subdir_names:
            bases.append(Path(f"{drive}:/{sub}"))
    bases += [
        Path.home() / "AppData/Local/Tencent/WeChat",
        Path.home() / "AppData/Local/Tencent/Weixin",
    ]
    for base in bases:
        if base.exists():
            for dll in base.rglob("Weixin.dll"):
                candidates.append(dll)

    if not candidates:
        raise FileNotFoundError(
            "找不到 Weixin.dll，请手动指定路径：\n"
            "  WEIXIN_DLL=C:\\path\\to\\Weixin.dll uv run python scripts\\verify_standalone_decrypt.py"
        )

    # 按修改时间排序，取最新
    candidates.sort(key=lambda p: p.stat().st_mtime, reverse=True)
    return candidates[0]


# 特征码：连续4个 mov rdx, <8bytes>，最后接 test rax, rax
DLL_PATTERN = re.compile(
    b"\x48\xBA(.{8})"
    b".{3,8}?"
    b"\x48\xBA(.{8})"
    b".{3,8}?"
    b"\x48\xBA(.{8})"
    b".{3,8}?"
    b"\x48\xBA(.{8})"
    b".{3,8}?"
    b"\x48\x85\xC0",
    re.DOTALL,
)

def scan_dll_for_internal_key(dll_path: Path) -> list[bytes]:
    """扫描 Weixin.dll 的代码段，提取 internal_db_key 候选"""
    print(f"  扫描 DLL: {dll_path.name} ({dll_path.stat().st_size // 1024} KB)")
    
    pe = pefile.PE(str(dll_path), fast_load=True)
    results = []
    
    try:
        with open(dll_path, "rb") as f:
            dll_data = f.read()
        
        CODE_SECTION = 0x20000000
        for section in pe.sections:
            if not (int(section.Characteristics) & CODE_SECTION):
                continue
            offset = int(section.PointerToRawData)
            size   = int(section.SizeOfRawData)
            chunk  = dll_data[offset: offset + size]
            
            idx = 0
            while True:
                pos = chunk.find(b"\x48\xBA", idx)
                if pos == -1:
                    break
                m = DLL_PATTERN.match(chunk[pos: pos + 100])
                if m:
                    key_bytes = m.group(1) + m.group(2) + m.group(3) + m.group(4)
                    if key_bytes not in results:
                        results.append(key_bytes)
                    idx = pos + len(m.group(0))
                else:
                    idx = pos + 1
    finally:
        pe.close()

    print(f"  找到 {len(results)} 个 internal_db_key 候选")
    return results


# ── Step 2：YARA 扫描进程内存找候选 key ──────────────────────────────────────

YARA_RULE = r"""
rule GetKeyAddrStub
{
    strings:
        $a = { ?? ?? ?? ?? ?? ?? 00 00 00 00 00 00 00 00 00 00 20 00 00 00 00 00 00 00 2f 00 00 00 00 00 00 00 }
    condition:
        all of them
}
"""

def _read_proc_mem(handle, addr: int, size: int) -> bytes:
    buf = ctypes.create_string_buffer(size)
    n   = ctypes.c_size_t(0)
    ok  = ctypes.windll.kernel32.ReadProcessMemory(
        handle, ctypes.c_void_p(addr), buf, size, ctypes.byref(n)
    )
    return bytes(buf)[:n.value] if ok else b""


def scan_process_memory(pid: int) -> list[bytes]:
    """用 YARA 规则扫描进程内存，返回 32 字节候选 key 列表"""
    print(f"  扫描进程 PID={pid} 内存（使用 YARA 规则）...")
    
    PROCESS_ALL_ACCESS = 0x1F0FFF
    MEM_COMMIT  = 0x1000
    MEM_PRIVATE = 0x20000
    
    class MBI(ctypes.Structure):
        _fields_ = [
            ("BaseAddress",       ctypes.c_void_p),
            ("AllocationBase",    ctypes.c_void_p),
            ("AllocationProtect", ctypes.c_ulong),
            ("RegionSize",        ctypes.c_size_t),
            ("State",             ctypes.c_ulong),
            ("Protect",           ctypes.c_ulong),
            ("Type",              ctypes.c_ulong),
        ]
    
    handle = ctypes.windll.kernel32.OpenProcess(PROCESS_ALL_ACCESS, False, pid)
    if not handle:
        raise PermissionError(f"无法打开进程 PID={pid}，请以管理员权限运行")
    
    # 枚举私有提交页
    regions = []
    mbi = MBI()
    addr = 0
    while ctypes.windll.kernel32.VirtualQueryEx(
            handle, ctypes.c_void_p(addr), ctypes.byref(mbi), ctypes.sizeof(mbi)):
        if mbi.State == MEM_COMMIT and mbi.Type == MEM_PRIVATE:
            regions.append((mbi.BaseAddress, mbi.RegionSize))
        addr += mbi.RegionSize or 0x1000

    print(f"  找到 {len(regions)} 个私有内存区域，开始 YARA 扫描...")
    
    rules = yara.compile(source=YARA_RULE)
    pre_addresses = []
    
    for base, size in regions:
        data = _read_proc_mem(handle, base, size)
        if not data:
            continue
        matches = rules.match(data=data)
        for match in matches:
            for string in match.strings:
                for instance in string.instances:
                    # 读取该位置存储的指针（8字节小端）
                    offset = instance.offset
                    if offset + 8 <= len(data):
                        ptr = struct.unpack_from("<Q", data, offset)[0]
                        if ptr:
                            pre_addresses.append(ptr)
    
    # 根据地址读取 32 字节候选 key
    candidates = []
    seen = set()
    for ptr in pre_addresses:
        key = _read_proc_mem(handle, ptr, KEY_SIZE)
        if len(key) == KEY_SIZE and key not in seen:
            seen.add(key)
            candidates.append(key)
    
    ctypes.windll.kernel32.CloseHandle(handle)
    print(f"  YARA 扫描完成，找到 {len(candidates)} 个候选 key")
    return candidates


def is_potential_key(k: bytes) -> bool:
    """快速熵过滤：排除低熵（全0、重复字节）和高可打印比例的候选"""
    if len(k) != KEY_SIZE:
        return False
    if len(set(k)) < 15:
        return False
    printable = sum(32 <= b <= 126 for b in k)
    if printable > 24:
        return False
    return True


# ── Step 3：HMAC 验证 ─────────────────────────────────────────────────────────

def verify_key(raw_key: bytes, internal_db_key: bytes, db_first_page: bytes) -> str | None:
    """
    raw_key XOR internal_db_key → passphrase
    PBKDF2-SHA512(passphrase, salt, 256000) → enc_key
    PBKDF2-SHA512(enc_key, mac_salt, 2) → mac_key
    HMAC-SHA512(page1_data) 验证
    返回 64 位 hex 密钥（XOR 后的真实密钥），失败返回 None
    """
    # XOR
    passphrase = bytes(a ^ b for a, b in zip(raw_key, internal_db_key))
    
    salt     = db_first_page[:SALT_SIZE]
    mac_salt = bytes(b ^ 0x3A for b in salt)
    
    enc_key  = hashlib.pbkdf2_hmac("sha512", passphrase, salt, PBKDF2_ITER, dklen=KEY_SIZE)
    mac_key  = hashlib.pbkdf2_hmac("sha512", enc_key, mac_salt, 2, dklen=KEY_SIZE)
    
    # HMAC 覆盖范围：page[SALT_SIZE : PAGE_SIZE - RESERVE_SIZE + IV_SIZE]
    data_end = PAGE_SIZE - RESERVE_SIZE + IV_SIZE
    h = hmac.new(mac_key, db_first_page[SALT_SIZE: data_end], hashlib.sha512)
    h.update(struct.pack("<I", 1))
    expected = h.digest()
    
    stored = db_first_page[PAGE_SIZE - HMAC_SIZE: PAGE_SIZE]
    if hmac.compare_digest(expected, stored):
        return passphrase.hex()
    return None


def find_key(pid: int, db_path: Path, silent: bool = False) -> str | None:
    """
    完整的密钥提取流程。
    
    :param silent: True 时减少输出（用于 Server 内部自动调用）
    """
    def _log(msg: str, end: str = "\n") -> None:
        if not silent:
            print(msg, end=end)

    _log("\nStep 1: 扫描 Weixin.dll 提取 internal_db_key...")
    env_dll = os.environ.get("WEIXIN_DLL", "")
    dll_path = Path(env_dll) if env_dll else find_weixin_dll()
    _log(f"  使用: {dll_path}")
    internal_keys = scan_dll_for_internal_key(dll_path) if not silent else _scan_dll_silent(dll_path)

    _log("\nStep 2: 扫描进程内存找候选 key...")
    raw_candidates = scan_process_memory(pid) if not silent else _scan_memory_silent(pid)
    if raw_candidates is None:
        return None

    # 熵过滤
    filtered = [k for k in raw_candidates if is_potential_key(k)]
    _log(f"  熵过滤后剩余: {len(filtered)} 个候选")

    _log("\nStep 3: HMAC 验证...")
    db_first_page = db_path.read_bytes()[:PAGE_SIZE]

    total = len(internal_keys) * len(filtered)
    checked = 0
    for internal_key in internal_keys:
        for raw_key in filtered:
            checked += 1
            if not silent and checked % 1000 == 0:
                print(f"  验证进度: {checked}/{total}", end="\r")
            result = verify_key(raw_key, internal_key, db_first_page)
            if result:
                _log(f"\n  ✅ 找到有效密钥！（第 {checked} 个候选）")
                return result

    _log(f"\n  验证完成，共 {checked} 个候选，未找到有效密钥")
    return None


def _scan_dll_silent(dll_path: Path) -> list[bytes]:
    """静默版 DLL 扫描，不打印任何输出"""
    try:
        pe = pefile.PE(str(dll_path), fast_load=True)
        results = []
        with open(dll_path, "rb") as f:
            dll_data = f.read()
        CODE_SECTION = 0x20000000
        for section in pe.sections:
            if not (int(section.Characteristics) & CODE_SECTION):
                continue
            offset = int(section.PointerToRawData)
            size   = int(section.SizeOfRawData)
            chunk  = dll_data[offset: offset + size]
            idx = 0
            while True:
                pos = chunk.find(b"\x48\xBA", idx)
                if pos == -1:
                    break
                m = DLL_PATTERN.match(chunk[pos: pos + 100])
                if m:
                    key_bytes = m.group(1) + m.group(2) + m.group(3) + m.group(4)
                    if key_bytes not in results:
                        results.append(key_bytes)
                    idx = pos + len(m.group(0))
                else:
                    idx = pos + 1
        pe.close()
        return results
    except Exception:
        return []


def _scan_memory_silent(pid: int) -> list[bytes] | None:
    """静默版内存扫描，失败时返回 None"""
    try:
        PROCESS_ALL_ACCESS = 0x1F0FFF
        MEM_COMMIT  = 0x1000
        MEM_PRIVATE = 0x20000

        class MBI(ctypes.Structure):
            _fields_ = [
                ("BaseAddress",       ctypes.c_void_p),
                ("AllocationBase",    ctypes.c_void_p),
                ("AllocationProtect", ctypes.c_ulong),
                ("RegionSize",        ctypes.c_size_t),
                ("State",             ctypes.c_ulong),
                ("Protect",           ctypes.c_ulong),
                ("Type",              ctypes.c_ulong),
            ]

        handle = ctypes.windll.kernel32.OpenProcess(PROCESS_ALL_ACCESS, False, pid)
        if not handle:
            return None

        regions = []
        mbi = MBI()
        addr = 0
        while ctypes.windll.kernel32.VirtualQueryEx(
                handle, ctypes.c_void_p(addr), ctypes.byref(mbi), ctypes.sizeof(mbi)):
            if mbi.State == MEM_COMMIT and mbi.Type == MEM_PRIVATE:
                regions.append((mbi.BaseAddress, mbi.RegionSize))
            addr += mbi.RegionSize or 0x1000

        rules = yara.compile(source=YARA_RULE)
        pre_addresses = []
        for base, size in regions:
            data = _read_proc_mem(handle, base, size)
            if not data:
                continue
            matches = rules.match(data=data)
            for match in matches:
                for string in match.strings:
                    for instance in string.instances:
                        offset = instance.offset
                        if offset + 8 <= len(data):
                            ptr = struct.unpack_from("<Q", data, offset)[0]
                            if ptr:
                                pre_addresses.append(ptr)

        candidates = []
        seen = set()
        for ptr in pre_addresses:
            key = _read_proc_mem(handle, ptr, KEY_SIZE)
            if len(key) == KEY_SIZE and key not in seen:
                seen.add(key)
                candidates.append(key)

        ctypes.windll.kernel32.CloseHandle(handle)
        return candidates
    except Exception:
        return None


# ── Step 4：解密数据库 ────────────────────────────────────────────────────────

def _read_with_wal(src: Path, wal_path: Path) -> bytes:
    """
    暂时禁用 WAL 合并——直接返回主 db 数据。
    WAL 合并逻辑会损坏部分数据库，暂时关闭等待修复。
    """
    return src.read_bytes()


def _decrypt_data(data: bytes, key_hex: str, dst: Path) -> bool:
    """直接解密已读取的数据（不合并 WAL）"""
    key_bytes = bytes.fromhex(key_hex)
    if len(data) < PAGE_SIZE:
        return False
    if data.startswith(SQLITE_MAGIC):
        dst.write_bytes(data)
        return True
    salt = data[:SALT_SIZE]
    mac_salt = bytes(b ^ 0x3A for b in salt)
    page1 = data[:PAGE_SIZE]
    data_end = PAGE_SIZE - RESERVE_SIZE + IV_SIZE
    stored_hmac = page1[PAGE_SIZE - HMAC_SIZE: PAGE_SIZE]
    # 试 raw enc_key
    mac_key_try = hashlib.pbkdf2_hmac("sha512", key_bytes, mac_salt, 2, dklen=KEY_SIZE)
    h = hmac.new(mac_key_try, page1[SALT_SIZE: data_end], hashlib.sha512)
    h.update(struct.pack("<I", 1))
    if hmac.compare_digest(h.digest(), stored_hmac):
        enc_key = key_bytes
    else:
        enc_key = hashlib.pbkdf2_hmac("sha512", key_bytes, salt, PBKDF2_ITER, dklen=KEY_SIZE)
        mac_key2 = hashlib.pbkdf2_hmac("sha512", enc_key, mac_salt, 2, dklen=KEY_SIZE)
        h2 = hmac.new(mac_key2, page1[SALT_SIZE: data_end], hashlib.sha512)
        h2.update(struct.pack("<I", 1))
        if not hmac.compare_digest(h2.digest(), stored_hmac):
            return False
    dst.parent.mkdir(parents=True, exist_ok=True)
    total_pages = len(data) // PAGE_SIZE
    with open(dst, "wb") as out:
        for page_no in range(total_pages):
            page = data[page_no * PAGE_SIZE: (page_no + 1) * PAGE_SIZE]
            offset = SALT_SIZE if page_no == 0 else 0
            iv = page[PAGE_SIZE - RESERVE_SIZE: PAGE_SIZE - RESERVE_SIZE + IV_SIZE]
            enc = page[offset: PAGE_SIZE - RESERVE_SIZE]
            from Crypto.Cipher import AES as _AES
            dec = _AES.new(enc_key, _AES.MODE_CBC, iv).decrypt(enc)
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
    except sqlite3.DatabaseError as e:
        try:
            dst.unlink(missing_ok=True)
        except Exception:
            pass
        return False


def decrypt_database(src: Path, key_hex: str, dst: Path) -> bool:
    """用找到的密钥解密数据库，解密前先合并 WAL"""
    key_bytes = bytes.fromhex(key_hex)

    # 如果有 WAL 文件，合并后再解密
    wal_path = Path(str(src) + "-wal")
    if wal_path.exists() and wal_path.stat().st_size > 0:
        data = _read_with_wal(src, wal_path)
    else:
        data = src.read_bytes()

    if len(data) < PAGE_SIZE:
        print(f"  ❌ 文件过小: {len(data)} 字节")
        return False

    # 检查是否已是明文
    if data.startswith(SQLITE_MAGIC):
        print("  文件已是明文 SQLite，无需解密")
        dst.write_bytes(data)
        return True

    salt     = data[:SALT_SIZE]
    mac_salt = bytes(b ^ 0x3A for b in salt)
    page1    = data[:PAGE_SIZE]

    # 自动判断 key 类型（raw enc_key 还是 passphrase）
    # 方式1：直接作为 enc_key
    mac_key_try = hashlib.pbkdf2_hmac("sha512", key_bytes, mac_salt, 2, dklen=KEY_SIZE)
    data_end = PAGE_SIZE - RESERVE_SIZE + IV_SIZE
    h = hmac.new(mac_key_try, page1[SALT_SIZE: data_end], hashlib.sha512)
    h.update(struct.pack("<I", 1))
    stored_hmac = page1[PAGE_SIZE - HMAC_SIZE: PAGE_SIZE]

    if hmac.compare_digest(h.digest(), stored_hmac):
        enc_key = key_bytes
        print("  key 类型: raw enc_key")
    else:
        # 方式2：作为 passphrase，走 PBKDF2-SHA512 256000轮
        enc_key  = hashlib.pbkdf2_hmac("sha512", key_bytes, salt, PBKDF2_ITER, dklen=KEY_SIZE)
        mac_key2 = hashlib.pbkdf2_hmac("sha512", enc_key, mac_salt, 2, dklen=KEY_SIZE)
        h2 = hmac.new(mac_key2, page1[SALT_SIZE: data_end], hashlib.sha512)
        h2.update(struct.pack("<I", 1))
        if hmac.compare_digest(h2.digest(), stored_hmac):
            print("  key 类型: SQLCipher passphrase（PBKDF2-SHA512）")
        else:
            print("  ❌ 密钥验证失败，key 不匹配该数据库")
            return False

    dst.parent.mkdir(parents=True, exist_ok=True)
    total_pages = len(data) // PAGE_SIZE

    with open(dst, "wb") as out:
        for page_no in range(total_pages):
            page   = data[page_no * PAGE_SIZE: (page_no + 1) * PAGE_SIZE]
            offset = SALT_SIZE if page_no == 0 else 0
            iv     = page[PAGE_SIZE - RESERVE_SIZE: PAGE_SIZE - RESERVE_SIZE + IV_SIZE]
            enc    = page[offset: PAGE_SIZE - RESERVE_SIZE]

            from Crypto.Cipher import AES as _AES
            dec = _AES.new(enc_key, _AES.MODE_CBC, iv).decrypt(enc)

            if page_no == 0:
                # 第一页：dec 已经包含 SQLite header（解密原文前16字节就是 header）
                # 输出：dec(4080字节) + 零保留区(80字节) = 4160? 不对
                # 正确：SQLITE_HEADER(16) + dec[16:](4064-16=4064字节) + 零(80) = 4096+80不对
                # 参考 wechat_decrypt.py：encrypted_page = page[SALT_SIZE:PAGE_SIZE-RESERVE_SIZE]
                # dec = decrypt(encrypted_page)，长度=4096-16-80=4000字节
                # 输出 = SQLITE_HEADER + dec + zeros(RESERVE_SIZE) = 16+4000+80 = 4096 ✅
                out.write(SQLITE_MAGIC)   # 16字节
                out.write(dec)            # 4000字节（page[16:4016] 解密结果）
                out.write(b"\x00" * RESERVE_SIZE)  # 80字节
            else:
                # 后续页：enc = page[0:PAGE_SIZE-RESERVE_SIZE] = page[0:4016]，解密后4016字节
                # 输出 = dec(4016) + zeros(80) = 4096 ✅
                out.write(dec)
                out.write(b"\x00" * RESERVE_SIZE)

    # 验证
    try:
        conn = sqlite3.connect(f"file:{dst}?mode=ro&immutable=1", uri=True)
        conn.execute("SELECT count(*) FROM sqlite_master").fetchone()
        conn.close()
        return True
    except sqlite3.DatabaseError as e:
        print(f"  ❌ 解密后验证失败: {e}")
        try:
            dst.unlink(missing_ok=True)
        except Exception:
            pass
        return False


def decrypt_all_databases(key_hex: str, xwechat_base: Path, output_dir: Path) -> tuple[int, int]:
    """
    批量解密 db_storage 下所有数据库，输出到 output_dir。
    message_0.db 合并 WAL，其他数据库直接解密主文件。
    """
    success = fail = 0

    wxid_dirs = sorted(
        [d for d in xwechat_base.iterdir() if d.is_dir() and (d / "db_storage").exists()],
        key=lambda p: p.stat().st_mtime, reverse=True,
    )
    if not wxid_dirs:
        print("❌ 找不到账号目录")
        return 0, 0

    db_storage = wxid_dirs[0] / "db_storage"
    print(f"  账号目录: {wxid_dirs[0].name}")
    print(f"  db_storage: {db_storage}")

    SKIP = {"key_info.db"}
    # 只对 message 类数据库合并 WAL，其他直接解密
    WAL_MERGE_PATTERNS = {"message_0.db", "biz_message_0.db", "media_0.db"}

    dbs = [
        p for p in db_storage.rglob("*.db")
        if p.stat().st_size >= PAGE_SIZE
        and p.name not in SKIP
        and "fts" not in p.name.lower()
    ]
    print(f"  找到 {len(dbs)} 个数据库文件\n")

    for src in sorted(dbs):
        rel = src.relative_to(db_storage)
        dst = output_dir / rel
        size_kb = src.stat().st_size // 1024
        print(f"  解密: {str(rel):45s} ({size_kb:>6} KB) ...", end=" ", flush=True)

        # 只对 message 类数据库合并 WAL
        wal_path = Path(str(src) + "-wal")
        if src.name in WAL_MERGE_PATTERNS and wal_path.exists() and wal_path.stat().st_size > 0:
            ok = decrypt_database(src, key_hex, dst)  # _read_with_wal 在 decrypt_database 里
        else:
            # 其他数据库直接读主文件，不合并 WAL
            data = src.read_bytes()
            dst.parent.mkdir(parents=True, exist_ok=True)
            ok = _decrypt_data(data, key_hex, dst)

        if ok:
            print("✅")
            success += 1
        else:
            print("❌")
            fail += 1

    return success, fail


# ── 主流程 ────────────────────────────────────────────────────────────────────

def main():
    print("=" * 60)
    print("  微信 Windows 独立解密工具")
    print("=" * 60)

    if sys.platform != "win32":
        print("❌ 本脚本仅支持 Windows")
        sys.exit(1)

    # 权限检查
    try:
        is_admin = bool(ctypes.windll.shell32.IsUserAnAdmin())
    except Exception:
        is_admin = False

    if not is_admin:
        print("⚠️  建议以管理员权限运行（内存扫描需要）")
    else:
        print("✅ 管理员权限")

    # 找微信进程
    try:
        pm = pymem.Pymem("Weixin.exe")
        pid = pm.process_id
        print(f"✅ 微信进程 PID={pid}")
    except Exception:
        try:
            pm = pymem.Pymem("WeChat.exe")
            pid = pm.process_id
            print(f"✅ 微信进程 PID={pid}")
        except Exception:
            print("❌ 未找到微信进程，请确保微信已登录运行")
            sys.exit(1)

    # 找 xwechat_files，优先读环境变量 WECHAT_FILES_DIR
    env_path = os.environ.get("WECHAT_FILES_DIR", "").strip()
    if env_path and Path(env_path).exists():
        xwechat_base = Path(env_path)
    else:
        # 从注册表读取
        xwechat_base = None
        try:
            import winreg
            with winreg.OpenKey(winreg.HKEY_CURRENT_USER, r"Software\Tencent\WeChat") as key:
                val, _ = winreg.QueryValueEx(key, "FileSavePath")
                if val and val != "MyDocument:":
                    candidate = Path(val) / "xwechat_files"
                    if candidate.exists():
                        xwechat_base = candidate
        except Exception:
            pass
        if xwechat_base is None:
            xwechat_base = Path.home() / "Documents" / "xwechat_files"

    if not xwechat_base.exists():
        print(f"❌ 找不到 xwechat_files: {xwechat_base}")
        print("   请设置环境变量 WECHAT_FILES_DIR 指定路径")
        sys.exit(1)
    print(f"✅ xwechat_files: {xwechat_base}")

    # 选验证用的数据库（小文件优先，减少验证时间）
    db_for_verify = None
    for wxid_dir in sorted(xwechat_base.iterdir(),
                           key=lambda p: p.stat().st_mtime, reverse=True):
        if not wxid_dir.is_dir():
            continue
        db_storage = wxid_dir / "db_storage"
        if not db_storage.exists():
            continue
        for name in ["favorite.db", "sns.db", "session.db", "contact.db"]:
            for p in db_storage.rglob(name):
                if p.stat().st_size >= PAGE_SIZE:
                    db_for_verify = p
                    break
            if db_for_verify:
                break
        if db_for_verify:
            break

    if not db_for_verify:
        print("❌ 找不到验证数据库")
        sys.exit(1)

    print(f"✅ 验证数据库: {db_for_verify.name} ({db_for_verify.stat().st_size // 1024} KB)")

    # 提取密钥
    key_hex = find_key(pid, db_for_verify)

    if not key_hex:
        print()
        print("❌ 密钥提取失败")
        print("  请以管理员权限重试，或检查 Weixin.dll 路径是否正确")
        sys.exit(1)

    print()
    print(f"🎉 密钥提取成功: {key_hex[:8]}...{key_hex[-8:]}")

    # 保存密钥到 keys.json（方便下次直接解密，不需要重新扫描内存）
    project_root = Path(__file__).parent.parent
    keys_file = project_root / "keys.json"
    import json
    # 读取 db_storage 里所有 db 的 salt，生成标准格式的 keys.json
    db_storage_root = db_for_verify.parent.parent
    keys_data = {}
    for db in db_storage_root.rglob("*.db"):
        if db.stat().st_size < PAGE_SIZE:
            continue
        try:
            salt_hex = db.read_bytes()[:SALT_SIZE].hex()
            rel = str(db.relative_to(xwechat_base.parent)).replace("\\", "/")
            keys_data[rel] = f"x'{key_hex}{salt_hex}'"
        except Exception:
            pass

    with open(keys_file, "w", encoding="utf-8") as f:
        json.dump(keys_data, f, indent=2, ensure_ascii=False)
    print(f"  密钥已保存到 keys.json（{len(keys_data)} 条）")

    # 批量解密所有数据库
    # 与 start.ps1 / quick_decrypt.py 对齐：优先读 WECHAT_DECRYPTED_DIR 环境变量
    _output_env = os.environ.get("WECHAT_DECRYPTED_DIR", "").strip()
    output_dir = Path(_output_env) if _output_env else (project_root / "decrypted")
    print()
    print(f"开始批量解密 → {output_dir}")
    print()
    success, fail = decrypt_all_databases(key_hex, xwechat_base, output_dir)

    print()
    print("=" * 60)
    print(f"解密完成：✅ {success} 个成功  ❌ {fail} 个失败")

    if success > 0:
        print(f"\n解密数据库位于: {output_dir}")
        print("\n下一步，启动 MCP Server（本地模式，不需要 WeChatDataAnalysis）：")
        print("  uv run python -m src.server --transport streamable-http --port 8765")
    else:
        print("\n❌ 所有解密失败，请检查密钥或以管理员权限重试")
        sys.exit(1)


if __name__ == "__main__":
    main()

if __name__ == "__main__":
    multiprocessing.freeze_support()
    main()

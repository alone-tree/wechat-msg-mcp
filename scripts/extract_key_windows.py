#!/usr/bin/env python3
"""
微信 Windows 4.x 数据库密钥提取工具

适用版本：微信 Windows 4.x（xwechat_files 目录结构）
加密格式：与 Mac 新版完全相同——32 字节 enc_key 直接 AES-CBC，不经过 PBKDF2

原理：
  微信 Windows 4.x 将 32 字节 AES 密钥存储在 WeChatWin.dll 对应的进程内存中。
  本脚本通过 ReadProcessMemory 枚举内存区域，收集 32 字节候选，
  然后用数据库 salt + AES 解密首页来验证（无需 PBKDF2，验证极快）。

生成文件：
  keys.json —— 格式与 Mac 版完全相同，可直接用 decrypt_db.py 解密

使用方法：
  以管理员权限运行：
    uv run python scripts\\extract_key_windows.py

依赖：pip install pycryptodome（已含于 uv sync）
"""
import ctypes
import ctypes.wintypes
import json
import sys
from pathlib import Path

# 确保输出立即刷新，避免"无输出"假象
import functools
print = functools.partial(print, flush=True)

PROJECT_ROOT = Path(__file__).parent.parent
KEYS_FILE    = PROJECT_ROOT / "keys.json"   # 与 Mac 版共用同一个 keys.json

# ── Windows API 常量 ─────────────────────────────────────────────────────────
PROCESS_QUERY_INFORMATION = 0x0400
PROCESS_VM_READ           = 0x0010
MEM_COMMIT                = 0x1000
PAGE_READWRITE            = 0x04
PAGE_READONLY             = 0x02
PAGE_WRITECOPY            = 0x08
PAGE_EXECUTE_READ         = 0x20
PAGE_EXECUTE_READWRITE    = 0x40
PAGE_EXECUTE_WRITECOPY    = 0x80
READABLE_PAGES = {PAGE_READWRITE, PAGE_READONLY, PAGE_WRITECOPY,
                  PAGE_EXECUTE_READ, PAGE_EXECUTE_READWRITE, PAGE_EXECUTE_WRITECOPY}


class MEMORY_BASIC_INFORMATION64(ctypes.Structure):
    _fields_ = [
        ("BaseAddress",       ctypes.c_ulonglong),
        ("AllocationBase",    ctypes.c_ulonglong),
        ("AllocationProtect", ctypes.wintypes.DWORD),
        ("__alignment1",      ctypes.wintypes.DWORD),
        ("RegionSize",        ctypes.c_ulonglong),
        ("State",             ctypes.wintypes.DWORD),
        ("Protect",           ctypes.wintypes.DWORD),
        ("Type",              ctypes.wintypes.DWORD),
        ("__alignment2",      ctypes.wintypes.DWORD),
    ]


# ── 工具函数 ─────────────────────────────────────────────────────────────────

def get_wechat_pid() -> int | None:
    """获取微信进程 PID，兼容新版 Weixin.exe 和旧版 WeChat.exe"""
    import subprocess
    # 微信 4.x Windows 版进程名为 Weixin.exe，旧版为 WeChat.exe
    for proc_name in ("Weixin.exe", "WeChat.exe"):
        try:
            out = subprocess.check_output(
                ["tasklist", "/FI", f"IMAGENAME eq {proc_name}", "/FO", "CSV", "/NH"],
                text=True, encoding="gbk", errors="replace"
            )
            for line in out.strip().splitlines():
                parts = line.strip('"').split('","')
                if len(parts) >= 2 and parts[0].lower() == proc_name.lower():
                    pid = int(parts[1])
                    print(f"  进程名: {proc_name}  PID={pid}")
                    return pid
        except Exception as e:
            print(f"  [tasklist 查询 {proc_name} 失败: {e}]")
    return None


def get_xwechat_base() -> Path:
    """找到 Windows 版 xwechat_files 根目录"""
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
    return Path.home() / "Documents" / "xwechat_files"


def find_db_files(xwechat_base: Path) -> list[Path]:
    """
    找到用于密钥验证的数据库文件（优先 contact.db，其次 message_0.db）。
    只需要找一个能做 salt 匹配验证的文件。
    """
    dbs: list[Path] = []
    if not xwechat_base.exists():
        return dbs
    for wxid_dir in sorted(xwechat_base.iterdir(),
                           key=lambda p: p.stat().st_mtime, reverse=True):
        if not wxid_dir.is_dir():
            continue
        db_storage = wxid_dir / "db_storage"
        if not db_storage.exists():
            continue
        # 优先选直接 AES key 格式的文件验证（message/session 前16字节可打印字节多）
        for pattern in [
            "message/message_0.db",
            "session/session.db",
            "contact/contact.db",
        ]:
            for p in db_storage.glob(pattern):
                if p.stat().st_size >= 4096:
                    dbs.append(p)
        if dbs:
            break  # 只用最近账号
    return dbs


# ── 密钥验证（新版直接 key 格式，无 PBKDF2）────────────────────────────────────

def verify_direct_key(key32: bytes, db_path: Path) -> bool:
    """
    新版微信（xwechat_files）加密格式验证：
      enc_key 直接是 32 字节 AES key，页布局：
        Page 1: [salt(16)][encrypted(4000)][iv(16)][hmac(64)]
      解密 encrypted 后检查是否以 'SQLite format 3\\x00' 开头。
    验证速度极快（一次 AES 解密），可大量候选批量跑。
    """
    SQLITE_HDR   = b"SQLite format 3\x00"
    RESERVE_SZ   = 80  # iv(16) + hmac(64)

    try:
        with open(db_path, "rb") as f:
            raw = f.read(4096)
    except OSError:
        return False
    if len(raw) < 4096:
        return False

    try:
        from Crypto.Cipher import AES
        iv  = raw[4096 - RESERVE_SZ : 4096 - RESERVE_SZ + 16]   # raw[4016:4032]
        enc = raw[16 : 4096 - RESERVE_SZ]                         # raw[16:4016]
        dec = AES.new(key32, AES.MODE_CBC, iv).decrypt(enc)
        return dec[:16] == SQLITE_HDR
    except Exception:
        return False


# ── 内存扫描 ─────────────────────────────────────────────────────────────────

def open_process(pid: int) -> int:
    k32    = ctypes.windll.kernel32
    handle = k32.OpenProcess(PROCESS_QUERY_INFORMATION | PROCESS_VM_READ, False, pid)
    if not handle:
        err = ctypes.GetLastError()
        raise PermissionError(f"无法打开进程 PID={pid}，错误码={err}，请以管理员权限运行")
    return handle


def scan_memory_for_key(pid: int, db_path: Path) -> str | None:
    """
    两阶段扫描：
      阶段1：枚举所有可读内存区域，收集 32 字节候选（按 8 字节对齐）
      阶段2：对每个候选做一次 AES 解密验证（极快，无 PBKDF2）
    """
    print(f"  打开进程 PID={pid} ...")
    try:
        handle = open_process(pid)
    except PermissionError as e:
        print(f"  ❌ {e}")
        return None

    k32     = ctypes.windll.kernel32
    mbi     = MEMORY_BASIC_INFORMATION64()
    addr    = 0
    MAX     = 0x7FFFFFFFFFFF
    MAX_REG = 32 * 1024 * 1024

    candidates: list[bytes] = []
    regions    = 0

    print("  阶段1：收集内存候选...")
    try:
        while addr < MAX:
            ret = k32.VirtualQueryEx(handle, ctypes.c_ulonglong(addr),
                                     ctypes.byref(mbi), ctypes.sizeof(mbi))
            if not ret:
                break
            size = mbi.RegionSize or 0x1000

            if (mbi.State == MEM_COMMIT
                    and (mbi.Protect & 0xFF) in READABLE_PAGES
                    and 32 <= size <= MAX_REG):
                buf    = ctypes.create_string_buffer(size)
                n_read = ctypes.c_ulonglong(0)
                ok = k32.ReadProcessMemory(handle, ctypes.c_ulonglong(mbi.BaseAddress),
                                           buf, size, ctypes.byref(n_read))
                if ok and n_read.value >= 32:
                    data = buf.raw[:n_read.value]
                    for off in range(0, len(data) - 31, 8):
                        chunk = data[off: off + 32]
                        if len(set(chunk)) >= 10:   # 快速熵过滤
                            candidates.append(chunk)

            regions += 1
            if regions % 100 == 0:
                print(f"  已扫描 {regions} 个区域，候选 {len(candidates)} 个", end="\r")

            addr = mbi.BaseAddress + max(size, 0x1000)
    finally:
        k32.CloseHandle(handle)

    # 去重
    candidates = list(dict.fromkeys(candidates))
    print(f"  收集完成：{regions} 个区域，去重后 {len(candidates)} 个候选              ")

    if not candidates:
        print("  ❌ 内存读取失败（权限不足？）")
        return None

    # 阶段2：AES 验证（无 PBKDF2，极快）
    print(f"  阶段2：AES 验证 {len(candidates)} 个候选（验证数据库：{db_path.name}）...")
    from Crypto.Cipher import AES
    SQLITE_HDR = b"SQLite format 3\x00"
    RESERVE_SZ = 80

    try:
        with open(db_path, "rb") as f:
            raw = f.read(4096)
    except OSError:
        print(f"  ❌ 无法读取数据库文件: {db_path}")
        return None

    iv  = raw[4096 - RESERVE_SZ : 4096 - RESERVE_SZ + 16]
    enc = raw[16 : 4096 - RESERVE_SZ]

    for i, key32 in enumerate(candidates):
        if i % 10000 == 0 and i > 0:
            print(f"  验证进度: {i}/{len(candidates)} ({i*100//len(candidates)}%)", end="\r")
        try:
            dec = AES.new(key32, AES.MODE_CBC, iv).decrypt(enc)
            if dec[:16] == SQLITE_HDR:
                print(f"\n  ✅ 在第 {i+1} 个候选找到密钥！")
                return key32.hex()
        except Exception:
            continue

    print(f"\n  验证完成，未找到匹配密钥")
    return None


# ── 主流程 ────────────────────────────────────────────────────────────────────

def main():
    print("=" * 60)
    print("  微信 Windows 4.x 密钥提取工具")
    print("=" * 60)
    print()

    if sys.platform != "win32":
        print("❌ 本脚本仅支持 Windows 系统")
        sys.exit(1)

    try:
        from Crypto.Cipher import AES  # noqa: F401
    except ImportError:
        print("❌ 缺少 pycryptodome，请运行：uv sync")
        sys.exit(1)

    # 权限检查
    try:
        is_admin = bool(ctypes.windll.shell32.IsUserAnAdmin())
    except Exception:
        is_admin = False

    if not is_admin:
        print("⚠️  当前无管理员权限")
        print("   建议：右键 PowerShell → 以管理员身份运行，再执行本脚本")
        print()
        ans = input("是否以当前权限继续尝试？(y/N): ").strip().lower()
        if ans != "y":
            sys.exit(0)
    else:
        print("✅ 管理员权限")

    # 查找微信进程
    print()
    print("🔍 查找微信进程...")
    pid = get_wechat_pid()
    if not pid:
        print("❌ 未找到 WeChat.exe，请先登录微信")
        sys.exit(1)
    print(f"✅ WeChat.exe PID={pid}")

    # 查找 xwechat_files 和数据库
    print()
    print("🔍 查找数据库文件...")
    xwechat_base = get_xwechat_base()
    print(f"   xwechat_files: {xwechat_base}")

    db_files = find_db_files(xwechat_base)
    if not db_files:
        print("❌ 找不到数据库文件")
        print(f"   请确认 xwechat_files 路径正确：{xwechat_base}")
        sys.exit(1)

    # 优先选 contact.db（标准 SQLCipher salt，验证更可靠）
    db_for_verify = db_files[0]
    print(f"   验证文件: {db_for_verify}")

    # 扫描内存
    print()
    print("🔑 扫描进程内存提取密钥...")
    print("   预计耗时：30秒~3分钟")
    print()

    key_hex = scan_memory_for_key(pid, db_for_verify)

    if not key_hex:
        print()
        print("❌ 未能提取到密钥")
        print()
        print("替代方案：")
        print("  使用其他工具（如 WeChatMsgDump）获取密钥后，")
        print("  手动创建 keys.json：")
        print('    {"key_0": "x\'<64位hex密钥><数据库salt16字节hex>\'"}')
        print()
        print("  或直接运行解密并指定密钥（需修改 decrypt_db.py）")
        sys.exit(1)

    print()
    print(f"🎉 密钥提取成功：{key_hex[:8]}...{key_hex[-8:]}")

    # 保存为与 Mac 版相同的格式：
    # {"xwechat_files/wxid.../db_storage/contact/contact.db": "x'<key><salt>'"}
    print()
    print("📋 验证所有数据库并生成 keys.json ...")
    result: dict[str, str] = {}

    if xwechat_base.exists():
        for wxid_dir in xwechat_base.iterdir():
            if not wxid_dir.is_dir():
                continue
            db_storage = wxid_dir / "db_storage"
            if not db_storage.exists():
                continue
            patterns = [
                "contact/contact.db",
                "session/session.db",
                "message/message_*.db",
                "message/biz_message_*.db",
            ]
            for pat in patterns:
                for db in sorted(db_storage.glob(pat)):
                    if db.stat().st_size < 4096:
                        continue
                    ok = verify_direct_key(bytes.fromhex(key_hex), db)
                    status = "✅" if ok else "❌"
                    print(f"  {status}  {db.name}")
                    if ok:
                        # 读取 salt，拼成 x'<key><salt>' 格式（与 Mac keys.json 完全一致）
                        try:
                            salt_hex = db.read_bytes()[:16].hex()
                            rel = str(db.relative_to(xwechat_base)).replace("\\", "/")
                            result[rel] = f"x'{key_hex}{salt_hex}'"
                        except Exception:
                            pass

    # 兜底：至少保存一条
    if not result:
        try:
            salt_hex = db_for_verify.read_bytes()[:16].hex()
        except Exception:
            salt_hex = "00" * 16
        result["key_0"] = f"x'{key_hex}{salt_hex}'"

    KEYS_FILE.parent.mkdir(parents=True, exist_ok=True)
    with open(KEYS_FILE, "w", encoding="utf-8") as f:
        json.dump(result, f, indent=2, ensure_ascii=False)

    print()
    print(f"✅ 保存到：{KEYS_FILE}（共 {len(result)} 条）")
    print()
    print("下一步：")
    print("  uv run python scripts\\decrypt_db.py")


if __name__ == "__main__":
    main()

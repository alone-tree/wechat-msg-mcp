#!/usr/bin/env python3
"""
从微信 Mac 进程内存中提取数据库加密密钥。

工作原理：
  微信使用 WCDB (SQLCipher 4, AES-256-CBC) 加密数据库，
  密钥以 x'<64字节hex><32字节盐值hex>' 格式缓存在进程内存中。
  本脚本通过 lldb 在 sqlite3_key 函数处设置断点，
  在微信登录时捕获密钥参数。

使用方法：
  1. 确保微信已安装并完成 ad-hoc 重新签名（见下方说明）
  2. 确保微信已退出（或准备好重新登录）
  3. 运行本脚本（需要 sudo）：
       sudo python3 scripts/extract_key.py
  4. 脚本启动后，打开微信并登录
  5. 密钥自动保存到 keys.json

微信重新签名（仅需一次）：
  sudo codesign --force --deep --sign - /Applications/WeChat.app
  # 签名后重新打开微信并登录

注意：每次微信更新后需重新签名。
"""
import json
import os
import re
import subprocess
import sys
import tempfile
import time
from pathlib import Path

PROJECT_ROOT = Path(__file__).parent.parent
KEYS_FILE = PROJECT_ROOT / "keys.json"

WECHAT_APP = "/Applications/WeChat.app/Contents/MacOS/WeChat"

# lldb 脚本：在 sqlite3_key 断点处读取内存中的密钥
LLDB_SCRIPT = r"""
import lldb
import re
import json
import os

keys = {}

def key_breakpoint_handler(frame, bp_loc, dict):
    thread = frame.GetThread()
    process = thread.GetProcess()
    target = process.GetTarget()

    # 第二个参数 (rsi) 是 key 指针，第三个 (rdx) 是 key 长度
    rsi = frame.FindRegister("rsi").GetValueAsUnsigned()
    rdx = frame.FindRegister("rdx").GetValueAsUnsigned()

    if rdx < 32 or rdx > 512:
        return False

    error = lldb.SBError()
    key_bytes = process.ReadMemory(rsi, rdx, error)

    if error.Success() and key_bytes:
        hex_key = key_bytes.hex()
        # 获取数据库文件路径（第一个参数 rdi 是 db 句柄，需要通过其他方式获取）
        # 简单处理：使用序号命名
        key_id = len(keys)
        keys[f"key_{key_id}"] = f"x'{hex_key}'"
        print(f"[+] 捕获到密钥 #{key_id}: x'{hex_key[:16]}...（共{rdx}字节）")

        # 保存到临时文件
        tmp_file = os.environ.get("KEYS_TMP_FILE", "/tmp/wechat_keys.json")
        with open(tmp_file, "w") as f:
            json.dump(keys, f, indent=2)

    return False  # 不停止进程，继续运行


def __lldb_init_module(debugger, internal_dict):
    target = debugger.GetSelectedTarget()
    if not target:
        target = debugger.CreateTarget(None)

    # 设置断点
    bp = target.BreakpointCreateByName("sqlite3_key")
    bp.SetScriptCallbackFunction("wechat_key_extract.key_breakpoint_handler")
    print(f"[*] 已在 sqlite3_key 设置断点 (id={bp.GetID()})")
    print("[*] 请打开微信并登录，密钥将自动捕获...")
"""

# 更简单的 lldb 命令行方式
LLDB_COMMANDS = """
# 等待 WeChat 进程
process attach --name WeChat --waitfor
# 在 sqlite3_key 设置断点
breakpoint set --name sqlite3_key
breakpoint command add --script-type python --python-function read_key
# 继续运行
continue
"""


def get_wechat_pid() -> int | None:
    """获取微信进程 PID"""
    try:
        result = subprocess.run(
            ["pgrep", "-x", "WeChat"],
            capture_output=True, text=True
        )
        pid_str = result.stdout.strip()
        return int(pid_str) if pid_str else None
    except Exception:
        return None


def extract_keys_via_lldb() -> dict:
    """通过 lldb 附加进程提取密钥（推荐方式）"""
    tmp_keys = "/tmp/wechat_keys.json"
    if os.path.exists(tmp_keys):
        os.remove(tmp_keys)

    pid = get_wechat_pid()

    # 生成 lldb python 脚本
    lldb_py = f"""
import lldb, json, os

_keys = {{}}
_TMP = "{tmp_keys}"

def on_sqlite3_key(frame, bp_loc, extra_args, internal_dict):
    process = frame.GetThread().GetProcess()
    rsi = frame.FindRegister("rsi").GetValueAsUnsigned()
    rdx = frame.FindRegister("rdx").GetValueAsUnsigned()

    if not (32 <= rdx <= 512):
        return False

    error = lldb.SBError()
    raw = process.ReadMemory(rsi, rdx, error)
    if not error.Success() or not raw:
        return False

    hex_key = raw.hex()
    key_label = f"key_{{len(_keys)}}"
    _keys[key_label] = f"x'{{hex_key}}'"
    print(f"[+] 已捕获 {{key_label}}: x'{{hex_key[:16]}}...'")

    with open(_TMP, "w") as f:
        json.dump(_keys, f, indent=2)
    return False


def __lldb_init_module(debugger, internal_dict):
    pass
"""

    lldb_script_file = "/tmp/wechat_key_extract.py"
    with open(lldb_script_file, "w") as f:
        f.write(lldb_py)

    # 生成 lldb 命令文件
    if pid:
        attach_cmd = f"process attach --pid {pid}"
        print(f"[*] 附加到已运行的微信进程 PID={pid}")
    else:
        attach_cmd = "process attach --name WeChat --waitfor"
        print("[*] 等待微信启动，请打开微信并登录...")

    lldb_cmds = f"""
command script import {lldb_script_file}
{attach_cmd}
breakpoint set --name sqlite3_key
breakpoint command add 1 --python-function wechat_key_extract.on_sqlite3_key
process continue
"""
    lldb_cmd_file = "/tmp/wechat_lldb_cmds.txt"
    with open(lldb_cmd_file, "w") as f:
        f.write(lldb_cmds)

    print("[*] 启动 lldb，等待捕获密钥（登录微信后约5-10秒）...")
    print("[*] 按 Ctrl+C 结束采集\n")

    try:
        proc = subprocess.Popen(
            ["lldb", "--source", lldb_cmd_file],
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
        )

        # 实时输出 lldb 日志，等待密钥文件出现
        timeout = 120
        start = time.time()
        keys_found = False

        while time.time() - start < timeout:
            if proc.stdout:
                line = proc.stdout.readline()
                if line:
                    print(line, end="")
                    if "[+] 已捕获" in line:
                        keys_found = True
                        # 给一点时间让更多密钥被捕获
                        time.sleep(5)
                        break

            if os.path.exists(tmp_keys):
                with open(tmp_keys) as f:
                    content = f.read().strip()
                if content and content != "{}":
                    keys_found = True
                    time.sleep(5)
                    break

            time.sleep(0.5)

        proc.terminate()

    except KeyboardInterrupt:
        print("\n[*] 用户中断")
        try:
            proc.terminate()
        except Exception:
            pass

    # 读取结果
    if os.path.exists(tmp_keys):
        with open(tmp_keys) as f:
            return json.load(f)
    return {}


def match_keys_to_dbs(raw_keys: dict) -> dict:
    """
    尝试将捕获的原始密钥匹配到对应的数据库文件。
    由于 lldb 方式难以直接获取 db 路径，这里通过尝试解密来验证。
    返回 {相对db路径: key_hex} 格式。
    """
    from pathlib import Path

    wechat_base = Path(
        "~/Library/Containers/com.tencent.xinWeChat/Data/Library/"
        "Application Support/com.tencent.xinWeChat/2.0b4.0.9"
    ).expanduser()

    if not wechat_base.exists():
        return raw_keys

    # 收集所有 db 文件
    db_files = []
    for account_dir in wechat_base.iterdir():
        if not account_dir.is_dir():
            continue
        for pattern in [
            "Contact/*.db",
            "Session/*.db",
            "Message/msg_*.db",
            "Group/*.db",
        ]:
            db_files.extend(account_dir.glob(pattern))

    if not db_files or not raw_keys:
        return raw_keys

    # 尝试用每个密钥解密每个 db，找到匹配的组合
    matched = {}
    key_values = list(raw_keys.values())

    try:
        from Crypto.Cipher import AES
        import hashlib
    except ImportError:
        # 无法验证，直接返回原始数据
        return raw_keys

    for db_path in db_files:
        for key_hex_str in key_values:
            # 格式：x'<64hex><32hex>' 或 x'<64hex>'
            m = re.match(r"x'([0-9a-fA-F]+)'", key_hex_str)
            if not m:
                continue
            hex_data = m.group(1)
            if len(hex_data) < 64:
                continue

            key_bytes = bytes.fromhex(hex_data[:64])

            try:
                with open(db_path, "rb") as f:
                    header = f.read(48)
                if len(header) < 48:
                    continue

                # SQLCipher 4: 前16字节是 salt，用 PBKDF2 派生真正的加密 key
                salt = header[:16]
                derived = hashlib.pbkdf2_hmac("sha512", key_bytes, salt, 256000, dklen=64)
                # 验证：尝试解密第17~48字节（IV + 第一个块）
                iv = header[16:32]
                cipher = AES.new(derived[:32], AES.MODE_CBC, iv)
                decrypted = cipher.decrypt(header[32:48])
                # SQLite magic header
                if decrypted[:6] == b"SQLite":
                    rel_path = str(db_path.relative_to(wechat_base))
                    matched[rel_path] = key_hex_str
                    print(f"[+] 匹配: {rel_path}")
                    break
            except Exception:
                continue

    return matched if matched else raw_keys


def main():
    print("=" * 60)
    print("  微信 Mac 数据库密钥提取工具")
    print("=" * 60)
    print()

    # 检查是否有 sudo 权限（lldb 需要）
    if os.geteuid() != 0:
        print("⚠️  建议以 sudo 运行以获得 lldb 调试权限：")
        print("     sudo python3 scripts/extract_key.py")
        print()
        answer = input("继续以当前权限运行？(y/N): ").strip().lower()
        if answer != "y":
            sys.exit(0)

    # 检查微信是否已重签名
    print("📋 前置检查：")
    result = subprocess.run(
        ["codesign", "-dv", "--verbose=4", "/Applications/WeChat.app"],
        capture_output=True, text=True
    )
    output = result.stdout + result.stderr
    is_adhoc = "adhoc" in output.lower() or "flags=0x2(" in output
    is_original = "Developer ID" in output or "Apple" in output

    if is_original and not is_adhoc:
        print("  ❌ 微信未完成 ad-hoc 重签名，lldb 无法附加")
        print()
        print("  请先执行以下命令（仅需一次）：")
        print("    sudo codesign --force --deep --sign - /Applications/WeChat.app")
        print()
        print("  重签名后重新打开微信并登录，再运行本脚本。")
        sys.exit(1)
    else:
        print("  ✅ 微信已 ad-hoc 签名或签名状态可接受")

    print()

    # 提取密钥
    raw_keys = extract_keys_via_lldb()

    if not raw_keys:
        print("\n❌ 未捕获到任何密钥。")
        print("可能原因：")
        print("  1. 微信未登录或未在提取期间打开/重新登录")
        print("  2. lldb 权限不足（请用 sudo 运行）")
        print("  3. 微信版本变化导致函数名变更")
        print("\n备选方案：参考 README.md 的手动提取步骤")
        sys.exit(1)

    print(f"\n[*] 共捕获 {len(raw_keys)} 个密钥，尝试匹配数据库文件...")
    matched_keys = match_keys_to_dbs(raw_keys)

    # 保存
    KEYS_FILE.parent.mkdir(parents=True, exist_ok=True)
    with open(KEYS_FILE, "w") as f:
        json.dump(matched_keys, f, indent=2, ensure_ascii=False)

    print(f"\n✅ 密钥已保存到: {KEYS_FILE}")
    print(f"   共 {len(matched_keys)} 个条目")
    print("\n下一步：运行 python3 scripts/decrypt_db.py 解密数据库")


if __name__ == "__main__":
    main()

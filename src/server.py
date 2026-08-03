"""
微信聊天记录 MCP Server 主入口
"""
import sys
import threading
import time
from pathlib import Path
from typing import Optional
from mcp.server.fastmcp import FastMCP

import sys
from .config import get_decrypted_dir, check_decrypted_dir
from .db.reader import WeChatDBReader
from .tools import contacts as contact_tools
from .tools import messages as message_tools
from .tools import summary as summary_tools

# Windows 平台优先使用本地解密数据库，回退到 HTTP API Reader
_IS_WINDOWS = sys.platform == "win32"
if _IS_WINDOWS:
    from .db.windows_reader import WeChatWindowsReader

# ------------------------------------------------------------------ #
# 初始化 MCP Server 和数据库 Reader                                    #
# ------------------------------------------------------------------ #

def _parse_args():
    import argparse
    parser = argparse.ArgumentParser(description="微信聊天记录 MCP Server")
    parser.add_argument(
        "--transport",
        choices=["stdio", "sse", "streamable-http"],
        default="stdio",
        help="传输模式：stdio（默认，本地）、sse 或 streamable-http（HTTP，远程）",
    )
    parser.add_argument(
        "--host",
        default="0.0.0.0",
        help="SSE 模式监听地址（默认 0.0.0.0）",
    )
    parser.add_argument(
        "--port",
        type=int,
        default=8765,
        help="SSE 模式监听端口（默认 8765）",
    )
    # 兼容 MCP 框架内部可能传入的参数，忽略未知参数
    args, _ = parser.parse_known_args()
    return args

_args = _parse_args()

mcp = FastMCP(
    name="wechat-message",
    instructions=(
        "本 MCP Server 提供对微信本地聊天记录的只读访问能力。\n"
        "可以查询联系人列表、聊天记录、搜索消息、生成统计汇总。\n"
        "Windows 平台：优先使用本地解密数据库（decrypted/ 目录），"
        "若未解密则回退到 WeChatDataAnalysis HTTP API（http://localhost:10392）。\n"
        "macOS 平台：请确保已运行 scripts/extract_key.py 和 scripts/decrypt_db.py 完成数据库解密。"
    ),
    host=_args.host,
    port=_args.port,
)

# 延迟初始化 Reader（避免启动时报错中断 MCP 握手）
_reader: Optional[WeChatDBReader] = None
_last_sync_time: float = 0
_last_key_refresh_time: float = 0
_SYNC_INTERVAL = 60
_KEY_REFRESH_COOLDOWN = 600
_sync_lock = threading.Lock()  # 防止并发同步写入导致文件损坏
_db_mtimes: dict[str, float] = {}  # 源 db 路径 -> 上次同步时的 mtime（避免重复解密）


def _auto_sync_decrypt() -> None:
    """
    增量同步：每分钟检查一次，仅对源文件 mtime 有变化的 message db 重新解密。
    解密失败且判断为密钥过期时，自动重提取密钥（需管理员权限运行 Server）。
    """
    global _last_sync_time, _reader, _keys_cache

    if not _sync_lock.acquire(blocking=False):
        return

    try:
        now = time.time()
        if now - _last_sync_time < _SYNC_INTERVAL:
            return

        import importlib.util, json, shutil, tempfile
        scripts_dir = Path(__file__).parent.parent / "scripts"
        spec = importlib.util.spec_from_file_location(
            "decrypt_db", scripts_dir / "decrypt_db.py"
        )
        decrypt_mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(decrypt_mod)

        keys_file = Path(__file__).parent.parent / "keys.json"
        if not keys_file.exists():
            return
        with open(keys_file) as f:
            keys: dict = json.load(f)
        if not keys:
            return

        all_dbs = decrypt_mod.find_db_for_key("")
        message_dbs = [
            p for p in all_dbs
            if p.parent.name.lower() == "message"
            and p.stat().st_size >= 4096
            and not p.name.startswith("message_fts")
            and not p.name.startswith("message_resource")
        ]

        needs_reload = False
        key_refresh_needed = False  # 是否需要刷新密钥

        with tempfile.TemporaryDirectory(prefix="wechat_sync_") as tmp_dir_str:
            tmp_dir = Path(tmp_dir_str)

            for src_path in message_dbs:
                src_key = str(src_path)
                try:
                    src_mtime = src_path.stat().st_mtime
                except OSError:
                    continue

                final_path = decrypt_mod.build_output_path(src_path)
                last_seen = _db_mtimes.get(src_key)
                if last_seen is None:
                    if final_path.exists() and final_path.stat().st_mtime >= src_mtime:
                        _db_mtimes[src_key] = src_mtime
                        continue
                elif src_mtime <= last_seen:
                    continue

                tmp_decrypted = tmp_dir / f"decrypted_{src_path.name}"

                # Windows 本地模式：用 decrypt_standalone 的解密逻辑
                decrypted_ok = False
                if _IS_WINDOWS:
                    try:
                        import importlib.util as _ilu, re as _re
                        _spec = _ilu.spec_from_file_location(
                            "decrypt_standalone",
                            scripts_dir / "decrypt_standalone.py"
                        )
                        _ds = _ilu.module_from_spec(_spec)
                        _spec.loader.exec_module(_ds)
                        # 从 keys.json 找该文件对应的 key
                        src_str = str(src_path).replace("\\", "/")
                        key_hex = None
                        for k, v in keys.items():
                            if src_str.endswith(k.replace("\\", "/")) or k.replace("\\", "/").endswith(src_path.name):
                                m = _re.match(r"x'([0-9a-fA-F]{64})", str(v))
                                if m:
                                    key_hex = m.group(1)
                                    break
                        if key_hex:
                            decrypted_ok = _ds.decrypt_database(src_path, key_hex, tmp_decrypted)
                    except Exception:
                        pass
                else:
                    direct_key = decrypt_mod._match_key_by_salt(src_path, keys)
                    if direct_key is not None:
                        decrypted_ok = decrypt_mod._try_direct_key(src_path, direct_key, tmp_decrypted)
                    if not decrypted_ok:
                        for key_hex in keys.values():
                            try:
                                if decrypt_mod.decrypt_database(src_path, key_hex, tmp_decrypted, keys, silent=True):
                                    decrypted_ok = True
                                    break
                            except Exception:
                                pass

                if not decrypted_ok:
                    # 解密失败：可能密钥已过期，标记需要刷新
                    # 注意：不记录 mtime，让下次仍然重试
                    key_refresh_needed = True
                    continue

                try:
                    import sqlite3 as _sqlite3
                    uri = f"file:{tmp_decrypted}?mode=ro&immutable=1"
                    conn = _sqlite3.connect(uri, uri=True)
                    conn.execute("SELECT count(*) FROM sqlite_master").fetchone()
                    conn.close()
                except Exception:
                    key_refresh_needed = True
                    _db_mtimes[src_key] = src_mtime
                    continue

                final_path.parent.mkdir(parents=True, exist_ok=True)
                try:
                    shutil.move(str(tmp_decrypted), str(final_path))
                except OSError:
                    continue

                for suffix in ("-shm", "-wal"):
                    side = Path(str(final_path) + suffix)
                    if side.exists():
                        try:
                            side.unlink()
                        except OSError:
                            pass

                _db_mtimes[src_key] = src_mtime
                needs_reload = True

        # 解密失败：尝试自动刷新密钥（需要管理员权限）
        if key_refresh_needed and _IS_WINDOWS:
            _auto_refresh_key(message_dbs, decrypt_mod, keys_file)

        if needs_reload and _reader is not None:
            _reader._discover_msg_dbs()

        _last_sync_time = now

    except Exception:
        pass
    finally:
        _sync_lock.release()


# 上次密钥刷新时间（避免频繁重提取）
_last_key_refresh_time: float = 0
_KEY_REFRESH_COOLDOWN = 600  # 10分钟内不重复刷新


def _auto_refresh_key(message_dbs: list, decrypt_mod, keys_file: Path) -> None:
    """
    自动刷新密钥：当解密失败时，从微信进程内存重新提取密钥。
    需要 Server 以管理员权限运行。
    冷却时间 10 分钟，避免反复扫内存。
    """
    global _last_key_refresh_time
    import json, logging, importlib.util

    logger = logging.getLogger(__name__)

    now = time.time()
    if now - _last_key_refresh_time < _KEY_REFRESH_COOLDOWN:
        return

    # 检查管理员权限
    try:
        import ctypes as _ctypes
        if not _ctypes.windll.shell32.IsUserAnAdmin():
            logger.warning("[key_refresh] 无管理员权限，跳过自动密钥刷新")
            return
    except Exception:
        return

    # 加载 decrypt_standalone 模块
    try:
        scripts_dir = Path(__file__).parent.parent / "scripts"
        spec = importlib.util.spec_from_file_location(
            "decrypt_standalone", scripts_dir / "decrypt_standalone.py"
        )
        standalone_mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(standalone_mod)
    except Exception as e:
        logger.warning(f"[key_refresh] 无法加载 decrypt_standalone: {e}")
        return

    # 找微信进程
    try:
        import pymem as _pymem
        for proc_name in ("Weixin.exe", "WeChat.exe"):
            try:
                pm = _pymem.Pymem(proc_name)
                pid = pm.process_id
                break
            except Exception:
                pid = None
        if not pid:
            logger.warning("[key_refresh] 未找到微信进程")
            return
    except ImportError:
        logger.warning("[key_refresh] pymem 未安装，跳过自动密钥刷新")
        return

    # 找一个用于验证的数据库
    db_for_verify = None
    for p in message_dbs:
        if p.stat().st_size >= standalone_mod.PAGE_SIZE:
            db_for_verify = p
            break
    if not db_for_verify:
        return

    logger.info(f"[key_refresh] 开始自动刷新密钥 (PID={pid})...")
    _last_key_refresh_time = now

    try:
        new_key = standalone_mod.find_key(pid, db_for_verify, silent=True)
        if not new_key:
            logger.warning("[key_refresh] 密钥提取失败")
            return

        # 更新 keys.json
        from pathlib import Path as _Path
        xwechat_base = standalone_mod.Path.home() / "Documents" / "xwechat_files"
        new_keys: dict = {}
        if xwechat_base.exists():
            for wxid_dir in xwechat_base.iterdir():
                db_storage = wxid_dir / "db_storage"
                if not db_storage.exists():
                    continue
                for db in db_storage.rglob("*.db"):
                    if db.stat().st_size < standalone_mod.PAGE_SIZE:
                        continue
                    try:
                        salt_hex = db.read_bytes()[:standalone_mod.SALT_SIZE].hex()
                        rel = str(db.relative_to(xwechat_base.parent)).replace("\\", "/")
                        new_keys[rel] = f"x'{new_key}{salt_hex}'"
                    except Exception:
                        pass

        if new_keys:
            with open(keys_file, "w", encoding="utf-8") as f:
                json.dump(new_keys, f, indent=2, ensure_ascii=False)
            # 清空 mtime 缓存，让下次同步重新解密所有文件
            _db_mtimes.clear()
            logger.info(f"[key_refresh] 密钥刷新成功，更新了 {len(new_keys)} 条记录")
    except Exception as e:
        logger.warning(f"[key_refresh] 密钥刷新异常: {e}")


def _get_reader() -> WeChatDBReader:
    global _reader
    if _IS_WINDOWS:
        # Windows 本地模式：decrypted/ 有数据时优先用本地 SQLite（与 Mac 完全一致）
        decrypted_dir = get_decrypted_dir()
        ready, _ = check_decrypted_dir(decrypted_dir)
        if ready:
            if _reader is None or isinstance(_reader, WeChatWindowsReader):
                _reader = WeChatDBReader(str(decrypted_dir))
            _auto_sync_decrypt()   # 与 Mac 相同的增量自动同步
            return _reader
        # 回退：decrypted/ 没有数据，用 HTTP API（需要 WeChatDataAnalysis 运行）
        if _reader is None or isinstance(_reader, WeChatDBReader):
            _reader = WeChatWindowsReader()
        return _reader
    # Mac：使用本地解密数据库（原有逻辑）
    if _reader is None:
        decrypted_dir = get_decrypted_dir()
        ready, msg = check_decrypted_dir(decrypted_dir)
        if not ready:
            raise RuntimeError(msg)
        _reader = WeChatDBReader(str(decrypted_dir))
    _auto_sync_decrypt()
    return _reader


# ------------------------------------------------------------------ #
# Tools：联系人                                                        #
# ------------------------------------------------------------------ #

@mcp.tool()
def list_contacts(
    keyword: str = "",
    include_groups: bool = True,
    limit: int = 100,
) -> str:
    """
    列出联系人和群聊列表。

    Args:
        keyword: 按名称/备注/昵称过滤，留空返回全部
        include_groups: 是否包含群聊（默认 True）
        limit: 最多返回条数（默认 100）
    """
    return contact_tools.list_contacts(_get_reader(), keyword=keyword,
                                       include_groups=include_groups, limit=limit)


@mcp.tool()
def find_contact(name: str) -> str:
    """
    查找联系人详细信息（支持备注名、昵称、微信ID模糊匹配）。

    Args:
        name: 联系人名称或微信ID
    """
    return contact_tools.find_contact(_get_reader(), name=name)


@mcp.tool()
def get_sessions(limit: int = 30) -> str:
    """
    获取最近会话列表（类似微信首页聊天列表）。

    Args:
        limit: 最多返回条数（默认 30）
    """
    return contact_tools.get_sessions(_get_reader(), limit=limit)


# ------------------------------------------------------------------ #
# Tools：消息                                                          #
# ------------------------------------------------------------------ #

@mcp.tool()
def get_chat_history(
    contact_name: str,
    start_date: str = "",
    end_date: str = "",
    limit: int = 100,
    offset: int = 0,
    output_format: str = "text",
) -> str:
    """
    获取与某联系人或群聊的聊天记录。

    Args:
        contact_name: 联系人备注名、昵称或微信ID（支持模糊匹配）
        start_date: 开始日期，格式 YYYY-MM-DD，留空不限
        end_date: 结束日期，格式 YYYY-MM-DD，留空不限
        limit: 最多返回条数（默认 100，最大 500）
        offset: 分页偏移（默认 0）
        output_format: 输出格式，"text"（可读文本）或 "json"
    """
    return message_tools.get_chat_history(
        _get_reader(),
        contact_name=contact_name,
        start_date=start_date or None,
        end_date=end_date or None,
        limit=limit,
        offset=offset,
        output_format=output_format,
    )


@mcp.tool()
def search_messages(
    keyword: str,
    contact_name: str = "",
    start_date: str = "",
    end_date: str = "",
    limit: int = 50,
    output_format: str = "text",
) -> str:
    """
    在聊天记录中搜索关键词。

    Args:
        keyword: 搜索关键词（必填）
        contact_name: 限定搜索范围到某联系人或群，留空则全局搜索
        start_date: 开始日期 YYYY-MM-DD，留空不限
        end_date: 结束日期 YYYY-MM-DD，留空不限
        limit: 最多返回条数（默认 50，最大 200）
        output_format: "text" 或 "json"
    """
    return message_tools.search_messages(
        _get_reader(),
        keyword=keyword,
        contact_name=contact_name or None,
        start_date=start_date or None,
        end_date=end_date or None,
        limit=limit,
        output_format=output_format,
    )


@mcp.tool()
def get_recent_messages(
    days: int = 7,
    group_only: bool = False,
    limit: int = 100,
    output_format: str = "text",
) -> str:
    """
    获取最近 N 天的聊天消息（跨所有会话）。

    Args:
        days: 最近几天（默认 7）
        group_only: 只返回群消息（默认 False）
        limit: 最多返回条数（默认 100）
        output_format: "text" 或 "json"
    """
    return message_tools.get_recent_messages(
        _get_reader(),
        days=days,
        group_only=group_only,
        limit=limit,
        output_format=output_format,
    )


# ------------------------------------------------------------------ #
# Tools：汇总统计                                                      #
# ------------------------------------------------------------------ #

@mcp.tool()
def summarize_chat(
    contact_name: str,
    period: str = "last_7_days",
    start_date: str = "",
    end_date: str = "",
) -> str:
    """
    汇总与某联系人/群的对话统计（消息量、活跃时段、成员发言排行等）。

    Args:
        contact_name: 联系人备注名/昵称/微信ID
        period: 预设时间段（当 start_date/end_date 为空时生效）：
                today / yesterday / this_week / last_week /
                this_month / last_7_days / last_30_days
        start_date: 自定义开始日期 YYYY-MM-DD（优先级高于 period）
        end_date: 自定义结束日期 YYYY-MM-DD
    """
    return summary_tools.summarize_chat(
        _get_reader(),
        contact_name=contact_name,
        period=period,
        start_date=start_date or None,
        end_date=end_date or None,
    )


@mcp.tool()
def chat_stats_overview(
    period: str = "last_7_days",
    top_n: int = 10,
) -> str:
    """
    跨所有会话的聊天统计总览，显示最活跃的 N 个会话。

    Args:
        period: 时间段：today / yesterday / this_week / last_week /
                this_month / last_7_days / last_30_days
        top_n: 显示消息最多的前 N 个会话（默认 10）
    """
    return summary_tools.chat_stats_overview(
        _get_reader(),
        period=period,
        top_n=top_n,
    )


# ------------------------------------------------------------------ #
# 诊断工具                                                             #
# ------------------------------------------------------------------ #

@mcp.tool()
def check_status() -> str:
    """
    检查 MCP Server 状态，包括数据源是否就绪、联系人数量等。
    遇到问题时首先运行此工具进行诊断。
    """
    lines = ["=== 微信 MCP Server 状态检查 ===", ""]

    if _IS_WINDOWS:
        decrypted_dir = get_decrypted_dir()
        ready, _ = check_decrypted_dir(decrypted_dir)
        if ready:
            lines.append("平台: Windows")
            lines.append(f"数据源: 本地解密数据库 ({decrypted_dir})")
        else:
            lines.append("平台: Windows")
            lines.append("数据源: WeChatDataAnalysis HTTP API (http://localhost:10392)")
        lines.append("")
        try:
            reader = _get_reader()
            contacts = reader.get_contacts()
            groups = [c for c in contacts if c.is_group]
            persons = [c for c in contacts if not c.is_group]
            lines.extend([
                f"状态: ✅ 后端连接正常",
                f"联系人总数: {len(persons)} 人",
                f"群聊总数:   {len(groups)} 个",
                "",
                "✅ Server 正常，可以开始查询聊天记录。",
            ])
        except Exception as e:
            lines.extend([
                f"状态: ❌ 后端连接失败",
                f"错误: {e}",
                "",
                "请按以下步骤操作：",
                "1. 启动 WeChatDataAnalysis",
                "2. 确保微信已登录",
                "3. 再次调用 check_status 确认就绪",
            ])
    else:
        # Mac：检查解密目录
        lines.append("平台: macOS")
        decrypted_dir = get_decrypted_dir()
        ready, msg = check_decrypted_dir(decrypted_dir)
        lines.append(f"解密目录: {decrypted_dir}")
        lines.append(f"状态: {'✅ 就绪' if ready else '❌ 未就绪'}")
        lines.append(f"详情: {msg}")

        if ready:
            try:
                reader = _get_reader()
                contacts = reader.get_contacts()
                groups = [c for c in contacts if c.is_group]
                persons = [c for c in contacts if not c.is_group]
                lines.extend([
                    "",
                    f"联系人总数: {len(persons)} 人",
                    f"群聊总数:   {len(groups)} 个",
                    f"消息数据库: {len(reader._msg_db_paths)} 个文件",
                    "",
                    "✅ Server 正常，可以开始查询聊天记录。",
                ])
            except Exception as e:
                lines.extend(["", f"❌ 初始化 Reader 失败: {e}"])
        else:
            lines.extend([
                "",
                "请按以下步骤操作：",
                "1. 确保微信已登录并运行",
                "2. 运行: python scripts/extract_key.py",
                "3. 运行: python scripts/decrypt_db.py",
                "4. 再次调用 check_status 确认就绪",
            ])

    return "\n".join(lines)


# ------------------------------------------------------------------ #
# 入口                                                                 #
# ------------------------------------------------------------------ #

def main():
    if _args.transport == "sse":
        print(f"启动 SSE 模式，监听 {_args.host}:{_args.port} ...")
        mcp.run(transport="sse")
    elif _args.transport == "streamable-http":
        print(f"启动 Streamable HTTP 模式，监听 {_args.host}:{_args.port} ...")
        mcp.run(transport="streamable-http")
    else:
        mcp.run(transport="stdio")


if __name__ == "__main__":
    main()

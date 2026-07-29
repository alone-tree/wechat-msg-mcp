"""
微信聊天记录 MCP Server 主入口
"""
import sys
import time
from pathlib import Path
from typing import Optional
from mcp.server.fastmcp import FastMCP

from .config import get_decrypted_dir, check_decrypted_dir
from .db.reader import WeChatDBReader
from .tools import contacts as contact_tools
from .tools import messages as message_tools
from .tools import summary as summary_tools

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
        "本 MCP Server 提供对微信 Mac 版本地聊天记录的只读访问能力。\n"
        "可以查询联系人列表、聊天记录、搜索消息、生成统计汇总。\n"
        "使用前请确保已运行 scripts/extract_key.py 和 scripts/decrypt_db.py 完成数据库解密。"
    ),
    host=_args.host,
    port=_args.port,
)

# 延迟初始化 Reader（避免启动时报错中断 MCP 握手）
_reader: Optional[WeChatDBReader] = None
_last_sync_time: float = 0
_SYNC_INTERVAL = float('inf')  # 禁用自动同步，避免覆盖损坏解密文件


def _copy_with_wal_checkpoint(src_path: Path, tmp_dir: Path) -> Path:
    """
    将源 db（及其 WAL/SHM）复制到临时目录，然后用 SQLite 做
    PRAGMA wal_checkpoint(TRUNCATE) 把 WAL 内容合并进主文件，
    返回合并后的临时 db 路径（可安全传给 decrypt_database）。
    """
    import shutil, sqlite3 as _sqlite3

    tmp_db = tmp_dir / src_path.name
    # 复制主文件
    shutil.copy2(src_path, tmp_db)
    # 复制 WAL（如存在）
    wal_src = Path(str(src_path) + "-wal")
    if wal_src.exists():
        shutil.copy2(wal_src, Path(str(tmp_db) + "-wal"))
    # 复制 SHM（如存在）
    shm_src = Path(str(src_path) + "-shm")
    if shm_src.exists():
        shutil.copy2(shm_src, Path(str(tmp_db) + "-shm"))

    # 用 SQLite 打开临时副本，触发 WAL 自动合并，然后强制 checkpoint
    # 注意：这些是已加密的 SQLCipher 文件，sqlite3 本身无法读取内容，
    # 但 SQLite 的 WAL 合并发生在文件层面，不需要解密密钥。
    # 实际上对加密文件直接 checkpoint 会失败——改为手动合并 WAL 字节。
    wal_tmp = Path(str(tmp_db) + "-wal")
    if wal_tmp.exists():
        _merge_wal_into_db(tmp_db, wal_tmp)
        # 合并后删除 WAL，避免 decrypt_database 再次读取旧 WAL
        try:
            wal_tmp.unlink()
        except OSError:
            pass
        shm_tmp = Path(str(tmp_db) + "-shm")
        try:
            shm_tmp.unlink()
        except OSError:
            pass

    return tmp_db


def _merge_wal_into_db(db_path: Path, wal_path: Path) -> None:
    """
    手动将 SQLite WAL 文件的帧写回到主 db 文件。
    WAL 格式：32字节文件头 + N×(24字节帧头 + page_size字节数据)
    只取每个 page_no 的最新帧（最后出现的那帧），覆写主 db 对应页。
    """
    WAL_HEADER_SIZE = 32
    WAL_FRAME_HEADER_SIZE = 24

    try:
        with open(wal_path, "rb") as wf:
            wal_data = wf.read()

        if len(wal_data) < WAL_HEADER_SIZE:
            return  # WAL 为空或损坏，跳过

        # 从 WAL 头读取页大小（偏移 8，4字节大端）
        import struct
        magic = wal_data[:4]
        # WAL magic: 0x377f0682 (big-endian) 或 0x377f0683
        if magic not in (b'\x37\x7f\x06\x82', b'\x37\x7f\x06\x83'):
            return  # 不是合法 WAL 文件

        page_size = struct.unpack(">I", wal_data[8:12])[0]
        if page_size == 1:
            page_size = 65536  # SQLite 特殊值

        frame_size = WAL_FRAME_HEADER_SIZE + page_size
        offset = WAL_HEADER_SIZE

        # 收集每个页号的最新数据（后出现的帧覆盖前面的）
        pages: dict[int, bytes] = {}
        while offset + frame_size <= len(wal_data):
            frame_header = wal_data[offset: offset + WAL_FRAME_HEADER_SIZE]
            page_no = struct.unpack(">I", frame_header[:4])[0]  # 1-based
            page_data = wal_data[offset + WAL_FRAME_HEADER_SIZE: offset + frame_size]
            if page_no > 0 and len(page_data) == page_size:
                pages[page_no] = page_data
            offset += frame_size

        if not pages:
            return

        # 将收集到的页写回主 db 文件
        with open(db_path, "r+b") as dbf:
            for page_no, page_data in pages.items():
                db_offset = (page_no - 1) * page_size
                dbf.seek(db_offset)
                dbf.write(page_data)
            dbf.flush()

    except Exception:
        pass  # WAL 合并失败不影响流程，decrypt 会用原始主文件内容


def _auto_sync_decrypt() -> None:
    """
    增量解密：检测微信原始 db 是否有新数据（包含 WAL），
    若有则先把 db+WAL 复制到临时目录并手动合并 WAL，
    再解密合并后的临时文件到 decrypted/ 目录。
    """
    global _last_sync_time, _reader

    now = time.time()
    if now - _last_sync_time < _SYNC_INTERVAL:
        return  # 距上次同步不足间隔，跳过

    try:
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

        if not decrypt_mod.WECHAT_BASE.exists():
            return

        all_dbs = decrypt_mod.find_db_for_key("")
        if not all_dbs:
            return

        # 只自动同步消息库，contact/session/group 不参与（避免 WAL 合并破坏稳定数据）
        message_dbs = [
            p for p in all_dbs
            if p.parent.name.lower() == "message"
            and p.stat().st_size >= 4096
        ]
        if not message_dbs:
            return

        with tempfile.TemporaryDirectory(prefix="wechat_wal_") as tmp_dir_str:
            tmp_dir = Path(tmp_dir_str)
            needs_reload = False

            for src_path in message_dbs:
                dst_path = decrypt_mod.build_output_path(src_path)

                # 把源 db + WAL 复制到临时目录并合并 WAL
                try:
                    tmp_db = _copy_with_wal_checkpoint(src_path, tmp_dir)
                except Exception:
                    tmp_db = src_path  # 复制失败退回使用原始文件

                # 优先用 salt 匹配找到对应 key，避免遍历所有密钥产生大量错误日志
                direct_key = decrypt_mod._match_key_by_salt(tmp_db, keys)
                if direct_key is not None:
                    if decrypt_mod._try_direct_key(tmp_db, direct_key, dst_path):
                        needs_reload = True
                        continue
                # salt 匹配失败，fallback 到旧版 PBKDF2（静默失败）
                for key_hex in keys.values():
                    try:
                        if decrypt_mod.decrypt_database(tmp_db, key_hex, dst_path, keys, silent=True):
                            needs_reload = True
                            break
                    except Exception:
                        pass

        if needs_reload and _reader is not None:
            # 只重新发现消息 db，不重建整个 reader（避免重载损坏的 contact.db）
            _reader._discover_msg_dbs()

        _last_sync_time = now

    except Exception:
        pass  # 自动同步失败不影响正常读取


def _get_reader() -> WeChatDBReader:
    global _reader
    # 先初始化 reader（用现有的解密文件）
    if _reader is None:
        decrypted_dir = get_decrypted_dir()
        ready, msg = check_decrypted_dir(decrypted_dir)
        if not ready:
            raise RuntimeError(msg)
        _reader = WeChatDBReader(str(decrypted_dir))
    # 再尝试自动同步解密（仅同步 message 类，不会影响 contact/session）
    _auto_sync_decrypt()
    # WAL checkpoint，读取尚未落盘的最新写入
    _reader.checkpoint_all()
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
    检查 MCP Server 状态，包括解密数据库目录是否就绪、联系人数量等。
    遇到问题时首先运行此工具进行诊断。
    """
    lines = ["=== 微信 MCP Server 状态检查 ===", ""]

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

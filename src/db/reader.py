"""
微信 Mac 版本地数据库读取层
支持解密后的 SQLite 数据库（SQLCipher 4 解密后的明文）
"""
import hashlib
import os
import sqlite3
import zstandard as zstd
from pathlib import Path
from typing import Optional
from datetime import datetime, timedelta

from .models import Contact, Message, Session


class WeChatDBReader:
    """
    读取微信本地解密数据库。

    使用前需先运行 scripts/extract_key.py 提取密钥，
    再运行 scripts/decrypt_db.py 解密数据库，
    解密后的数据库默认放在 <wechat_root>/decrypted/ 目录下。
    """

    def __init__(self, decrypted_dir: str):
        """
        :param decrypted_dir: 解密后的数据库目录路径，
               该目录应包含 contact/contact.db, session/session.db,
               message/message_*.db 等子目录和文件
        """
        self.root = Path(decrypted_dir).expanduser()
        if not self.root.exists():
            raise FileNotFoundError(
                f"解密数据库目录不存在: {self.root}\n"
                "请先运行 scripts/extract_key.py 和 scripts/decrypt_db.py 完成解密。"
            )

        self._contact_cache: dict[str, Contact] = {}   # username -> Contact
        self._contact_id_cache: dict[int, Contact] = {}  # row id -> Contact（新版微信群消息用数字ID）
        self._name2id_cache: dict[str, str] = {}       # md5 -> username（反查）
        self._my_username: str = ""                      # 自己的微信 username
        self._msg_db_paths: list[Path] = []
        self._zstd_decompressor = zstd.ZstdDecompressor()

        self._load_contacts()
        self._detect_my_username()
        self._discover_msg_dbs()

    # ------------------------------------------------------------------ #
    # 初始化                                                               #
    # ------------------------------------------------------------------ #

    def _load_contacts(self) -> None:
        """加载联系人信息到缓存"""
        contact_db = self.root / "contact" / "contact.db"
        if not contact_db.exists():
            return

        try:
            conn = self._open_db(contact_db)
        except Exception:
            return  # contact.db 损坏，跳过，不影响消息查询

        try:
            conn = self._open_db(contact_db)
        except Exception:
            return  # contact.db 损坏，跳过，不影响消息查询

        try:
            # 微信 4.x: contact 表，字段 username/remark/nick_name，有 id 字段
            # 旧版: Friend 表，字段 userName/remark/nickName
            for table, col_id, col_user, col_remark, col_nick in [
                ("contact", "id",   "username", "remark", "nick_name"),
                ("Friend",  "rowid","userName", "remark", "nickName"),
                ("WCContact","rowid","userName","remark", "nickName"),
            ]:
                try:
                    rows = conn.execute(
                        f"SELECT {col_id}, {col_user}, {col_remark}, {col_nick} FROM {table}"
                    ).fetchall()
                    for row_id, username, remark, nickname in rows:
                        if not username:
                            continue
                        is_group = username.endswith("@chatroom")
                        c = Contact(
                            username=username,
                            remark=remark or "",
                            nickname=nickname or "",
                            is_group=is_group,
                        )
                        self._contact_cache[username] = c
                        if row_id:
                            self._contact_id_cache[int(row_id)] = c
                    break
                except sqlite3.OperationalError:
                    continue
        except Exception:
            pass  # contact.db 读取失败时静默跳过
        finally:
            conn.close()

    def _detect_my_username(self) -> None:
        """
        从 xwechat_files 目录名或 session 数据库推断自己的微信 username。
        xwechat_files/<wxid>_<suffix>/db_storage/ 目录名就是账号的 wxid。
        """
        import re
        xwechat_base = Path(
            "~/Library/Containers/com.tencent.xinWeChat/Data/Documents/xwechat_files"
        ).expanduser()
        if xwechat_base.exists():
            for d in xwechat_base.iterdir():
                if not d.is_dir():
                    continue
                # 目录名格式：wxid_xxx_suffix
                m = re.match(r"(wxid_\w+)_[0-9a-f]+$", d.name)
                if m:
                    self._my_username = m.group(1)
                    return
                # 也可能直接是 wxid_xxx
                if d.name.startswith("wxid_"):
                    self._my_username = d.name
                    return

    def _discover_msg_dbs(self) -> None:
        """发现所有消息数据库文件"""
        msg_dir = self.root / "message"
        if not msg_dir.exists():
            return
        # 微信 4.x: message_0.db ~ message_N.db
        self._msg_db_paths = sorted(msg_dir.glob("message_*.db"))
        if not self._msg_db_paths:
            # 兼容旧版路径 msg_*.db
            self._msg_db_paths = sorted(msg_dir.glob("msg_*.db"))

    @staticmethod
    def _checkpoint_db(path: Path) -> None:
        """
        对指定数据库执行 WAL checkpoint，将 WAL 文件内容合并到主数据库。
        解密后的 db 不需要 checkpoint（没有真实的 WAL），直接跳过。
        """
        pass  # 解密后的 SQLite 文件不需要 checkpoint，跳过

    def checkpoint_all(self) -> None:
        """对所有数据库文件执行 WAL checkpoint，获取最新消息数据。"""
        dbs_to_checkpoint = []

        contact_db = self.root / "contact" / "contact.db"
        session_db = self.root / "session" / "session.db"
        dbs_to_checkpoint.extend([contact_db, session_db])
        dbs_to_checkpoint.extend(self._msg_db_paths)

        for db_path in dbs_to_checkpoint:
            self._checkpoint_db(db_path)

    @staticmethod
    def _open_db(path: Path) -> sqlite3.Connection:
        """只读方式打开 SQLite 数据库，先清理遗留的 shm/wal 文件避免 malformed 错误"""
        # 删掉遗留的 shm/wal（自动同步可能留下 0 字节的 wal + 非空 shm，导致 malformed）
        for suffix in ("-shm", "-wal"):
            side_file = path.with_name(path.name + suffix)
            if side_file.exists():
                try:
                    side_file.unlink()
                except OSError:
                    pass
        uri = f"file:{path}?mode=ro"
        conn = sqlite3.connect(uri, uri=True)
        conn.row_factory = sqlite3.Row
        return conn

    # ------------------------------------------------------------------ #
    # 内部工具                                                             #
    # ------------------------------------------------------------------ #

    @staticmethod
    def _username_to_md5(username: str) -> str:
        return hashlib.md5(username.encode()).hexdigest()

    def _decompress_content(self, raw: bytes, ct_flag: int) -> str:
        """
        ct_flag == 4 表示 zstd 压缩，其他情况直接按 UTF-8 解码。
        """
        if not raw:
            return ""
        try:
            if ct_flag == 4:
                data = self._zstd_decompressor.decompress(raw)
                return data.decode("utf-8", errors="replace")
            if isinstance(raw, bytes):
                return raw.decode("utf-8", errors="replace")
            return str(raw)
        except Exception:
            return "[内容解析失败]"

    def _parse_group_sender(self, content: str) -> tuple[Optional[str], str]:
        """
        群消息内容格式：'wxid_xxx:\n实际内容'
        返回 (sender_id, real_content)
        """
        if "\n" in content:
            first_line, rest = content.split("\n", 1)
            if first_line.endswith(":") and ("wxid_" in first_line or "@" in first_line):
                return first_line[:-1], rest
        return None, content

    def _get_sender_name(self, sender_id: Optional[str]) -> Optional[str]:
        if not sender_id:
            return None
        # 先按 username 查
        contact = self._contact_cache.get(sender_id)
        if contact:
            return contact.display_name
        # 如果是纯数字，尝试按 row id 查
        try:
            row_id = int(sender_id)
            contact = self._contact_id_cache.get(row_id)
            if contact:
                return contact.display_name
        except (ValueError, TypeError):
            pass
        return sender_id

    def _build_name2id_map(self, conn: sqlite3.Connection) -> dict[str, str]:
        """
        从 Name2Id 表构建 md5 -> username 映射（用于查找消息表对应的会话）
        """
        mapping: dict[str, str] = {}
        try:
            rows = conn.execute("SELECT rowid, userName FROM Name2Id").fetchall()
            for row in rows:
                username = row["userName"] if isinstance(row, sqlite3.Row) else row[1]
                if username:
                    md5 = self._username_to_md5(username)
                    mapping[md5] = username
        except sqlite3.OperationalError:
            pass
        return mapping

    def _ts_range(
        self,
        start_date: Optional[str] = None,
        end_date: Optional[str] = None,
    ) -> tuple[int, int]:
        """将日期字符串转为 Unix 时间戳范围"""
        if start_date:
            start_ts = int(datetime.strptime(start_date, "%Y-%m-%d").timestamp())
        else:
            start_ts = 0

        if end_date:
            dt_end = datetime.strptime(end_date, "%Y-%m-%d") + timedelta(days=1)
            end_ts = int(dt_end.timestamp())
        else:
            end_ts = int(datetime(9999, 12, 31).timestamp())

        return start_ts, end_ts

    # ------------------------------------------------------------------ #
    # 公开 API                                                             #
    # ------------------------------------------------------------------ #

    def get_contacts(self, keyword: str = "", include_groups: bool = True) -> list[Contact]:
        """
        获取联系人列表。

        :param keyword: 按显示名称/备注名/昵称过滤（空=全部）
        :param include_groups: 是否包含群聊
        """
        result = []
        for c in self._contact_cache.values():
            if not include_groups and c.is_group:
                continue
            if keyword:
                kw = keyword.lower()
                match = (
                    kw in c.remark.lower()
                    or kw in c.nickname.lower()
                    or kw in c.username.lower()
                )
                if not match:
                    continue
            result.append(c)

        result.sort(key=lambda c: (c.is_group, c.display_name))
        return result

    def get_contact(self, name_or_id: str) -> Optional[Contact]:
        """
        按 username / 备注名 / 昵称查找联系人（模糊匹配，返回最佳匹配）
        """
        # 精确匹配 username
        if name_or_id in self._contact_cache:
            return self._contact_cache[name_or_id]

        # 模糊匹配
        name_lower = name_or_id.lower()
        candidates = []
        for c in self._contact_cache.values():
            if (
                name_lower in c.remark.lower()
                or name_lower in c.nickname.lower()
            ):
                candidates.append(c)

        if not candidates:
            return None
        # 优先返回完全匹配
        for c in candidates:
            if c.remark.lower() == name_lower or c.nickname.lower() == name_lower:
                return c
        return candidates[0]

    def get_sessions(self, limit: int = 50) -> list[Session]:
        """获取最近会话列表"""
        session_db = self.root / "session" / "session.db"
        if not session_db.exists():
            return []

        sessions: list[Session] = []
        conn = self._open_db(session_db)
        try:
            for table, col_time, col_unread, col_summary in [
                ("SessionTable", "sort_timestamp", "unread_count", "summary"),  # 微信 4.x
                ("Chat", "createTime", "unreadCount", "lastMsg"),               # 旧版
            ]:
                try:
                    rows = conn.execute(
                        f"""SELECT username, {col_time}, {col_unread}, {col_summary}
                            FROM {table}
                            ORDER BY {col_time} DESC
                            LIMIT ?""",
                        (limit,),
                    ).fetchall()
                    for row in rows:
                        username = row[0] or ""
                        last_time = row[1] or 0
                        unread = row[2] or 0
                        last_msg = row[3] or ""
                        if isinstance(last_msg, bytes):
                            last_msg = self._decompress_content(last_msg, 4)

                        contact = self._contact_cache.get(username)
                        display = contact.display_name if contact else username
                        is_group = username.endswith("@chatroom")

                        sessions.append(Session(
                            username=username,
                            display_name=display,
                            last_message=str(last_msg)[:100],
                            last_time=last_time,
                            unread_count=unread,
                            is_group=is_group,
                        ))
                    break
                except sqlite3.OperationalError:
                    continue
        finally:
            conn.close()

        return sessions

    def get_messages(
        self,
        contact_name: str,
        start_date: Optional[str] = None,
        end_date: Optional[str] = None,
        limit: int = 100,
        offset: int = 0,
    ) -> list[Message]:
        """
        获取与某联系人/群的聊天记录。

        :param contact_name: 联系人备注名/昵称/username 或群名
        :param start_date: 开始日期 YYYY-MM-DD（包含）
        :param end_date: 结束日期 YYYY-MM-DD（包含）
        :param limit: 最多返回条数
        :param offset: 分页偏移
        """
        contact = self.get_contact(contact_name)
        if not contact:
            raise ValueError(f"未找到联系人: {contact_name}")

        return self._query_messages_by_username(
            contact.username,
            start_date=start_date,
            end_date=end_date,
            limit=limit,
            offset=offset,
        )

    def _query_messages_by_username(
        self,
        username: str,
        start_date: Optional[str] = None,
        end_date: Optional[str] = None,
        limit: int = 100,
        offset: int = 0,
    ) -> list[Message]:
        """按 username 直接查询消息"""
        md5 = self._username_to_md5(username)
        table_name = f"Msg_{md5}"
        start_ts, end_ts = self._ts_range(start_date, end_date)

        messages: list[Message] = []
        is_group = username.endswith("@chatroom")

        for db_path in self._msg_db_paths:
            conn = self._open_db(db_path)
            try:
                # 检查表是否存在
                exists = conn.execute(
                    "SELECT name FROM sqlite_master WHERE type='table' AND name=?",
                    (table_name,),
                ).fetchone()
                if not exists:
                    continue

                rows = conn.execute(
                    f"""SELECT local_id, server_id, local_type,
                               real_sender_id, create_time,
                               message_content, WCDB_CT_message_content,
                               origin_source
                        FROM {table_name}
                        WHERE create_time >= ? AND create_time < ?
                        ORDER BY create_time ASC, local_id ASC
                        LIMIT ? OFFSET ?""",
                    (start_ts, end_ts, limit, offset),
                ).fetchall()

                for row in rows:
                    (local_id, server_id, local_type,
                     real_sender_id, create_time,
                     raw_content, ct_flag, origin_source) = row

                    content = self._decompress_content(raw_content, ct_flag or 0)

                    # 判断发送方
                    # 新版微信规律：origin_source=1 是自己发的，origin_source=2 是别人发的
                    # 群消息：message_content 带 "wxid_xxx:\n" 前缀的是别人发的，无前缀是自己发的
                    sender_id: Optional[str] = None
                    origin_source = row[7] if len(row) > 7 else None

                    if is_group and content:
                        extracted_id, content = self._parse_group_sender(content)
                        if extracted_id:
                            # 有 wxid 前缀 = 别人发的
                            sender_id = extracted_id
                            is_from_me = False
                        else:
                            # 无 wxid 前缀 = 自己发的
                            sender_id = self._my_username or real_sender_id
                            is_from_me = True
                    else:
                        sender_id = real_sender_id
                        if self._my_username:
                            is_from_me = (real_sender_id == self._my_username) or (not real_sender_id)
                        else:
                            is_from_me = not bool(real_sender_id)

                    messages.append(Message(
                        local_id=local_id or 0,
                        server_id=server_id or 0,
                        create_time=create_time or 0,
                        local_type=local_type or 0,
                        content=content,
                        is_from_me=is_from_me,
                        sender_id=sender_id,
                        sender_name=self._get_sender_name(sender_id),
                        chat_username=username,
                        source_db=str(db_path),
                    ))
            finally:
                conn.close()

        # 多 db 文件合并后重新按时间排序，截取 limit 条
        messages.sort(key=lambda m: (m.create_time, m.local_id))
        return messages[offset: offset + limit]

    def search_messages(
        self,
        keyword: str,
        contact_name: Optional[str] = None,
        start_date: Optional[str] = None,
        end_date: Optional[str] = None,
        limit: int = 50,
    ) -> list[Message]:
        """
        全局关键词搜索。

        :param keyword: 搜索关键词
        :param contact_name: 限定某联系人/群（空=全局搜索）
        :param start_date: 日期范围开始
        :param end_date: 日期范围结束
        :param limit: 最大返回条数
        """
        start_ts, end_ts = self._ts_range(start_date, end_date)
        results: list[Message] = []

        # 确定要搜索的 username 列表
        if contact_name:
            contact = self.get_contact(contact_name)
            if not contact:
                return []
            target_usernames = {contact.username}
        else:
            target_usernames = None  # 全局

        for db_path in self._msg_db_paths:
            if len(results) >= limit:
                break

            conn = self._open_db(db_path)
            try:
                name2id = self._build_name2id_map(conn)

                # 获取当前 db 中的所有 Msg_ 表
                tables = conn.execute(
                    "SELECT name FROM sqlite_master WHERE type='table' AND name LIKE 'Msg_%'"
                ).fetchall()

                for (table_name,) in tables:
                    if len(results) >= limit:
                        break

                    md5_part = table_name[4:]  # 去掉 "Msg_" 前缀
                    username = name2id.get(md5_part, "")

                    if target_usernames and username not in target_usernames:
                        continue

                    is_group = username.endswith("@chatroom")

                    try:
                        rows = conn.execute(
                            f"""SELECT local_id, server_id, local_type,
                                       real_sender_id, create_time,
                                       message_content, WCDB_CT_message_content,
                                       origin_source
                                FROM {table_name}
                                WHERE create_time >= ? AND create_time < ?
                                ORDER BY create_time DESC
                                LIMIT 500""",
                            (start_ts, end_ts),
                        ).fetchall()
                    except sqlite3.OperationalError:
                        continue

                    for row in rows:
                        (local_id, server_id, local_type,
                         real_sender_id, create_time,
                         raw_content, ct_flag, origin_source) = row

                        content = self._decompress_content(raw_content, ct_flag or 0)

                        if keyword.lower() not in content.lower():
                            continue

                        sender_id: Optional[str] = None
                        if is_group and content:
                            extracted_id, content = self._parse_group_sender(content)
                            if extracted_id:
                                sender_id = extracted_id
                                is_from_me = False
                            else:
                                sender_id = self._my_username or real_sender_id
                                is_from_me = True
                        else:
                            sender_id = real_sender_id
                            if self._my_username:
                                is_from_me = (real_sender_id == self._my_username) or (not real_sender_id)
                            else:
                                is_from_me = not bool(real_sender_id)

                        results.append(Message(
                            local_id=local_id or 0,
                            server_id=server_id or 0,
                            create_time=create_time or 0,
                            local_type=local_type or 0,
                            content=content,
                            is_from_me=is_from_me,
                            sender_id=sender_id,
                            sender_name=self._get_sender_name(sender_id),
                            chat_username=username,
                            source_db=str(db_path),
                        ))
                        if len(results) >= limit:
                            break
            finally:
                conn.close()

        results.sort(key=lambda m: m.create_time)
        return results[:limit]

    def get_recent_messages(
        self,
        days: int = 7,
        group_only: bool = False,
        limit: int = 200,
    ) -> list[Message]:
        """
        获取最近 N 天的消息（按时间降序）

        :param days: 最近几天
        :param group_only: 只返回群消息
        :param limit: 最大条数
        """
        since = datetime.now() - timedelta(days=days)
        start_date = since.strftime("%Y-%m-%d")

        # 先从 sessions 获取最近活跃的会话
        sessions = self.get_sessions(limit=100)
        if group_only:
            sessions = [s for s in sessions if s.is_group]

        all_msgs: list[Message] = []
        for session in sessions[:30]:  # 最多查前30个会话
            try:
                msgs = self._query_messages_by_username(
                    session.username,
                    start_date=start_date,
                    limit=50,
                )
                all_msgs.extend(msgs)
            except Exception:
                continue

        all_msgs.sort(key=lambda m: m.create_time, reverse=True)
        return all_msgs[:limit]

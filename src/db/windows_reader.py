"""
微信 Windows 版数据读取层

通过调用 WeChatDataAnalysis 后端（http://localhost:10392）的 HTTP API
读取联系人、会话、消息数据。

接口与 WeChatDBReader 保持一致，可以直接替换使用。

前置条件：
  WeChatDataAnalysis 已安装并运行，微信已登录。
  默认后端地址：http://localhost:10392
  可通过环境变量 WCDA_BASE_URL 自定义。
"""
import os
import urllib.request
import urllib.parse
import urllib.error
import json
import time
from datetime import datetime, timedelta
from typing import Optional

from .models import Contact, Message, Session


_DEFAULT_BASE_URL = "http://localhost:10392"


class WeChatWindowsReader:
    """
    通过 WeChatDataAnalysis HTTP API 读取微信 Windows 数据。
    接口与 WeChatDBReader 完全一致。
    """

    def __init__(self, base_url: str = ""):
        self._base = (base_url or os.environ.get("WCDA_BASE_URL", _DEFAULT_BASE_URL)).rstrip("/")
        self._contact_cache: dict[str, Contact] = {}
        self._session_cache: list[Session] = []
        self._cache_ts: float = 0
        self._cache_ttl: float = 60.0   # 联系人/会话缓存 60 秒
        self._profile_cache: dict[str, str] = {}   # username -> 显示名
        # 群昵称 → 真实备注名 的别名映射（从系统撤回消息自动提取）
        # 例如：{"莫言": "边计涛 信息安全"}
        self._alias_map: dict[str, str] = {}
        # 兼容 server.py 里访问的属性
        self._msg_db_paths: list = []
        self._my_username: str = ""

        # 验证后端是否可达
        self._check_backend()
        # 预加载联系人缓存
        self._refresh_cache()

    # ------------------------------------------------------------------ #
    # 内部工具                                                             #
    # ------------------------------------------------------------------ #

    def _check_backend(self) -> None:
        """检查后端是否可达，不可达时抛出异常"""
        try:
            self._get("/api")
        except Exception as e:
            raise RuntimeError(
                f"无法连接到 WeChatDataAnalysis 后端 ({self._base})\n"
                f"请确保 WeChatDataAnalysis 已启动且微信已登录。\n"
                f"错误详情: {e}"
            )

    def _get(self, path: str, params: dict = None) -> dict:
        """发送 GET 请求，返回 JSON 响应"""
        url = self._base + path
        if params:
            # 过滤掉 None 值
            clean = {k: v for k, v in params.items() if v is not None}
            if clean:
                url += "?" + urllib.parse.urlencode(clean)
        req = urllib.request.Request(url, headers={"Accept": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=10) as r:
                return json.loads(r.read().decode("utf-8"))
        except urllib.error.HTTPError as e:
            body = e.read().decode("utf-8", errors="replace")
            raise RuntimeError(f"HTTP {e.code} {path}: {body[:200]}")
        except urllib.error.URLError as e:
            raise RuntimeError(f"无法连接后端 {self._base}: {e.reason}")

    def _refresh_cache(self, force: bool = False) -> None:
        """刷新联系人和会话缓存"""
        now = time.time()
        if not force and now - self._cache_ts < self._cache_ttl:
            return

        # 加载联系人
        try:
            data = self._get("/api/chat/contacts")
            self._contact_cache = {}
            for c in data.get("contacts", []):
                username = c.get("username", "")
                if not username:
                    continue
                contact = Contact(
                    username=username,
                    remark=c.get("remark") or "",
                    nickname=c.get("nickname") or c.get("name") or "",
                    is_group=username.endswith("@chatroom"),
                )
                self._contact_cache[username] = contact
        except Exception:
            pass

        # 从 sessions 接口补充显示名（sessions 返回的 name 更可靠）
        try:
            data = self._get("/api/chat/sessions", {"limit": 200})
            account = data.get("account", "")
            # 从账号目录名推断 my_username
            if account and not self._my_username:
                # account 格式：wxid_xxx_a518
                import re
                m = re.match(r"(wxid_\w+?)_[0-9a-f]+$", account)
                self._my_username = m.group(1) if m else account

            for s in data.get("sessions", []):
                username = s.get("username", "")
                if not username:
                    continue
                # 如果联系人缓存里没有，从 session 补充
                if username not in self._contact_cache:
                    self._contact_cache[username] = Contact(
                        username=username,
                        remark="",
                        nickname=s.get("name") or username,
                        is_group=username.endswith("@chatroom"),
                    )
                else:
                    # 用 session 的 name 补充空的 nickname
                    c = self._contact_cache[username]
                    if not c.nickname and s.get("name"):
                        self._contact_cache[username] = Contact(
                            username=c.username,
                            remark=c.remark,
                            nickname=s.get("name"),
                            is_group=c.is_group,
                        )
        except Exception:
            pass

        self._cache_ts = now

    @staticmethod
    def _ts_range(
        start_date: Optional[str],
        end_date: Optional[str],
    ) -> tuple[int, int]:
        if start_date:
            start_ts = int(datetime.strptime(start_date, "%Y-%m-%d").timestamp())
        else:
            start_ts = 0
        if end_date:
            dt_end = datetime.strptime(end_date, "%Y-%m-%d") + timedelta(days=1)
            end_ts = int(dt_end.timestamp())
        else:
            end_ts = 9999999999  # Windows 不支持 datetime(9999,12,31).timestamp()
        return start_ts, end_ts

    def _parse_message(self, raw: dict, chat_username: str) -> Message:
        """将后端返回的消息 dict 转换为 Message 对象"""
        content = raw.get("content") or ""
        render_type = raw.get("renderType", "")
        if render_type and render_type != "text" and not content:
            content = f"[{render_type}]"
        title = raw.get("title", "")
        if title and render_type in ("link", "miniprogram"):
            content = f"{title}\n{content}".strip()

        sender_username = raw.get("senderUsername") or ""
        is_sent = raw.get("isSent", False)
        sender_display = raw.get("senderDisplayName") or ""

        # 从系统撤回消息中提取别名映射
        # 系统消息 content 格式："\"群昵称 真实昵称\" 撤回了一条消息"
        # 例如："\"莫言 边计涛 信息安全\" 撤回了一条消息"
        # 或者："\"边计涛 信息安全\" 撤回了一条消息"（此时群昵称=真实昵称）
        if render_type == "system" or (raw.get("type") == 10000):
            self._extract_alias_from_system_msg(content)

        # 确定发送者名称
        if is_sent:
            sender_name = "我"
        elif sender_display:
            # 检查是否有已知的真实身份映射
            real_name = self._alias_map.get(sender_display)
            if real_name and real_name != sender_display:
                sender_name = f"{sender_display}（{real_name}）"
            else:
                sender_name = sender_display
        else:
            sender_name = self._get_sender_name_from_cache(sender_username)

        return Message(
            local_id=raw.get("localId") or 0,
            server_id=raw.get("serverId") or raw.get("serverIdStr") or 0,
            create_time=raw.get("createTime") or 0,
            local_type=raw.get("type") or 0,
            content=content,
            is_from_me=bool(is_sent),
            sender_id=sender_username or (self._my_username if is_sent else ""),
            sender_name=sender_name,
            chat_username=chat_username,
            source_db="windows_api",
        )

    def _extract_alias_from_system_msg(self, content: str) -> None:
        """
        从系统消息中提取群昵称和真实昵称的对应关系。

        微信系统消息格式（撤回）：
          "\"边计涛 信息安全\" 撤回了一条消息"       → 说明该人群昵称可能是别的
          content 里的名字是微信真实昵称/备注，
          而同条消息的 senderDisplayName 是群昵称。

        另一种格式（后端统一处理后）：
          content = '"边计涛 信息安全" 撤回了一条消息'
          此处提取 "边计涛 信息安全" 作为真实昵称。
        """
        import re
        # 匹配 "XXX" 撤回了一条消息
        m = re.search(r'"([^"]+)"\s*撤回了一条消息', content)
        if not m:
            return
        real_name = m.group(1).strip()
        if not real_name:
            return
        # 存入 profile_cache，供后续通过备注名反查
        # 同时记录到 alias_map（真实名 → 真实名，作为已知真实身份）
        self._alias_map.setdefault(real_name, real_name)

    def _get_sender_name_from_cache(self, sender_id: str) -> str:
        """只查本地缓存，不发 HTTP 请求"""
        if not sender_id:
            return ""
        if sender_id in self._profile_cache:
            return self._profile_cache[sender_id]
        c = self._contact_cache.get(sender_id)
        if c:
            return c.display_name
        return sender_id

    def _get_sender_name(self, sender_id: str) -> Optional[str]:
        if not sender_id:
            return None
        # 1. 联系人缓存（好友/群）
        c = self._contact_cache.get(sender_id)
        if c:
            return c.display_name
        # 2. 已查询过的 profile 缓存
        if sender_id in self._profile_cache:
            return self._profile_cache[sender_id]
        # 3. 自己
        if self._my_username and sender_id == self._my_username:
            self._profile_cache[sender_id] = "我"
            return "我"
        # 4. 向后端查询 profile（群成员、非好友），超时 1 秒
        try:
            url = (self._base + "/api/chat/contacts/profile?"
                   + urllib.parse.urlencode({"username": sender_id}))
            req = urllib.request.Request(url, headers={"Accept": "application/json"})
            with urllib.request.urlopen(req, timeout=1) as r:
                data = json.loads(r.read().decode("utf-8"))
            contact = data.get("contact") or {}
            name = (contact.get("remark")
                    or contact.get("displayName")
                    or contact.get("nickname")
                    or "")
            if name:
                self._profile_cache[sender_id] = name
                return name
        except Exception:
            pass
        # 5. 无法解析，返回原始 id
        self._profile_cache[sender_id] = sender_id   # 避免反复查询
        return sender_id

    # ------------------------------------------------------------------ #
    # 公开 API（与 WeChatDBReader 接口一致）                               #
    # ------------------------------------------------------------------ #

    def get_contacts(self, keyword: str = "", include_groups: bool = True) -> list[Contact]:
        """获取联系人列表"""
        self._refresh_cache()
        result = []
        for c in self._contact_cache.values():
            if not include_groups and c.is_group:
                continue
            if keyword:
                kw = keyword.lower()
                if not (kw in c.remark.lower()
                        or kw in c.nickname.lower()
                        or kw in c.username.lower()):
                    continue
            result.append(c)
        result.sort(key=lambda c: (c.is_group, c.display_name))
        return result

    def get_contact(self, name_or_id: str) -> Optional[Contact]:
        """按 username / 备注名 / 昵称查找联系人"""
        self._refresh_cache()
        if name_or_id in self._contact_cache:
            return self._contact_cache[name_or_id]

        name_lower = name_or_id.lower()
        candidates = [
            c for c in self._contact_cache.values()
            if name_lower in c.remark.lower() or name_lower in c.nickname.lower()
        ]
        if not candidates:
            return None
        for c in candidates:
            if c.remark.lower() == name_lower or c.nickname.lower() == name_lower:
                return c
        return candidates[0]

    def get_sessions(self, limit: int = 50) -> list[Session]:
        """获取最近会话列表"""
        try:
            data = self._get("/api/chat/sessions", {"limit": limit})
        except Exception:
            return []

        sessions = []
        for s in data.get("sessions", []):
            username = s.get("username", "")
            if not username:
                continue
            # 把 lastMessageTime 转成时间戳（后端返回的是格式化字符串如 "21:44"，用 sortSeq 更好）
            last_time = s.get("sortSeq") or s.get("lastMessageTime") or 0
            if isinstance(last_time, str):
                last_time = 0  # 无法解析的字符串时间置 0

            sessions.append(Session(
                username=username,
                display_name=s.get("name") or username,
                last_message=s.get("lastMessage") or "",
                last_time=int(last_time) if last_time else 0,
                unread_count=s.get("unreadCount") or 0,
                is_group=username.endswith("@chatroom"),
            ))
        return sessions

    def get_messages(
        self,
        contact_name: str,
        start_date: Optional[str] = None,
        end_date: Optional[str] = None,
        limit: int = 100,
        offset: int = 0,
    ) -> list[Message]:
        """获取与某联系人/群的聊天记录"""
        contact = self.get_contact(contact_name)
        if not contact:
            raise ValueError(f"未找到联系人: {contact_name}")

        # 群聊时，先预热别名映射（拉最近200条，覆盖撤回系统消息）
        if contact.is_group and contact.username not in self._alias_map_warmed:
            self._warmup_alias_map(contact.username)

        return self._query_messages_by_username(
            contact.username,
            start_date=start_date,
            end_date=end_date,
            limit=limit,
            offset=offset,
        )

    # 已预热过的群 username 集合，避免重复预热
    _alias_map_warmed: set = set()

    def _warmup_alias_map(self, username: str) -> None:
        """
        拉取最近200条消息，建立 群昵称→真实名 的别名映射。

        关联逻辑：
          1. 普通消息：senderUsername → senderDisplayName（群昵称）
          2. 撤回系统消息：content 里提取真实名（备注/昵称）
          3. 通过联系人缓存反查：真实名 → username → 群昵称
        """
        import re
        try:
            data = self._get("/api/chat/messages", {
                "username": username,
                "limit": 200,
                "order": "desc",
            })
        except Exception:
            self._alias_map_warmed.add(username)
            return

        # username → 群昵称
        uid_to_display: dict[str, str] = {}
        # 撤回消息里出现的真实名集合
        retracted_real_names: set[str] = set()

        for raw in data.get("messages", []):
            sid = raw.get("senderUsername") or ""
            display = raw.get("senderDisplayName") or ""
            if sid and display:
                uid_to_display[sid] = display

            if raw.get("type") == 10000:
                content = raw.get("content") or ""
                m = re.search(r'"([^"]+)"\s*撤回了一条消息', content)
                if m:
                    retracted_real_names.add(m.group(1).strip())

        # 构建 备注/昵称 → username 的反查表
        remark_to_uid: dict[str, str] = {}
        for uid, contact in self._contact_cache.items():
            if contact.remark:
                remark_to_uid[contact.remark] = uid
            if contact.nickname:
                remark_to_uid[contact.nickname] = uid

        # 关联：真实名 → username → 群昵称
        for real_name in retracted_real_names:
            uid = remark_to_uid.get(real_name)
            if uid and uid in uid_to_display:
                group_nick = uid_to_display[uid]
                if group_nick != real_name:
                    self._alias_map[group_nick] = real_name

        self._alias_map_warmed.add(username)

    def _query_messages_by_username(
        self,
        username: str,
        start_date: Optional[str] = None,
        end_date: Optional[str] = None,
        limit: int = 100,
        offset: int = 0,
    ) -> list[Message]:
        """按 username 查询消息"""
        start_ts, end_ts = self._ts_range(start_date, end_date)

        params: dict = {
            "username": username,
            "limit":    limit,
            "offset":   offset,
            "order":    "asc",
        }
        if start_date or end_date:
            params["limit"] = min(limit * 3, 500)

        try:
            data = self._get("/api/chat/messages", params)
        except Exception as e:
            raise ValueError(f"查询消息失败: {e}")

        # 先收集原始消息，同时建立 username → displayName 和 username → realName 的映射
        import re
        raw_messages = []
        username_to_display: dict[str, str] = {}   # senderUsername → 群昵称
        username_to_real: dict[str, str] = {}       # senderUsername → 真实昵称（从系统消息提取）

        for raw in data.get("messages", []):
            create_time = raw.get("createTime") or 0
            if create_time < start_ts or create_time >= end_ts:
                continue
            raw_messages.append(raw)

            sid = raw.get("senderUsername") or ""
            display = raw.get("senderDisplayName") or ""
            if sid and display:
                username_to_display[sid] = display

            # 从系统撤回消息提取真实昵称
            if raw.get("type") == 10000:
                content = raw.get("content") or ""
                m = re.search(r'"([^"]+)"\s*撤回了一条消息', content)
                if m and sid:
                    username_to_real[sid] = m.group(1).strip()

        # 建立 群昵称 → 真实昵称 的别名映射
        for sid, display in username_to_display.items():
            real = username_to_real.get(sid, "")
            if real and real != display:
                self._alias_map[display] = real

        messages = [self._parse_message(raw, username) for raw in raw_messages]
        messages = messages[offset: offset + limit]
        return messages

    def search_messages(
        self,
        keyword: str,
        contact_name: Optional[str] = None,
        start_date: Optional[str] = None,
        end_date: Optional[str] = None,
        limit: int = 50,
    ) -> list[Message]:
        """关键词搜索消息"""
        start_ts, end_ts = self._ts_range(start_date, end_date)

        # 先调后端的搜索接口
        params = {
            "keyword": keyword,
            "limit":   min(limit * 2, 200),
        }
        if contact_name:
            contact = self.get_contact(contact_name)
            if contact:
                params["username"] = contact.username

        try:
            data = self._get("/api/chat/messages", params)
            raw_messages = data.get("messages", [])
        except Exception:
            raw_messages = []

        # 如果后端没有 keyword 参数支持，退化为拉取后本地过滤
        if not raw_messages and contact_name:
            contact = self.get_contact(contact_name)
            if contact:
                try:
                    data = self._get("/api/chat/messages", {
                        "username": contact.username,
                        "limit": 500,
                    })
                    raw_messages = data.get("messages", [])
                except Exception:
                    raw_messages = []

        results = []
        for raw in raw_messages:
            create_time = raw.get("createTime") or 0
            if create_time < start_ts or create_time >= end_ts:
                continue
            content = raw.get("content") or ""
            if keyword.lower() not in content.lower():
                continue
            chat_username = raw.get("strTalker") or (
                self.get_contact(contact_name).username if contact_name else ""
            )
            results.append(self._parse_message(raw, chat_username))
            if len(results) >= limit:
                break

        results.sort(key=lambda m: m.create_time)
        return results[:limit]

    def get_recent_messages(
        self,
        days: int = 7,
        group_only: bool = False,
        limit: int = 200,
    ) -> list[Message]:
        """获取最近 N 天的消息"""
        since = datetime.now() - timedelta(days=days)
        start_date = since.strftime("%Y-%m-%d")

        sessions = self.get_sessions(limit=100)
        if group_only:
            sessions = [s for s in sessions if s.is_group]

        all_msgs: list[Message] = []
        for session in sessions[:30]:
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

    # ------------------------------------------------------------------ #
    # server.py 兼容属性                                                   #
    # ------------------------------------------------------------------ #

    def checkpoint_all(self) -> None:
        """兼容接口，Windows 模式不需要 checkpoint"""
        pass

"""MCP 工具注册：七个工具，全部委托 service 层。

依据 docs/重构方案-2026-11-13.md 第 6 与 20 节。
工具只做参数转换、导出判定与 JSON 序列化，业务逻辑在 src/service.py。
"""
from __future__ import annotations

import json
from typing import Optional

AUTO_EXPORT_ROWS = 200
AUTO_EXPORT_BYTES = 80000
EXPORT_KEYS = ("messages", "entities", "items", "moments", "members", "groups", "by_day")


def _dumps(obj) -> str:
    return json.dumps(obj, ensure_ascii=False)


def _pick_rows(obj: dict, keys: tuple[str, ...]) -> list:
    for key in keys:
        value = obj.get(key)
        if isinstance(value, list) and value:
            return value
    return []


def _trim_preview(obj: dict, keys: tuple[str, ...], keep: int = 3) -> None:
    for key in keys:
        value = obj.get(key)
        if isinstance(value, list) and len(value) > keep:
            obj[key] = value[:keep]


def _respond(
    obj: dict,
    name: str,
    keys: tuple[str, ...],
    output: str = "inline",
    fmt: str = "json",
    out_dir: str = "",
) -> str:
    """统一返回：output=file 强制落盘；inline 超阈值自动落盘并只留预览。

    落盘目录默认是本进程的 exports/run-<pid>-<时间>/，可用 out_dir 指定其它目录；
    指定目录不参与自动清理。
    """
    from . import export

    def do_export() -> dict:
        return export.write_rows(_pick_rows(obj, keys), fmt=fmt, out_dir=out_dir, prefix=name)

    if output == "file":
        exported = do_export()
        _trim_preview(obj, keys)
        obj["export"] = exported
        return _dumps(obj)

    text = _dumps(obj)
    rows = sum(len(obj.get(k) or []) for k in keys)
    if len(text) > AUTO_EXPORT_BYTES or rows >= AUTO_EXPORT_ROWS:
        exported = do_export()
        _trim_preview(obj, keys)
        obj["auto_export"] = {
            **exported,
            "reason": "返回体过大，已自动落盘，仅保留前 3 条预览",
        }
        return _dumps(obj)
    return text


def register(mcp, get_service) -> None:
    @mcp.tool()
    def list_entities(
        scope: str = "session",
        keyword: str = "",
        since_days: int = 0,
        only_group: bool = False,
        only_unread: bool = False,
        min_members: int = 0,
        max_members: int = 0,
        sort: str = "last_time",
        limit: int = 100,
        offset: int = 0,
        output: str = "inline",
        output_dir: str = "",
        format: str = "json",
    ) -> str:
        """列出会话、联系人或群，带元信息与筛选。

        Args:
            scope: session / contact / group / all
            keyword: 匹配名称、备注、昵称、wxid
            since_days: 只返回该天数内有过消息的实体，0 表示不限
            only_group: 只看群
            only_unread: 只看有未读
            min_members: 群成员数下限
            max_members: 群成员数上限
            sort: last_time / members / name
            limit: 返回条数上限
            offset: 分页偏移
            output: inline 直接返回，file 强制落盘
            output_dir: 落盘目录，留空写本进程的 exports/run-* 目录
            format: 落盘格式 json/csv/md/html
        """
        obj = get_service().list_entities(
            scope=scope,
            keyword=keyword,
            since_days=since_days,
            only_group=only_group,
            only_unread=only_unread,
            min_members=min_members,
            max_members=max_members,
            sort=sort,
            limit=limit,
            offset=offset,
        )
        return _respond(obj, "entities", ("entities",), output, format, output_dir)

    @mcp.tool()
    def get_messages(
        targets: Optional[list[str]] = None,
        sender: str = "",
        start_date: str = "",
        end_date: str = "",
        types: Optional[list[str]] = None,
        keyword: str = "",
        order: str = "desc",
        limit: int = 200,
        cursor: str = "",
        output: str = "inline",
        format: str = "csv",
        include_media: bool = False,
        output_dir: str = "",
    ) -> str:
        """读取聊天消息，默认返回最新的一批。

        Args:
            targets: 会话名或 ID 列表，可混人与群；留空则取最近 30 天全部会话
            sender: 发言人，支持 wxid、备注或昵称
            start_date: 开始日期 YYYY-MM-DD
            end_date: 结束日期 YYYY-MM-DD
            types: 类型过滤，可选 text/image/voice/video/emoticon/file/link/mini_program/quote/system
            keyword: 正文关键词
            order: desc 最新优先（默认）或 asc
            limit: 返回条数上限，最多 1000
            cursor: 上一页返回的 next_cursor
            output: inline 直接返回，file 强制落盘
            format: 落盘格式 csv/json/md/html
            output_dir: 落盘目录，留空写本进程的 exports/run-* 目录
        """
        svc = get_service()
        obj = svc.get_messages(
            targets=targets,
            sender=sender,
            start=start_date,
            end=end_date,
            types=types,
            keyword=keyword,
            order=order,
            limit=limit,
            cursor=cursor,
            include_media=include_media,
        )
        return _respond(obj, "messages", ("messages",), output, format, output_dir)

    @mcp.tool()
    def relations(
        kind: str,
        subject: str,
        targets: Optional[list[str]] = None,
        since_date: str = "",
        until_date: str = "",
        limit: int = 500,
        output: str = "inline",
        output_dir: str = "",
        format: str = "json",
    ) -> str:
        """关系查询：群成员、共同群、某人在多群的发言、活跃统计。

        Args:
            kind: group_members（群到人）/ common_groups（人到群）/ person_activity（人在多群发言）/ activity（单会话统计）
            subject: 群名、群 ID、联系人名或 wxid
            targets: 仅 person_activity 使用，限定群清单；省略即全部群
            since_date: 开始日期 YYYY-MM-DD
            until_date: 结束日期 YYYY-MM-DD
            limit: 成员或群数量上限
            output: inline 直接返回，file 强制落盘
            output_dir: 落盘目录，留空写本进程的 exports/run-* 目录
            format: 落盘格式 json/csv/md/html
        """
        obj = get_service().relations(
            kind,
            subject,
            targets=targets,
            since=since_date,
            until=until_date,
            limit=limit,
        )
        return _respond(
            obj,
            "relations",
            ("members", "groups", "messages", "by_day"),
            output,
            format,
            output_dir,
        )

    @mcp.tool()
    def search(
        query: str,
        scope: str = "all",
        since_date: str = "",
        until_date: str = "",
        limit: int = 30,
        offset: int = 0,
        output: str = "inline",
        output_dir: str = "",
        format: str = "json",
    ) -> str:
        """一次查询同时搜索实体与消息。

        Args:
            query: 关键词，多个词为 AND
            scope: all / entity / message
            since_date: 消息时间下界 YYYY-MM-DD
            until_date: 消息时间上界 YYYY-MM-DD
            limit: 每组结果上限
            offset: 消息结果偏移，配合 next_offset 翻页
            output: inline 直接返回，file 强制落盘
            output_dir: 落盘目录，留空写本进程的 exports/run-* 目录
            format: 落盘格式 json/csv/md/html
        """
        obj = get_service().search(
            query, scope=scope, since=since_date, until=until_date, limit=limit, offset=offset
        )
        return _respond(obj, "search", ("messages", "entities"), output, format, output_dir)

    @mcp.tool()
    def sync(action: str = "status", force: bool = True) -> str:
        """查看数据新鲜度，或立即刷新解密数据与索引。

        其余查询工具会自动按 TTL（默认 60 秒）刷新，无需手动调用；
        需要绕过 TTL 立刻拉最新时用本工具。

        Args:
            action: status 只看状态，sync 执行刷新
            force: 忽略 TTL，默认 True；传 False 则受 TTL 约束
        """
        svc = get_service()
        if action == "sync":
            return _dumps(svc.refresh(force=force))
        return _dumps(svc.status())

    @mcp.tool()
    def list_moments(
        author: str = "",
        keyword: str = "",
        since_date: str = "",
        until_date: str = "",
        limit: int = 20,
        offset: int = 0,
        with_interactions: bool = True,
        output: str = "inline",
        output_dir: str = "",
        format: str = "json",
    ) -> str:
        """查朋友圈动态，可带评论与点赞。

        Args:
            author: 只看某人的朋友圈，留空为全部（含自己）
            keyword: 正文关键词
            since_date: 开始日期 YYYY-MM-DD
            until_date: 结束日期 YYYY-MM-DD
            limit: 返回条数上限
            offset: 分页偏移
            with_interactions: 是否附带评论与点赞
            output: inline 直接返回，file 强制落盘
            output_dir: 落盘目录，留空写本进程的 exports/run-* 目录
            format: 落盘格式 json/csv/md/html
        """
        obj = get_service().list_moments(
            author=author,
            keyword=keyword,
            since=since_date,
            until=until_date,
            limit=limit,
            offset=offset,
            with_interactions=with_interactions,
        )
        return _respond(obj, "moments", ("moments",), output, format, output_dir)

    @mcp.tool()
    def list_favorites(
        type: str = "",
        keyword: str = "",
        tag: str = "",
        since_date: str = "",
        until_date: str = "",
        limit: int = 20,
        offset: int = 0,
        output: str = "inline",
        output_dir: str = "",
        format: str = "json",
    ) -> str:
        """查收藏条目。

        Args:
            type: text/image/voice/video/link/file/chat_record/note，留空为全部
            keyword: 标题与描述关键词
            tag: 收藏标签名
            since_date: 收藏时间下界 YYYY-MM-DD
            until_date: 收藏时间上界 YYYY-MM-DD
            limit: 返回条数上限
            offset: 分页偏移
            output: inline 直接返回，file 强制落盘
            output_dir: 落盘目录，留空写本进程的 exports/run-* 目录
            format: 落盘格式 json/csv/md/html
        """
        obj = get_service().list_favorites(
            type=type,
            keyword=keyword,
            tag=tag,
            since=since_date,
            until=until_date,
            limit=limit,
            offset=offset,
        )
        return _respond(obj, "favorites", ("items",), output, format, output_dir)

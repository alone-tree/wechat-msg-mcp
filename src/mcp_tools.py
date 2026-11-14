"""MCP 工具注册：七个工具，全部委托 service 层。

依据 docs/重构方案-2026-11-13.md 第 6 与 20 节。
工具只做参数转换、导出判定与 JSON 序列化，业务逻辑在 src/service.py。
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Optional

AUTO_EXPORT_ROWS = 200
AUTO_EXPORT_BYTES = 80000
# 单次返回体上限：内联图片会让响应膨胀，超了宿主会直接断连接，需要重新 load。
MAX_RESPONSE_BYTES = 10 * 1024 * 1024
# 图片原始字节预算：base64 传输膨胀约 4/3，6MB 原始字节约合 8MB 传输体积。
INLINE_IMAGE_BUDGET = 6_000_000
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


FILE_EXPORT_MAX_BYTES = 100 * 1024 * 1024
FILE_EXPORT_MAX_COUNT = 20


def _export_files(obj: dict, target) -> dict:
    """把消息里已定位到的文件（PDF、表格、文档等）复制到导出目录。

    AI 拿到的是副本路径，可直接读；源文件仍在微信目录。
    """
    from . import media_files

    msgs = obj.get("messages") or []
    done = skipped = 0
    total = 0
    for m in msgs:
        media = m.get("media")
        if not isinstance(media, dict) or media.get("kind") != "file":
            continue
        if not media.get("path") or not media.get("exists"):
            continue
        if done >= FILE_EXPORT_MAX_COUNT:
            skipped += 1
            continue
        new = media_files.export_copy(media, Path(target), FILE_EXPORT_MAX_BYTES)
        if new is not None and new.get("exported"):
            m["media"] = new
            done += 1
            total += int(new.get("size") or 0)
        else:
            if new is not None:
                m["media"] = new
            skipped += 1
    return {"exported": done, "skipped": skipped, "dir": str(target), "bytes": total}


def _export_videos(obj: dict, target) -> dict:
    """把视频原始文件复制到导出目录（仅 extract_media 时用）。"""
    from . import media_files

    done = skipped = 0
    for m in obj.get("messages") or []:
        media = m.get("media")
        if not isinstance(media, dict) or media.get("kind") != "video":
            continue
        vp = media.get("video_path")
        if not vp:
            continue
        info = {
            "kind": "video",
            "name": Path(vp).name,
            "size": int(media.get("video_size") or 0),
            "path": vp,
            "exists": True,
        }
        new = media_files.export_copy(info, Path(target), FILE_EXPORT_MAX_BYTES)
        if new is not None and new.get("exported"):
            media["video_source_path"] = vp
            media["video_path"] = new["path"]
            done += 1
        else:
            skipped += 1
    return {"exported": done, "skipped": skipped, "dir": str(target)}


def _save_images(obj: dict, images: list, src, target=None) -> int:
    """把图片字节全部落盘，并把路径回填到对应消息的 media。返回落盘张数。"""
    if not images:
        return 0
    from . import export, media_inline

    out_dir = Path(target) if target else export.run_dir() / "media"
    by_id = {m.get("local_id"): m for m in obj.get("messages") or []}
    saved = 0
    for img in images:
        if img.get("kind") == "voice":
            info = media_inline.save_voice(img["bytes"], Path(target).parent / "voice" if target else export.run_dir() / "voice")
        else:
            info = media_inline.save_jpeg(src, img["bytes"], img.get("kind", "image"), out_dir)
        m = by_id.get(img.get("local_id"))
        if m is not None and isinstance(m.get("media"), dict):
            m["media"]["path"] = info["path"]
        saved += 1
    return saved


def _inline_images(obj: dict, images: list, src) -> tuple[list, dict]:
    """按体积预算内联图片。超预算的不内联，落盘后给路径。"""
    from mcp.server.fastmcp.utilities.types import Image

    from . import media_inline

    by_id = {m.get("local_id"): m for m in obj.get("messages") or []}
    blocks: list = []
    used = 0
    overflow: list = []
    for img in images:
        if img.get("kind") == "voice":
            continue
        size = len(img["bytes"])
        if used + size <= INLINE_IMAGE_BUDGET:
            used += size
            blocks.append(Image(data=img["bytes"], format="jpeg"))
            m = by_id.get(img.get("local_id"))
            if m is not None and isinstance(m.get("media"), dict):
                m["media"]["inlined"] = True
        else:
            overflow.append(img)

    if overflow:
        from . import export

        target = export.run_dir() / "media"
        for img in overflow:
            info = media_inline.save_jpeg(src, img["bytes"], img.get("kind", "image"), target)
            m = by_id.get(img.get("local_id"))
            if m is not None and isinstance(m.get("media"), dict):
                m["media"]["inlined"] = False
                m["media"]["path"] = info["path"]

    stats = {
        "inlined": len(blocks),
        "inlined_bytes": used,
        "not_inlined": len(overflow),
        "image_budget_bytes": INLINE_IMAGE_BUDGET,
    }
    if overflow:
        stats["note"] = (
            f"另有 {len(overflow)} 张图片因返回体体积上限未内联，"
            "已保存为本地文件，media.path 可直接读取"
        )
    return blocks, stats


def register(mcp, get_service) -> None:
    @mcp.tool()
    def system(action: str = "start", force: bool = True) -> str:
        """系统入口：启动引导、看数据状态、刷新数据，三合一的固定动作。

        Args:
            action: start 返回完整使用说明与当前状态（新会话先调这个）；
                status 只看数据新鲜度；sync 立即增量刷新解密数据与索引
            force: 仅 action=sync 时生效，默认 True 忽略 TTL

        平台无关：任何 MCP 客户端都能调，不依赖宿主自身的引导能力。
        其余六个工具是查询类，会自己按 TTL（默认 60 秒）增量刷新。
        """
        svc = get_service()
        if action not in ("start", "status", "sync"):
            return _dumps(
                {
                    "error": f"未知 action: {action}",
                    "valid": ["start", "status", "sync"],
                    "hint": "写错动作不会自动回退到 start",
                }
            )
        if action == "sync":
            return _dumps(svc.refresh(force=force))

        st = svc.status()
        if action == "status":
            return _dumps(st)

        from .guide import GUIDE

        idx = st.get("index") or {}
        return _dumps(
            {
                "server": "wechat-msg-mcp",
                "intro": "本机微信聊天记录只读查询，数据不出本机。",
                "guide": GUIDE,
                "status": {
                    "state": st.get("state"),
                    "data_cutoff": st.get("data_cutoff"),
                    "message_dbs": st.get("message_dbs"),
                    "contacts": st.get("contacts"),
                    "groups": st.get("groups"),
                    "index_rows": idx.get("rows"),
                    "index_size_mb": idx.get("size_mb"),
                },
                "next": "直接调用其余六个查询工具；要立刻拿最新数据调 system(action=\"sync\")。",
            }
        )

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
        include_media: bool = True,
        output_dir: str = "",
        extract_media: str = "",
    ) -> list:
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
            include_media: 默认开。图片、表情、视频封面直接内联返回，语音给转写文字；
                返回条数超过 50 条时自动跳过媒体提取（那种量级会被截成预览）
            output_dir: 结果落盘目录，留空写本进程的 exports/run-* 目录
            extract_media: 额外把媒体原文件存到文件；留空不存，auto 存到本进程导出目录，也可传绝对目录

        媒体处理会比纯文本查询慢：需要看图时再用 include_media，并尽量配合 types
        缩小范围（例如 types=["image"]），不要在一次大批量取数里同时开它。
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
        from . import export as export_mod

        files = _export_files(obj, export_mod.run_dir() / "files")
        if files["exported"] or files["skipped"]:
            obj["file_export"] = files
        images = obj.pop("_images", None) or []

        if extract_media and images:
            from . import export as export_mod

            target = export_mod.run_dir() / "media" if extract_media == "auto" else Path(extract_media) / "media"
            saved = _save_images(obj, images, svc.src, target)
            obj["media_extract"] = {"saved": saved, "dir": str(target)}

        if extract_media:
            from . import export as export_mod

            vtarget = export_mod.run_dir() / "video" if extract_media == "auto" else Path(extract_media) / "video"
            vres = _export_videos(obj, vtarget)
            if vres["exported"] or vres["skipped"]:
                obj.setdefault("media_extract", {})["video"] = vres

        if not images:
            return _respond(obj, "messages", ("messages",), output, format, output_dir)

        inline_text = _dumps(obj)
        need_export = (
            output == "file"
            or len(inline_text) > AUTO_EXPORT_BYTES
            or obj.get("returned", 0) >= AUTO_EXPORT_ROWS
        )
        if need_export:
            saved = _save_images(obj, images, svc.src)
            obj["media_inline"] = {"inlined": 0, "saved": saved, "reason": "结果已落盘，图片同时保存为文件"}
            return _respond(obj, "messages", ("messages",), output, format, output_dir)

        blocks, stats = _inline_images(obj, images, svc.src)
        obj["media_inline"] = stats
        return [_dumps(obj), *blocks]

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

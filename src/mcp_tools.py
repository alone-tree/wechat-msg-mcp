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
# 图片原始字节预算：base64 传输膨胀约 4/3，6MB 原始字节约合 8MB 传输体积。
INLINE_IMAGE_BUDGET = 6_000_000
EXPORT_KEYS = ("messages", "entities", "items", "moments", "members", "groups", "by_day")
# 这些是信封的说明性字段，不当作数据集合另存
_ENVELOPE_NOISE = {
    "unresolved",
    "warnings",
    "hints",
    "ambiguous",
    "media_missing",
    "media_inline",
    "media_extract",
    "file_export",
    "export",
    "auto_export",
    "refresh",
    "self",
    "senders",
    "counts",
    "total",
    "query",
    "kind",
    "subject",
    "person",
    "group",
    "chat",
    "scope",
    "basis",
    "data_until",
    "detail",
    "order",
    "chat_name",
}


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
    kind: str = "rows",
) -> str:
    """统一返回：output=file 强制落盘；inline 超阈值自动落盘并只留预览。

    落盘目录默认是本进程的 exports/run-<pid>-<时间>/，可用 out_dir 指定其它目录；
    指定目录不参与自动清理。
    """
    from . import export

    def do_export() -> dict:
        result = export.write_rows(
            _pick_rows(obj, keys), fmt=fmt, out_dir=out_dir, prefix=name, kind=kind, envelope=obj
        )
        # 主表之外还有别的集合（例如 activity 的 by_hour、by_type、top_senders）时，
        # 另存一份 JSON 旁挂，否则那些数据只在预览里留 3 条就丢了
        extra = {
            k: v
            for k, v in obj.items()
            if k not in keys
            and k not in _ENVELOPE_NOISE
            and not k.startswith("_")
            and isinstance(v, (list, dict))
            and v
        }
        if extra:
            side = export.write_json(extra, out_dir=out_dir, prefix=name + "-extra")
            result["extra"] = side["path"]
            result["extra_collections"] = sorted(extra)
        return result

    if output == "file":
        exported = do_export()
        _trim_preview(obj, keys)
        exported["is_partial"] = bool(obj.get("has_more"))
        obj["export"] = exported
        return _dumps(obj)

    text = _dumps(obj)
    rows = sum(len(obj.get(k) or []) for k in keys)
    if len(text) > AUTO_EXPORT_BYTES or rows >= AUTO_EXPORT_ROWS:
        exported = do_export()
        _trim_preview(obj, keys)
        obj["auto_export"] = {
            **exported,
            "is_partial": bool(obj.get("has_more")),
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


def _path_key(kind: str) -> str:
    """媒体本地路径挂在哪个键上：视频的 mp4 才是主件，封面单独一个键。"""
    return "cover_path" if kind == "video" else "path"


def _export_videos(obj: dict, target) -> dict:
    """把视频原始文件复制到导出目录（仅 extract_media 时用）。"""
    from . import media_files

    done = skipped = 0
    for m in obj.get("messages") or []:
        media = m.get("media")
        if not isinstance(media, dict) or media.get("kind") != "video":
            continue
        path = media.get("path")
        if not path or not media.get("exists"):
            skipped += 1
            continue
        info = {
            "kind": "video",
            "name": Path(path).name,
            "size": int(media.get("size") or 0),
            "path": path,
            "exists": True,
        }
        new = media_files.export_copy(info, Path(target), FILE_EXPORT_MAX_BYTES)
        if new is not None and new.get("exported"):
            media["source_path"] = new.get("source_path", "")
            media["path"] = new["path"]
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
    by_id = {m.get("id", m.get("local_id")): m for m in obj.get("messages") or []}
    saved = 0
    for img in images:
        if img.get("kind") == "voice":
            info = media_inline.save_voice(img["bytes"], Path(target).parent / "voice" if target else export.run_dir() / "voice")
        else:
            info = media_inline.save_jpeg(src, img["bytes"], img.get("kind", "image"), out_dir)
        m = by_id.get(img.get("local_id"))
        if m is not None and isinstance(m.get("media"), dict):
            m["media"][_path_key(img.get("kind") or "image")] = info["path"]
        saved += 1
    return saved


def _tiles_for(
    data: bytes, tile_height: int = 1600, max_tiles: int = 10, overlap: int = 80
) -> list:
    """超长截图切成竖条，否则客户端把整图缩到 1600 高后文字完全看不清。

    相邻两段重叠 overlap 像素：切缝正好落在一行字上时，上下两段各能看到完整的这一行，
    代价是每段边界重复一行，读的时候跳过即可。
    只在长宽比超过 3 且高度超过一个 tile 时切；普通图片返回空列表。
    """
    import io

    try:
        from PIL import Image as PILImage
    except ImportError:
        return []
    try:
        with PILImage.open(io.BytesIO(data)) as im:
            im.load()
            w, h = im.size
            if w <= 0 or h <= tile_height or h < 3 * w:
                return []
            overlap = max(0, min(overlap, tile_height // 4))
            step = tile_height - overlap
            n = -(-(h - overlap) // step)
            if n > max_tiles:
                # 段数超上限时拉大跨步，宁可每段更长也不丢内容
                step = -(-(h - tile_height) // max(1, max_tiles - 1))
                n = -(-(h - tile_height) // step) + 1 if h > tile_height else 1
                n = min(max_tiles, n)
            out = []
            top = 0
            while top < h and len(out) < max_tiles:
                bottom = min(h, top + tile_height)
                if bottom - top < 40:
                    break
                tile = im.crop((0, top, w, bottom))
                buf = io.BytesIO()
                tile.convert("RGB").save(buf, format="JPEG", quality=85)
                out.append(buf.getvalue())
                if bottom >= h:
                    break
                top += step
    except Exception:
        return []
    return out if len(out) > 1 else []


def _inline_images(obj: dict, images: list, src) -> tuple[list, dict]:
    """按体积预算内联图片。超预算的不内联，落盘后给路径。"""
    from mcp.server.fastmcp.utilities.types import Image

    from . import media_inline

    by_id = {m.get("id", m.get("local_id")): m for m in obj.get("messages") or []}
    blocks: list = []
    used = 0
    overflow: list = []
    for img in images:
        if img.get("kind") == "voice":
            continue
        size = len(img["bytes"])
        tiles = _tiles_for(img["bytes"]) if img.get("kind") != "voice" else []
        cost = sum(len(x) for x in tiles) if tiles else size
        if used + cost <= INLINE_IMAGE_BUDGET:
            used += cost
            for tile in tiles or [img["bytes"]]:
                blocks.append(Image(data=tile, format="jpeg"))
            m = by_id.get(img.get("local_id"))
            if m is not None and isinstance(m.get("media"), dict):
                m["media"]["inlined"] = True
                if tiles:
                    from . import export

                    info = media_inline.save_jpeg(
                        src, img["bytes"], "full", export.run_dir() / "media"
                    )
                    m["media"]["tiles"] = len(tiles)
                    m["media"][_path_key(img.get("kind") or "image")] = info["path"]
                    m["media"]["note"] = (
                        f"长截图已切成 {len(tiles)} 段按顺序返回，相邻两段有约 80 像素重叠，"
                        "重复的一两行属正常；整图另存 media.path 便于核对"
                    )
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
                m["media"][_path_key(img.get("kind") or "image")] = info["path"]

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


def _export_messages_full(svc, kwargs: dict, out_dir: str, fmt: str) -> dict:
    """output=file：翻页取满再落盘，实现见 Service.export_messages。"""
    return svc.export_messages(out_dir=out_dir, fmt=fmt, **kwargs)

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
                    "ok": False,
                    "error": {
                        "code": "unknown_action",
                        "message": f"未知 action: {action}",
                        "candidates": ["start", "status", "sync"],
                    },
                    "hint": "写错动作不会自动回退到 start",
                }
            )
        if action == "sync":
            return _dumps(svc.refresh(force=force))

        st = svc.status()
        if action == "status":
            return _dumps(st)

        from .config import get_account_wxid
        from .guide import GUIDE
        from .messages import _self_info

        idx = st.get("index") or {}
        return _dumps(
            {
                "server": "wechat-msg-mcp",
                "intro": "本机微信聊天记录只读查询，数据不出本机。",
                "self": _self_info(get_account_wxid(), None, svc.src),
                "guide": GUIDE,
                "status": {
                    "state": st.get("state"),
                    "data_until": st.get("data_until"),
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
        detail: str = "lite",
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
            detail: lite 只给常用字段（默认）；full 追加地区、性别、说明、群公告等
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
            detail=detail,
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
        detail: str = "lite",
        per_target_limit: int = 0,
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
            types: 类型过滤，可选 text/image/voice/video/emoticon/file/link/mini_program/
                quote/chat_record/channels/transfer/red_packet/location/card/voip/system
            keyword: 正文关键词
            order: desc 最新优先（默认）或 asc
            limit: 返回条数上限，最多 1000
            cursor: 上一页返回的 next
            detail: lite 只给 id/time/from/text/media（默认）；full 追加
                sender_id/type/chat_id/chat_name/senders；raw 追加 local_type 与原文
            per_target_limit: 多会话查询时每个会话最多取多少条，0 表示不单独限制；
                截断会在 warnings 里点名
            output: inline 直接返回，file 翻页取满后落盘
            format: 落盘格式 csv/json/ndjson/md/text/html
            include_media: 默认开。图片、表情、视频封面直接内联返回，语音给转写文字；
                返回条数超过 50 条时自动跳过媒体提取（那种量级会被截成预览）
            output_dir: 结果落盘目录，留空写本进程的 exports/run-* 目录
            extract_media: 额外把媒体原文件存到文件；留空不存，auto 存到本进程导出目录，也可传绝对目录

        消息不带收发方向：from 为「我」就是本人发的，系统消息 from 为空。
        媒体处理会比纯文本查询慢：要看图时再用 include_media，并尽量配合 types
        缩小范围（例如 types=["image"]），不要在一次大批量取数里同时开它。
        """
        svc = get_service()
        kwargs = dict(
            targets=targets,
            sender=sender,
            start=start_date,
            end=end_date,
            types=types,
            keyword=keyword,
            order=order,
            limit=limit,
            cursor=cursor,
            per_target_limit=per_target_limit,
            include_media=include_media,
            detail=detail,
        )
        obj = svc.get_messages(**kwargs)
        if not obj.get("ok", True):
            return _dumps(obj)
        images = obj.pop("_images", None) or []

        if output == "file":
            exported = _export_messages_full(svc, kwargs, output_dir, format)
            _trim_preview(obj, ("messages",), 3)
            obj["export"] = exported
            obj.setdefault("warnings", []).append(
                "output=file 只导出文本；要看图请单独查并传 include_media 或 extract_media"
            )
            return _dumps(obj)

        from . import export as export_mod

        files = _export_files(obj, export_mod.run_dir() / "files")
        if files["exported"] or files["skipped"]:
            obj["file_export"] = files

        if extract_media and images:
            target = export_mod.run_dir() / "media" if extract_media == "auto" else Path(extract_media) / "media"
            saved = _save_images(obj, images, svc.src, target)
            obj["media_extract"] = {"saved": saved, "dir": str(target)}
        if extract_media:
            vtarget = export_mod.run_dir() / "video" if extract_media == "auto" else Path(extract_media) / "video"
            vres = _export_videos(obj, vtarget)
            if vres["exported"] or vres["skipped"]:
                obj.setdefault("media_extract", {})["video"] = vres

        if not images:
            return _respond(obj, "messages", ("messages",), output, format, output_dir, kind="messages")

        inline_text = _dumps(obj)
        need_export = len(inline_text) > AUTO_EXPORT_BYTES or obj.get("returned", 0) >= AUTO_EXPORT_ROWS
        if need_export:
            saved = _save_images(obj, images, svc.src)
            obj["media_inline"] = {"inlined": 0, "saved": saved, "reason": "结果已落盘，图片同时保存为文件"}
            return _respond(obj, "messages", ("messages",), output, format, output_dir, kind="messages")

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
        offset: int = 0,
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
            limit: 成员、群或发言人数上限
            offset: 分页偏移，配合 next 翻页（group_members、common_groups、person_activity）
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
            offset=offset,
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
            offset: 消息结果偏移，配合 next 翻页
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

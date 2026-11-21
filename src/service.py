"""service 门面：MCP 工具与 CLI 共用的唯一入口。

依据 docs/重构方案-2026-11-13.md 第 4 节：MCP 与 CLI 都是薄壳，
参数校验、数据源选择与错误语义都在这里。
"""
from __future__ import annotations

import os
from collections import Counter
from pathlib import Path
from typing import Optional

from . import entities, freshness, index, messages, relations, search as search_mod, sync as sync_mod
from .db.locator import LocalSource


class Service:
    def __init__(self, root: Optional[str] = None, auto_refresh: bool = True):
        from .config import get_decrypted_dir

        self.src = LocalSource(Path(root) if root else get_decrypted_dir())
        self.auto_refresh = auto_refresh

    def _refresh_snapshot_guard(self) -> Optional[dict]:
        from .config import DEFAULT_DECRYPTED_DIR, PROJECT_ROOT

        root = self.src.root.resolve()
        default = DEFAULT_DECRYPTED_DIR.resolve()
        if root == default:
            return None
        protected = (PROJECT_ROOT / "exports", default / "exports")
        if (os.environ.get("WECHAT_REFRESH_CUSTOM_ROOT") != "1"
                or any(root.is_relative_to(p.resolve()) for p in protected)):
            return {"refreshed": False, "skipped": "snapshot_root",
                    "error": "当前根目录按只读副本处理，未从真实微信刷新数据"}
        return None

    def _fresh(self, force: bool = False) -> dict:
        """查询前的按需刷新，失败不阻断查询。"""
        if not self.auto_refresh:
            return {"refreshed": False, "skipped": "disabled"}
        blocked = self._refresh_snapshot_guard()
        return blocked if blocked else freshness.ensure(self.src, force=force)

    def ensure_fresh(self, force: bool = False) -> dict:
        """对外暴露的刷新入口，供 CLI 的 --fresh 与 sync 工具使用。"""
        return self._fresh(force)

    def _query(self, read, *args, **kwargs) -> dict:
        refresh = self._fresh()
        result = read(self.src, *args, **kwargs)
        result["refresh"] = refresh
        return result

    # 实体
    def list_entities(self, **kwargs) -> dict:
        return self._query(entities.list_entities, **kwargs)

    def resolve(self, name: str, limit: int = 5) -> dict:
        return entities.resolve_name(self.src, name, limit=limit)

    def groups_by_members(self, members: list[str], limit: int = 20, require_all: bool = True) -> dict:
        """按成员组合找群，CLI 的 groups 子命令用。"""
        return self._query(entities.groups_by_members, members, limit=limit, require_all=require_all)

    # 消息
    def get_messages(self, **kwargs) -> dict:
        return self._query(messages.list_messages, **kwargs)

    def export_messages(
        self,
        out_dir: str = "",
        fmt: str = "csv",
        with_media: bool = True,
        media_limit: int = 500,
        **kwargs,
    ) -> dict:
        """导出：每张会话表只扫一遍再落盘，几十万条的大群也不会越翻越慢。

        with_media 默认开：图片、语音、视频封面与本体、文件消息都会落成真实文件，
        并把副本路径写进导出的 media.path（导出不内联，AI 拿不到字节）。
        """
        from . import export

        limit_rows = int(kwargs.pop("max_rows", 200000))
        out_dir = str(Path(out_dir).resolve()) if out_dir else ""
        refresh = self._fresh()
        media_root = ""
        if with_media:
            base = Path(out_dir) if out_dir else export.run_dir()
            media_root = str(base / "media-export")
        detail = str(kwargs.get("detail") or "full").lower()
        if detail == "lite":
            # 导出要能分辨会话与类型，lite 不带 chat_name 与 type
            detail = "full"
        query = {
            "sender": kwargs.get("sender", ""),
            "start": kwargs.get("start", ""),
            "end": kwargs.get("end", ""),
            "types": kwargs.get("types"),
            "keyword": kwargs.get("keyword", ""),
            "order": kwargs.get("order") or "asc",
            "detail": detail,
        }
        try:
            stream, meta = messages.export_stream(
                self.src, targets=kwargs.get("targets"), **query,
                media_dir=media_root, media_limit=media_limit, max_rows=limit_rows,
            )
        except ValueError as exc:
            return {
                "ok": False,
                "error": {"code": "bad_argument", "message": str(exc)},
            }
        if not meta["data_until"]:
            return {
                "ok": False, "error": {"code": "no_message_db", "message": "本地没有可读的消息分库"},
                "refresh": refresh,
            }
        rows = list(stream)
        partial = meta["is_partial"]
        requests = meta["requests"]
        read_errors = meta["read_errors"]
        coverage = {
            "scope": "explicit_targets" if requests else "local_messages",
            "is_complete": not (partial or meta["unresolved"] or read_errors),
            "requested": len(requests),
            "resolved_unique": len({r["chat_id"] for r in requests if r["status"] != "unresolved"}),
            "by_status": dict(Counter(r["status"] for r in requests)),
            "read_errors": len(read_errors),
            "query": {**query, "with_media": with_media, "max_rows": limit_rows},
        }
        envelope = {
            "ok": True,
            "detail": meta["detail"],
            "total": len(rows),
            "returned": len(rows),
            "has_more": partial,
            "unresolved": meta.get("unresolved") or [],
            "data_until": meta.get("data_until"),
        }
        result = export.write_rows(
            rows, fmt=fmt, out_dir=out_dir, prefix="messages", kind="messages", envelope=envelope
        )
        receipt = export.write_json(
            {**coverage, "targets": requests, "unresolved": meta["unresolved"], "read_errors": read_errors,
             "data_until": meta["data_until"], "refresh": refresh, "files": result["files"]},
            out_dir=out_dir, prefix="messages-coverage",
        )
        result.update(
            {
                "ok": True,
                "is_partial": partial,
                "basis": "单遍顺序流式导出",
                "tables": meta["tables"],
                "unresolved": meta["unresolved"],
                "data_until": meta["data_until"],
                "refresh": refresh,
                "coverage": {**coverage, "path": receipt["path"]},
            }
        )
        if not coverage["is_complete"] and not partial:
            result["note"] = "有目标未解析或读取失败；零行不代表未发言，见 coverage.path"
        media = meta.get("media") or {}
        if media_root and media.get("saved"):
            result["media_export"] = {
                "dir": media.get("dir"),
                "images": media.get("images"),
                "voice": media.get("voice"),
                "video_covers": media.get("video"),
                "video_files": media.get("video_files"),
                "files": media.get("files"),
                "saved": media.get("saved"),
                "skipped": media.get("skipped"),
                "missing": media.get("missing") or {},
                "limit": media.get("limit_display", media.get("limit")),
            }
        elif media_root:
            result["media_export"] = {
                "dir": media.get("dir"),
                "saved": 0,
                "skipped": media.get("skipped"),
                "missing": media.get("missing") or {},
                "limit": media.get("limit_display", media.get("limit")),
                "note": "本次导出的数据里没有可落盘的媒体",
            }
        if partial:
            result["note"] = f"达到 {limit_rows} 行上限，剩余未导出，可用时间段或 targets 分批"
        return result

    def relations(self, kind: str, subject: str, **kwargs) -> dict:
        offset = kwargs.get("offset", 0) or 0
        refresh = self._fresh()
        if kind == "group_members":
            result = relations.group_members(
                self.src, subject, limit=kwargs.get("limit", 500), offset=offset
            )
        elif kind == "common_groups":
            result = relations.common_groups(
                self.src, subject, limit=kwargs.get("limit", 200), offset=offset
            )
        elif kind == "person_activity":
            result = relations.person_activity(
                self.src,
                subject,
                targets=kwargs.get("targets"),
                since=kwargs.get("since", ""),
                until=kwargs.get("until", ""),
                limit=kwargs.get("limit", 200),
                offset=offset,
            )
        elif kind == "activity":
            result = relations.activity(
                self.src,
                subject,
                since=kwargs.get("since", ""),
                until=kwargs.get("until", ""),
                limit=kwargs.get("limit", 10),
            )
        else:
            result = {"error": f"未知关系类型: {kind}"}
        result["refresh"] = refresh
        return result

    # 搜索
    def search(self, query: str, **kwargs) -> dict:
        return self._query(search_mod.search, query, **kwargs)

    # 朋友圈与收藏
    def list_moments(self, **kwargs) -> dict:
        from .moments import list_moments

        return self._query(list_moments, **kwargs)

    def list_favorites(self, **kwargs) -> dict:
        from .favorites import list_favorites

        return self._query(list_favorites, **kwargs)

    # 同步与索引
    def status(self) -> dict:
        info = {"ok": True}
        info.update(sync_mod.status(self.src))
        info["index"] = index.count(self.src)
        info["refresh"] = {
            "ttl_seconds": freshness.ttl_seconds(),
            "ttl_left": freshness.peek_ttl_left(self.src),
            "last": freshness._read_state(self.src),
        }
        return info

    def refresh(self, force: bool = True) -> dict:
        blocked = self._refresh_snapshot_guard()
        result = blocked if blocked else freshness.ensure(self.src, force=force, update_index=True)
        if not isinstance(result, dict):
            return {"ok": False, "error": {"code": "refresh_failed", "message": "刷新没有返回结果"}}
        failed = result.get("skipped") in ("error", "no_source", "no_keys", "snapshot_root")
        result["ok"] = not failed
        if failed and result.get("error"):
            result["error"] = {
                "code": str(result.get("skipped") or "refresh_failed"),
                "message": str(result["error"]),
            }
        return result

    def build_index(self, rebuild: bool = False) -> dict:
        return index.build(self.src, rebuild=rebuild)

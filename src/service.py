"""service 门面：MCP 工具与 CLI 共用的唯一入口。

依据 docs/重构方案-2026-11-13.md 第 4 节：MCP 与 CLI 都是薄壳，
参数校验、数据源选择与错误语义都在这里。
"""
from __future__ import annotations

from pathlib import Path
from typing import Optional

from . import entities, freshness, index, messages, relations, search as search_mod, sync as sync_mod
from .db.locator import LocalSource


class Service:
    def __init__(self, root: Optional[str] = None, auto_refresh: bool = True):
        from .config import get_decrypted_dir

        self.src = LocalSource(Path(root) if root else get_decrypted_dir())
        self.auto_refresh = auto_refresh

    def _fresh(self, force: bool = False) -> dict:
        """查询前的按需刷新，失败不阻断查询。"""
        if not self.auto_refresh:
            return {"refreshed": False, "skipped": "disabled"}
        return freshness.ensure(self.src, force=force)

    def ensure_fresh(self, force: bool = False) -> dict:
        """对外暴露的刷新入口，供 CLI 的 --fresh 与 sync 工具使用。"""
        return self._fresh(force)

    def _with_fresh(self, result: dict, force: bool = False) -> dict:
        if isinstance(result, dict):
            result["refresh"] = self._fresh(force)
        return result

    # 实体
    def list_entities(self, **kwargs) -> dict:
        return self._with_fresh(entities.list_entities(self.src, **kwargs))

    def resolve(self, name: str, limit: int = 5) -> dict:
        return entities.resolve_name(self.src, name, limit=limit)

    def groups_by_members(self, members: list[str], limit: int = 20, require_all: bool = True) -> dict:
        """按成员组合找群，CLI 的 groups 子命令用。"""
        return self._with_fresh(
            entities.groups_by_members(self.src, members, limit=limit, require_all=require_all)
        )

    # 消息
    def get_messages(self, **kwargs) -> dict:
        return self._with_fresh(messages.list_messages(self.src, **kwargs))

    def export_messages(self, out_dir: str = "", fmt: str = "csv", **kwargs) -> dict:
        """按游标翻页取满再落盘，不只导当前页。CLI 与 MCP 共用这一份。"""
        from . import export

        base = dict(kwargs)
        base["include_media"] = False
        if str(base.get("detail") or "lite") == "lite":
            base["detail"] = "full"
        rows: list = []
        cursor = ""
        pages = 0
        envelope: dict = {}
        partial = False
        while True:
            obj = self.get_messages(**{**base, "limit": 1000, "cursor": cursor})
            if not obj.get("ok", True):
                return obj
            if not envelope:
                envelope = {k: v for k, v in obj.items() if k not in ("messages", "_images")}
            rows.extend(obj.get("messages") or [])
            pages += 1
            cursor = obj.get("next") or ""
            partial = bool(obj.get("has_more"))
            if not partial or not cursor or pages >= 200 or len(rows) >= 200000:
                break
        result = export.write_rows(
            rows, fmt=fmt, out_dir=out_dir, prefix="messages", kind="messages", envelope=envelope
        )
        result.update({"pages": pages, "is_partial": partial})
        return result

    # 关系
    def relations(self, kind: str, subject: str, **kwargs) -> dict:
        if kind == "group_members":
            result = relations.group_members(self.src, subject, limit=kwargs.get("limit", 500))
        elif kind == "common_groups":
            result = relations.common_groups(self.src, subject, limit=kwargs.get("limit", 200))
        elif kind == "person_activity":
            result = relations.person_activity(
                self.src,
                subject,
                targets=kwargs.get("targets"),
                since=kwargs.get("since", ""),
                until=kwargs.get("until", ""),
                limit=kwargs.get("limit", 200),
            )
        elif kind == "activity":
            result = relations.activity(
                self.src, subject, since=kwargs.get("since", ""), until=kwargs.get("until", "")
            )
        else:
            result = {"error": f"未知关系类型: {kind}"}
        return self._with_fresh(result)

    # 搜索
    def search(self, query: str, **kwargs) -> dict:
        return self._with_fresh(search_mod.search(self.src, query, **kwargs))

    # 朋友圈与收藏
    def list_moments(self, **kwargs) -> dict:
        from .moments import list_moments

        return self._with_fresh(list_moments(self.src, **kwargs))

    def list_favorites(self, **kwargs) -> dict:
        from .favorites import list_favorites

        return self._with_fresh(list_favorites(self.src, **kwargs))

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
        result = freshness.ensure(self.src, force=force, update_index=True)
        if not isinstance(result, dict):
            return {"ok": False, "error": {"code": "refresh_failed", "message": "刷新没有返回结果"}}
        failed = result.get("skipped") in ("error", "no_source", "no_keys")
        result["ok"] = not failed
        if failed and result.get("error"):
            result["error"] = {
                "code": str(result.get("skipped") or "refresh_failed"),
                "message": str(result["error"]),
            }
        return result

    def build_index(self, rebuild: bool = False) -> dict:
        return index.build(self.src, rebuild=rebuild)

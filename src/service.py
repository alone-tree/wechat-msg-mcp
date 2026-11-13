"""service 门面：MCP 工具与 CLI 共用的唯一入口。

依据 docs/重构方案-2026-11-13.md 第 4 节：MCP 与 CLI 都是薄壳，
参数校验、数据源选择与错误语义都在这里。
"""
from __future__ import annotations

from pathlib import Path
from typing import Optional

from . import entities, index, messages, relations, search as search_mod, sync as sync_mod
from .db.locator import LocalSource


class Service:
    def __init__(self, root: Optional[str] = None):
        from .config import get_decrypted_dir

        self.src = LocalSource(Path(root) if root else get_decrypted_dir())

    # 实体
    def list_entities(self, **kwargs) -> dict:
        return entities.list_entities(self.src, **kwargs)

    def resolve(self, name: str, limit: int = 5) -> dict:
        return entities.resolve_name(self.src, name, limit=limit)

    # 消息
    def get_messages(self, **kwargs) -> dict:
        return messages.list_messages(self.src, **kwargs)

    # 关系
    def relations(self, kind: str, subject: str, **kwargs) -> dict:
        if kind == "group_members":
            return relations.group_members(self.src, subject, limit=kwargs.get("limit", 500))
        if kind == "common_groups":
            return relations.common_groups(self.src, subject, limit=kwargs.get("limit", 200))
        if kind == "person_activity":
            return relations.person_activity(
                self.src,
                subject,
                targets=kwargs.get("targets"),
                since=kwargs.get("since", ""),
                until=kwargs.get("until", ""),
            )
        if kind == "activity":
            return relations.activity(
                self.src, subject, since=kwargs.get("since", ""), until=kwargs.get("until", "")
            )
        return {"error": f"未知关系类型: {kind}"}

    # 搜索
    def search(self, query: str, **kwargs) -> dict:
        return search_mod.search(self.src, query, **kwargs)

    # 朋友圈与收藏
    def list_moments(self, **kwargs) -> dict:
        from .moments import list_moments

        return list_moments(self.src, **kwargs)

    def list_favorites(self, **kwargs) -> dict:
        from .favorites import list_favorites

        return list_favorites(self.src, **kwargs)

    # 同步与索引
    def status(self) -> dict:
        info = sync_mod.status(self.src)
        info["index"] = index.count(self.src)
        return info

    def refresh(self) -> dict:
        result = sync_mod.refresh(self.src)
        if result.get("ok"):
            result["index"] = index.update(self.src)
        return result

    def build_index(self, rebuild: bool = False) -> dict:
        return index.build(self.src, rebuild=rebuild)

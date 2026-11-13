"""wechatq 命令行入口。

与 MCP 工具同参同义，共用 service 层。
依据 docs/重构方案-2026-11-13.md 第 7 节。
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from . import entities, sync
from .db.locator import LocalSource


def _source(args) -> LocalSource:
    if args.root:
        return LocalSource(Path(args.root))
    from .config import get_decrypted_dir

    return LocalSource(get_decrypted_dir())


def _print(obj, as_json: bool, columns: list[str] | None = None) -> None:
    if as_json:
        print(json.dumps(obj, ensure_ascii=False, indent=2))
        return
    if columns and isinstance(obj.get("entities"), list):
        print(
            f"data_cutoff={obj.get('data_cutoff', '')}  total={obj.get('total')}  "
            f"returned={obj.get('returned')}  has_more={obj.get('has_more')}"
        )
        widths = {c: max(len(c), 28 if c in ("id", "name") else 12) for c in columns}
        print("  ".join(c.ljust(widths[c]) for c in columns))
        for row in obj["entities"]:
            print("  ".join(str(row.get(c, "")).ljust(widths[c]) for c in columns))
        return
    print(json.dumps(obj, ensure_ascii=False, indent=2))


def cmd_list(args) -> int:
    src = _source(args)
    obj = entities.list_entities(
        src,
        scope=args.scope,
        keyword=args.keyword,
        since_days=args.since_days,
        only_group=args.only_group,
        only_unread=args.only_unread,
        min_members=args.min_members,
        max_members=args.max_members,
        sort=args.sort,
        limit=args.limit,
        offset=args.offset,
    )
    _print(obj, args.json, ["id", "kind", "name", "member_count", "owner_name", "last_active_at", "unread"])
    return 0


def cmd_resolve(args) -> int:
    src = _source(args)
    obj = entities.resolve_name(src, args.name, limit=args.limit)
    _print(obj, args.json)
    return 0


def cmd_status(args) -> int:
    src = _source(args)
    _print(sync.status(src), args.json)
    return 0


def cmd_sync(args) -> int:
    src = _source(args)
    obj = sync.refresh(src)
    _print(obj, args.json)
    return 0 if obj.get("ok") else 1


def build_parser() -> argparse.ArgumentParser:
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--root", default="", help="解密目录，默认取 WECHAT_DECRYPTED_DIR 或项目内 decrypted")
    common.add_argument("--json", action="store_true", help="输出 JSON")

    p = argparse.ArgumentParser(prog="wechatq", description="微信聊天记录只读查询 CLI")
    sub = p.add_subparsers(dest="cmd", required=True)

    s = sub.add_parser("list", parents=[common], help="列出实体")
    s.add_argument("--scope", default="session", choices=["session", "contact", "group", "all"])
    s.add_argument("--keyword", default="")
    s.add_argument("--since-days", type=int, default=0)
    s.add_argument("--only-group", action="store_true")
    s.add_argument("--only-unread", action="store_true")
    s.add_argument("--min-members", type=int, default=0)
    s.add_argument("--max-members", type=int, default=0)
    s.add_argument("--sort", default="last_time", choices=["last_time", "members", "name"])
    s.add_argument("--limit", type=int, default=50)
    s.add_argument("--offset", type=int, default=0)
    s.set_defaults(func=cmd_list)

    r = sub.add_parser("resolve", parents=[common], help="名称解析，返回候选")
    r.add_argument("name")
    r.add_argument("--limit", type=int, default=5)
    r.set_defaults(func=cmd_resolve)

    st = sub.add_parser("status", parents=[common], help="数据源状态")
    st.set_defaults(func=cmd_status)

    sy = sub.add_parser("sync", parents=[common], help="刷新解密数据")
    sy.set_defaults(func=cmd_sync)
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        return args.func(args)
    except FileNotFoundError as e:
        print(f"错误: {e}", file=sys.stderr)
        return 3


if __name__ == "__main__":
    raise SystemExit(main())

"""wechatq 命令行入口。

与 MCP 工具同参同义，共用 service 层。
依据 docs/重构方案-消息模型与输出规范-2026-11-14.md 第 0 与 5 节：
- 默认输出 NDJSON，一行一条消息，脚本直接读；
- --json 输出紧凑信封，--pretty 缩进信封，--format text/md 输出人读分节文本；
- 消息默认 detail=full（脚本要 ID 与类型），MCP 默认 lite。
"""
from __future__ import annotations

import argparse
import json
import sys

from .service import Service


def _emit(obj, as_json: bool = True, pretty: bool = False) -> None:
    if as_json and not pretty:
        print(json.dumps(obj, ensure_ascii=False))
    else:
        print(json.dumps(obj, ensure_ascii=False, indent=2))


def _warn(obj: dict) -> None:
    """提示信息一律走 stderr，不污染 NDJSON。"""
    for w in obj.get("warnings") or []:
        print(f"提示: {w}", file=sys.stderr)
    if obj.get("unresolved"):
        print("未解析: " + json.dumps(obj["unresolved"], ensure_ascii=False), file=sys.stderr)


def _service(args) -> Service:
    auto = not getattr(args, "no_fresh", False)
    svc = Service(args.root or None, auto_refresh=auto)
    if getattr(args, "fresh", False):
        svc.ensure_fresh(force=True)
    return svc


def _fail(obj: dict) -> int:
    _emit(obj)
    return 2


def cmd_list(args) -> int:
    svc = _service(args)
    obj = svc.list_entities(
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
        detail=args.detail,
    )
    if not obj.get("ok", True):
        return _fail(obj)
    if args.json or args.pretty:
        _emit(obj, True, args.pretty)
        return 0
    print(
        f"data_until={obj['data_until']}  total={obj['total']}  "
        f"returned={obj['returned']}  has_more={obj['has_more']}  next={obj['next'] or '-'}"
    )
    cols = ["id", "kind", "name", "member_count", "owner_name", "last_active_at", "unread"]
    print("  ".join(c.ljust(24) for c in cols))
    for row in obj["entities"]:
        print("  ".join(str(row.get(c, "") or "").ljust(24) for c in cols))
    return 0


def cmd_messages(args) -> int:
    svc = _service(args)
    targets = args.targets or None
    if targets and len(targets) == 1 and "," in targets[0]:
        targets = [t for t in targets[0].split(",") if t]
    kwargs = dict(
        targets=targets,
        sender=args.sender,
        start=args.start,
        end=args.end,
        types=args.types or None,
        keyword=args.keyword,
        order=args.order,
        limit=args.limit,
        cursor=args.cursor,
        per_target_limit=args.per_target_limit,
        include_media=args.include_media,
        detail=args.detail,
    )
    if args.output == "file":
        result = svc.export_messages(out_dir=args.out, fmt=args.format, **kwargs)
        _emit(result)
        return 0 if result.get("exported") else 1

    obj = svc.get_messages(**kwargs)
    if not obj.get("ok", True):
        return _fail(obj)
    _warn(obj)
    if args.format in ("text", "md"):
        from . import render

        text = render.render_text(obj) if args.format == "text" else render.render_md(obj)
        sys.stdout.write(text)
        return 0
    if args.format == "csv":
        from . import export

        rows = obj.get("messages") or []
        sys.stdout.write(export.render_csv(rows, export.columns_of(rows, export.MESSAGE_COLUMNS)))
        return 0
    if args.json or args.pretty or args.format == "json":
        _emit(obj, True, args.pretty)
        return 0
    from . import render

    sys.stdout.write(render.render_ndjson(obj))
    return 0


def cmd_groups(args) -> int:
    """按成员组合找群：哪些群同时包含这几个人。"""
    svc = _service(args)
    obj = svc.groups_by_members(
        args.members, limit=args.limit, require_all=not args.any_member
    )
    if not obj.get("ok", True):
        return _fail(obj)
    if args.json or args.pretty:
        _emit(obj, True, args.pretty)
        return 0
    print(f"total={obj['total']}  returned={obj['returned']}  data_until={obj['data_until']}")
    for g in obj["groups"]:
        print(f"{g['member_count'] or 0:>6}  {g['name']}  [{g['name_source']}]  命中={g['matched']}")
    if obj.get("unresolved"):
        print("未解析: " + json.dumps(obj["unresolved"], ensure_ascii=False), file=sys.stderr)
    return 0


def cmd_rel(args) -> int:
    svc = _service(args)
    obj = svc.relations(
        args.kind,
        args.subject,
        targets=args.targets,
        since=args.since,
        until=args.until,
        limit=args.limit,
        offset=args.offset,
    )
    if not obj.get("ok", True):
        return _fail(obj)
    if args.json or args.pretty:
        _emit(obj, True, args.pretty)
        return 0
    if args.kind == "group_members":
        members = obj.get("members", [])
        print(f"group={obj.get('group', {}).get('name', '')}  total={obj.get('total')}  data_until={obj.get('data_until')}")
        for m in members[:50]:
            print(f"{m['name']} | {m['relation']} | {m['alias']}")
    elif args.kind == "common_groups":
        print(f"person={obj.get('person', {}).get('name', '')}  total={obj.get('total')}  basis={obj.get('basis')}")
        for g in obj.get("groups", [])[:30]:
            print(f"{g['messages']:>6}  {g['name']}")
    elif args.kind == "person_activity":
        print(f"person={obj.get('person', {}).get('name', '')}  total={obj.get('total')}  basis={obj.get('basis')}")
        for g in obj.get("groups", [])[:30]:
            print(f"{g['messages']:>6}  {g['name']}  last={g['last_at']}")
    else:
        print(f"chat={obj.get('chat', {}).get('name', '')}  total={obj.get('total')}  basis={obj.get('basis')}")
        print("by_type:", obj.get("by_type"))
        print("top_senders:", obj.get("top_senders"))
        print("by_hour:", obj.get("by_hour"))
    return 0


def cmd_search(args) -> int:
    svc = _service(args)
    obj = svc.search(
        args.query, scope=args.scope, since=args.since, until=args.until, limit=args.limit, offset=args.offset
    )
    if not obj.get("ok", True):
        return _fail(obj)
    if args.json or args.pretty:
        _emit(obj, True, args.pretty)
        return 0
    print(
        f"counts={obj['counts']}  total={obj['total']}  has_more={obj['has_more']}"
        f"  next={obj['next']}  data_until={obj['data_until']}"
    )
    for e in obj["entities"][:20]:
        print(f"[{e['kind']}] {e['name']}  <- {e['matched']}={e['value']}")
    for m in obj["messages"][:20]:
        print(f"[{m['time']}] {m['chat_name']} | {m['from']}: {m['snippet'][:120]}")
    return 0


def cmd_resolve(args) -> int:
    svc = _service(args)
    _emit(svc.resolve(args.name, limit=args.limit), True, args.pretty)
    return 0


def cmd_status(args) -> int:
    obj = _service(args).status()
    # status 本身就是 JSON；不加 --json 时按缩进给，加了就紧凑
    _emit(obj, True, args.pretty or not args.json)
    return 0 if obj.get("ok", True) else 2


def cmd_sync(args) -> int:
    svc = Service(args.root or None, auto_refresh=False)
    obj = svc.refresh(force=True)
    _emit(obj, True, True)
    return 0 if obj.get("refreshed") else 1


def cmd_index(args) -> int:
    obj = _service(args).build_index(rebuild=args.rebuild)
    _emit(obj, True, args.pretty)
    return 0 if obj.get("ok") else 1


def cmd_moments(args) -> int:
    svc = _service(args)
    obj = svc.list_moments(
        author=args.author,
        keyword=args.keyword,
        since=args.since,
        until=args.until,
        limit=args.limit,
        offset=args.offset,
    )
    if args.json or args.pretty:
        _emit(obj, True, args.pretty)
        return 0
    if not obj.get("ok", True):
        return _fail(obj)
    print(
        f"total={obj['total']} returned={obj['returned']} has_more={obj['has_more']}"
        f" next={obj['next']} data_until={obj['data_until']}"
    )
    for m in obj["moments"]:
        print(
            f"[{m['time']}] {m['author_name']}: {(m['text'] or '')[:80]}"
            f" likes={m['like_count']} comments={m['comment_count']}"
        )
    return 0


def cmd_favorites(args) -> int:
    svc = _service(args)
    obj = svc.list_favorites(
        type=args.type,
        keyword=args.keyword,
        tag=args.tag,
        since=args.since,
        until=args.until,
        limit=args.limit,
        offset=args.offset,
    )
    if args.json or args.pretty:
        _emit(obj, True, args.pretty)
        return 0
    if not obj.get("ok", True):
        return _fail(obj)
    print(
        f"total={obj['total']} returned={obj['returned']} has_more={obj['has_more']}"
        f" next={obj['next']} data_until={obj['data_until']}"
    )
    for it in obj["items"]:
        label = it["title"] or it["desc"]
        print(f"[{it['time']}] {it['type']} {label[:60]} {it['url'][:60]}")
    return 0


def cmd_report(args) -> int:
    from .report import run as run_report

    svc = _service(args)
    out = args.out or f"reports/{args.year}"
    obj = run_report(svc.src, args.year, out)
    _emit(obj, True, True)
    return 0 if obj.get("ok") else 1


def cmd_export(args) -> int:
    """导出：按 asc 顺序翻页取满，边取边写。"""
    svc = _service(args)
    result = svc.export_messages(
        out_dir=args.out,
        fmt=args.format,
        targets=args.targets or None,
        sender=args.sender,
        start=args.start,
        end=args.end,
        types=args.types or None,
        keyword=args.keyword,
        order="asc",
        detail="full",
    )
    result["basis"] = "单遍流式导出（asc）"
    _emit(result)
    return 0 if result.get("exported") else 1


def build_parser() -> argparse.ArgumentParser:
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--root", default="", help="解密目录，默认取 WECHAT_DECRYPTED_DIR 或项目内 decrypted")
    common.add_argument("--json", action="store_true", help="输出紧凑 JSON 信封")
    common.add_argument("--pretty", action="store_true", help="输出缩进 JSON 信封")
    common.add_argument("--fresh", action="store_true", help="无视 TTL，执行前立即刷新解密数据")
    common.add_argument("--no-fresh", action="store_true", help="本次不自动刷新，直接用现有数据")

    detail = argparse.ArgumentParser(add_help=False)
    detail.add_argument("--detail", default="full", choices=["lite", "full", "raw"], help="详细度，默认 full")

    p = argparse.ArgumentParser(prog="wechatq", description="微信聊天记录只读查询 CLI")
    sub = p.add_subparsers(dest="cmd", required=True)

    s = sub.add_parser("list", parents=[common, detail], help="列出实体")
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

    m = sub.add_parser("messages", parents=[common, detail], help="读取消息")
    m.add_argument("--targets", nargs="*", default=[])
    m.add_argument("--sender", default="")
    m.add_argument("--start", default="")
    m.add_argument("--end", default="")
    m.add_argument("--types", nargs="*", default=[])
    m.add_argument("--keyword", default="")
    m.add_argument("--order", default="desc", choices=["desc", "asc"])
    m.add_argument("--limit", type=int, default=50)
    m.add_argument("--cursor", default="", help="上一页返回的 next")
    m.add_argument("--per-target-limit", type=int, default=0, help="多会话时每会话上限，0 表示不限")
    m.add_argument("--include-media", action="store_true", help="提取语音转写与图片路径")
    m.add_argument("--output", default="inline", choices=["inline", "file"])
    m.add_argument("--out", default="", help="--output file 时的落盘目录")
    m.add_argument(
        "--format",
        default="ndjson",
        choices=["ndjson", "json", "csv", "md", "text"],
        help="stdout 或落盘格式，默认 ndjson",
    )
    m.set_defaults(func=cmd_messages)

    g = sub.add_parser("groups", parents=[common], help="按成员组合找群")
    g.add_argument("--members", nargs="+", required=True, help="成员名或 wxid，多个用空格或逗号分隔")
    g.add_argument("--any-member", action="store_true", help="只要有其中一人就算命中，默认要求全部命中")
    g.add_argument("--limit", type=int, default=20)
    g.set_defaults(func=cmd_groups)

    r = sub.add_parser("rel", parents=[common], help="关系查询")
    r.add_argument("--kind", required=True, choices=["group_members", "common_groups", "person_activity", "activity"])
    r.add_argument("--subject", required=True)
    r.add_argument("--targets", nargs="*", default=None)
    r.add_argument("--since", default="")
    r.add_argument("--until", default="")
    r.add_argument("--limit", type=int, default=500)
    r.add_argument("--offset", type=int, default=0, help="分页偏移，配合 next 翻页")
    r.set_defaults(func=cmd_rel)

    q = sub.add_parser("search", parents=[common], help="搜索实体与消息")
    q.add_argument("query")
    q.add_argument("--scope", default="all", choices=["message", "entity", "all"])
    q.add_argument("--since", default="")
    q.add_argument("--until", default="")
    q.add_argument("--limit", type=int, default=30)
    q.add_argument("--offset", type=int, default=0)
    q.set_defaults(func=cmd_search)

    e = sub.add_parser("export", parents=[common], help="导出到文件")
    e.add_argument("--targets", nargs="*", default=[])
    e.add_argument("--sender", default="")
    e.add_argument("--start", default="")
    e.add_argument("--end", default="")
    e.add_argument("--types", nargs="*", default=[])
    e.add_argument("--keyword", default="")
    e.add_argument("--format", default="csv", choices=["csv", "json", "ndjson", "md", "text", "html"])
    e.add_argument("--out", default="")
    e.set_defaults(func=cmd_export)

    rs = sub.add_parser("resolve", parents=[common], help="名称解析，返回候选")
    rs.add_argument("name")
    rs.add_argument("--limit", type=int, default=5)
    rs.set_defaults(func=cmd_resolve)

    st = sub.add_parser("status", parents=[common], help="数据源状态")
    st.set_defaults(func=cmd_status)

    sy = sub.add_parser("sync", parents=[common], help="刷新解密数据并更新索引")
    sy.set_defaults(func=cmd_sync)

    ix = sub.add_parser("index", parents=[common], help="构建或更新全文索引")
    ix.add_argument("--rebuild", action="store_true")
    ix.set_defaults(func=cmd_index)

    rp = sub.add_parser("report", parents=[common], help="年度报告")
    rp.add_argument("--year", type=int, required=True)
    rp.add_argument("--out", default="")
    rp.set_defaults(func=cmd_report)

    mo = sub.add_parser("moments", parents=[common], help="朋友圈")
    mo.add_argument("--author", default="")
    mo.add_argument("--keyword", default="")
    mo.add_argument("--since", default="")
    mo.add_argument("--until", default="")
    mo.add_argument("--limit", type=int, default=20)
    mo.add_argument("--offset", type=int, default=0)
    mo.set_defaults(func=cmd_moments)

    fa = sub.add_parser("favorites", parents=[common], help="收藏")
    fa.add_argument("--type", default="")
    fa.add_argument("--keyword", default="")
    fa.add_argument("--tag", default="")
    fa.add_argument("--since", default="")
    fa.add_argument("--until", default="")
    fa.add_argument("--limit", type=int, default=20)
    fa.add_argument("--offset", type=int, default=0)
    fa.set_defaults(func=cmd_favorites)
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

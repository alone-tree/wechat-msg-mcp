"""wechatq 命令行入口。

与 MCP 工具同参同义，共用 service 层。
依据 docs/重构方案-2026-11-13.md 第 7 节。
"""
from __future__ import annotations

import argparse
import json
import sys

from .service import Service


def _print(obj, as_json: bool) -> None:
    print(json.dumps(obj, ensure_ascii=False, indent=2))


def _service(args) -> Service:
    auto = not getattr(args, "no_fresh", False)
    svc = Service(args.root or None, auto_refresh=auto)
    if getattr(args, "fresh", False):
        svc.ensure_fresh(force=True)
    return svc


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
    )
    if args.json:
        _print(obj, True)
        return 0
    print(
        f"data_cutoff={obj['data_cutoff']}  total={obj['total']}  "
        f"returned={obj['returned']}  has_more={obj['has_more']}"
    )
    cols = ["id", "kind", "name", "member_count", "owner_name", "last_active_at", "unread"]
    print("  ".join(c.ljust(24) for c in cols))
    for row in obj["entities"]:
        print("  ".join(str(row.get(c, "")).ljust(24) for c in cols))
    return 0


def cmd_messages(args) -> int:
    svc = _service(args)
    targets = args.targets or None
    if targets and len(targets) == 1 and "," in targets[0]:
        targets = [t for t in targets[0].split(",") if t]
    obj = svc.get_messages(
        targets=targets,
        sender=args.sender,
        start=args.start,
        end=args.end,
        types=args.types,
        keyword=args.keyword,
        order=args.order,
        limit=args.limit,
        cursor=args.cursor,
        include_media=args.include_media,
    )
    if args.output == "file":
        from . import export

        exported = export.write_rows(obj["messages"], fmt=args.format, prefix="messages")
        obj["export"] = exported
        obj["messages"] = obj["messages"][:3]
    if args.json:
        _print(obj, True)
        return 0
    print(
        f"data_cutoff={obj['data_cutoff']}  total={obj['total']}  returned={obj['returned']}  "
        f"has_more={obj['has_more']}  next_cursor={obj['next_cursor'] or '-'}"
    )
    for m in obj["messages"]:
        body = m.get("text") or (f"[{m['type']}]" + (f"({m['subtype']})" if m.get("subtype") else ""))
        if m.get("voice_text"):
            body = f"[语音转写] {m['voice_text']}"
        print(f"[{m['time']}] {m['chat_name']} | {m['sender_name']}: {body[:120]}")
    if obj.get("auto_export"):
        print("提示：返回体较大，已自动落盘：", obj["auto_export"].get("path"), "行数", obj["auto_export"].get("rows"))
    elif len(json.dumps(obj, ensure_ascii=False)) > 80000:
        print("提示：本次结果较大，建议加 --output file 落盘")
    if obj.get("unresolved"):
        print("unresolved:", json.dumps(obj["unresolved"], ensure_ascii=False))
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
    )
    if args.json:
        _print(obj, True)
        return 0
    if args.kind == "group_members":
        members = obj.get("members", [])
        print(f"group={obj.get('group', {}).get('name', '')}  total={obj.get('total')}  data_cutoff={obj.get('data_cutoff')}")
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
    if args.json:
        _print(obj, True)
        return 0
    print(f"counts={obj['counts']}  total={obj['total']}  has_more={obj['has_more']}  next_offset={obj['next_offset']}  data_cutoff={obj['data_cutoff']}")
    for e in obj["entities"][:20]:
        print(f"[{e['kind']}] {e['name']}  <- {e['matched']}={e['value']}")
    for m in obj["messages"][:20]:
        print(f"[{m['time']}] {m['chat_name']} | {m['sender_name']}: {m['snippet'][:120]}")
    return 0


def cmd_resolve(args) -> int:
    svc = _service(args)
    _print(svc.resolve(args.name, limit=args.limit), True)
    return 0


def cmd_status(args) -> int:
    _print(_service(args).status(), True)
    return 0


def cmd_sync(args) -> int:
    svc = Service(args.root or None, auto_refresh=False)
    obj = svc.refresh(force=True)
    _print(obj, True)
    return 0 if obj.get("refreshed") else 1


def cmd_index(args) -> int:
    obj = _service(args).build_index(rebuild=args.rebuild)
    _print(obj, True)
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
    if args.json:
        _print(obj, True)
        return 0
    print(f"total={obj['total']} returned={obj['returned']} has_more={obj['has_more']} data_cutoff={obj['data_cutoff']}")
    for m in obj["moments"]:
        print(f"[{m['time']}] {m['author_name']}: {(m['text'] or '')[:80]} likes={m['like_count']} comments={m['comment_count']}")
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
    if args.json:
        _print(obj, True)
        return 0
    print(f"total={obj['total']} returned={obj['returned']} has_more={obj['has_more']} data_cutoff={obj['data_cutoff']}")
    for it in obj["items"]:
        label = it["title"] or it["desc"]
        print(f"[{it['time']}] {it['type']} {label[:60]} {it['url'][:60]}")
    return 0


def cmd_report(args) -> int:
    from .report import run as run_report

    svc = _service(args)
    out = args.out or f"reports/{args.year}"
    obj = run_report(svc.src, args.year, out)
    _print(obj, True)
    return 0 if obj.get("ok") else 1


def cmd_export(args) -> int:
    """导出：按条件循环翻页，边取边写。"""
    from . import export

    svc = _service(args)
    rows: list[dict] = []
    cursor = ""
    pages = 0
    while True:
        obj = svc.get_messages(
            targets=args.targets or None,
            sender=args.sender,
            start=args.start,
            end=args.end,
            types=args.types,
            keyword=args.keyword,
            order="asc",
            limit=1000,
            cursor=cursor,
        )
        rows.extend(obj["messages"])
        pages += 1
        if not obj["has_more"] or not obj["next_cursor"] or pages > 500:
            break
        cursor = obj["next_cursor"]
    result = export.write_rows(rows, fmt=args.format, prefix="export", out_dir=args.out)
    result.update({"total": len(rows), "basis": "asc 顺序导出", "data_cutoff": svc.src.data_cutoff("message")})
    _print(result, True)
    return 0


def build_parser() -> argparse.ArgumentParser:
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--root", default="", help="解密目录，默认取 WECHAT_DECRYPTED_DIR 或项目内 decrypted")
    common.add_argument("--json", action="store_true", help="输出 JSON")
    common.add_argument("--fresh", action="store_true", help="无视 TTL，执行前立即刷新解密数据")
    common.add_argument("--no-fresh", action="store_true", help="本次不自动刷新，直接用现有数据")

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

    m = sub.add_parser("messages", parents=[common], help="读取消息")
    m.add_argument("--targets", nargs="*", default=[])
    m.add_argument("--sender", default="")
    m.add_argument("--start", default="")
    m.add_argument("--end", default="")
    m.add_argument("--types", nargs="*", default=[])
    m.add_argument("--keyword", default="")
    m.add_argument("--order", default="desc", choices=["desc", "asc"])
    m.add_argument("--limit", type=int, default=50)
    m.add_argument("--cursor", default="")
    m.add_argument("--output", default="inline", choices=["inline", "file"])
    m.add_argument("--include-media", action="store_true", help="内联图片路径与语音转写")
    m.add_argument("--format", default="csv", choices=["csv", "json", "md", "html"])
    m.set_defaults(func=cmd_messages)

    r = sub.add_parser("rel", parents=[common], help="关系查询")
    r.add_argument("--kind", required=True, choices=["group_members", "common_groups", "person_activity", "activity"])
    r.add_argument("--subject", required=True)
    r.add_argument("--targets", nargs="*", default=None)
    r.add_argument("--since", default="")
    r.add_argument("--until", default="")
    r.add_argument("--limit", type=int, default=500)
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
    e.add_argument("--format", default="csv", choices=["csv", "json", "md", "html"])
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

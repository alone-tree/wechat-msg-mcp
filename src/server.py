"""MCP 服务入口。

七个工具注册在 src/mcp_tools.py，业务逻辑在 src/service.py。
刷新链在 src/freshness.py。默认 stdio，客户端拉起即可用。
依据 docs/重构方案-2026-11-13.md 第 6 节。
"""
from __future__ import annotations

import argparse
from typing import Optional

from mcp.server.fastmcp import FastMCP

from . import mcp_tools
from .service import Service


def _parse_args():
    parser = argparse.ArgumentParser(description="微信聊天记录 MCP Server")
    parser.add_argument(
        "--transport",
        choices=["stdio", "sse", "streamable-http"],
        default="stdio",
        help="传输模式：stdio（默认）、sse 或 streamable-http",
    )
    parser.add_argument("--host", default="0.0.0.0", help="监听地址")
    parser.add_argument("--port", type=int, default=18907, help="监听端口，默认 18907")
    args, _ = parser.parse_known_args()
    return args


_args = _parse_args()

mcp = FastMCP(
    name="wechat-msg-mcp",
    instructions=(
        "微信聊天记录只读查询。七个工具：\n"
        "list_entities 列会话/联系人/群，get_messages 读消息，\n"
        "relations 查群成员/共同群/跨群发言/活跃统计，search 搜实体与消息，\n"
        "sync 看数据新鲜度或强制刷新，list_moments 查朋友圈，list_favorites 查收藏。\n"
        "查询类工具会自己按 TTL（默认 60 秒）增量刷新解密数据，无需预热脚本；"
        "返回里的 refresh 字段说明本次是否刷新、解了哪些库、耗时多少，"
        "skipped=ttl 表示在 TTL 内直接用了现有数据。\n"
        "所有结果带 data_cutoff（源库最后写入时间）；超出体积的结果请用 output=file 落盘。"
    ),
    host=_args.host,
    port=_args.port,
)

_service: Optional[Service] = None


def get_service() -> Service:
    global _service
    if _service is None:
        _service = Service()
    return _service


mcp_tools.register(mcp, get_service)


def main():
    if _args.transport == "sse":
        mcp.run(transport="sse")
    elif _args.transport == "streamable-http":
        mcp.run(transport="streamable-http")
    else:
        mcp.run(transport="stdio")


if __name__ == "__main__":
    main()

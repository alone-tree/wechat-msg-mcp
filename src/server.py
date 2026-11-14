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

from .guide import INSTRUCTIONS

mcp = FastMCP(
    name="wechat-msg-mcp",
    instructions=INSTRUCTIONS,
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

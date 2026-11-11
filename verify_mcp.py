import asyncio, json
from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client

async def main():
    async with streamable_http_client("http://127.0.0.1:8765/mcp") as (read, write, _):
        async with ClientSession(read, write) as session:
            await session.initialize()
            tools = await session.list_tools()
            print("MCP 工具清单:", [t.name for t in tools.tools])

            res = await session.call_tool("search_messages", {"keyword": "陪跑", "limit": 2})
            content = res.content
            text = ""
            for c in content:
                if getattr(c, "type", None) == "text":
                    text += getattr(c, "text", "")
            print("=== search_messages('陪跑') 返回(截断) ===")
            print(text[:1200])

asyncio.run(main())

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

_GUIDE = """微信聊天记录只读查询（本机数据，不联网）。七个工具。

【默认行为】
- 调任何查询工具时，服务会自己按 TTL（默认 60 秒）增量刷新解密数据。不需要先跑脚本，也不需要重启服务。
- 结果里的 refresh 字段说明本次做了什么：refreshed=true 表示刚解了 changed_dbs 里的库；skipped=ttl 表示在 TTL 内直接用了现有数据；locked 表示另一个进程正在刷。
- data_cutoff 是该数据源最后一次写入的时间，不是最新消息时间。消息看 message 库，朋友圈看 sns 库，收藏看 favorite 库，会话列表看 session 与 contact。
- 需要绕过 TTL 立刻拉最新时，调 sync(action="sync")。

【七个工具】
- list_entities：列会话 / 联系人 / 群。scope=session|contact|group|all。返回名称、备注、微信号、地区、性别、好友关系、群成员数、群主、群公告、未读数、最后活跃时间。
- get_messages：读消息。targets 可给多个会话（人或群混传），sender 限定发言人，types 限定类型，时间用 start_date / end_date。默认最新优先，数组第一个就是最新的。
- relations：四种关系。group_members 群成员；common_groups 某人所在的群；person_activity 某人在多群的发言；activity 单会话统计（按天、按小时、按类型、发言人排行）。
- search：一次搜实体与消息。消息带命中片段，支持 offset 翻页。
- sync：action=status 看数据新鲜度；action=sync 立刻刷新。
- list_moments：朋友圈动态，含图片链接、点赞、评论。
- list_favorites：收藏条目，含类型、链接、标签。

【读消息时各类内容怎么给】
传 include_media=true 才会处理媒体：
- 图片：直接内联返回图片本身，你能直接看到图，不需要再调工具读文件。
- 表情：内联首帧图。
- 视频：内联封面图，media 里标着 kind=video 与时长。
- 语音：直接给转写文字（voice_text），不内联音频。
- 链接、小程序、视频号：给 media.url，可直接在浏览器打开。
- 转账：给 media.amount 金额与 des。红包：给可打开的 url，金额本地不存。
- 文件（PDF、表格、压缩包等）：不需要任何参数，读消息时自动复制到导出目录，media.path 就是副本路径，可直接读；source_path 是微信原路径。

体积保护：内联图片总字节超过 6MB 时，超出的不内联，改成落盘并在 media.path 给路径，media_inline.note 会说明还有多少张。

想把媒体原件存成文件时用 extract_media：留空不存，auto 存到导出目录，也可传绝对目录。语音会从微信的 SILK 转成可直接播放的 WAV。

【结果很大时】
- 超过 200 行或 80KB 会自动落盘，messages 只留前 3 条预览，auto_export 里给路径。
- 想强制落盘用 output="file"，格式 format=csv|json|md|html。
- 落盘目录默认是本进程的 exports/run-<pid>-<时间>/，12 小时后自动清理；想放到指定位置用 output_dir。
- 拿到路径后用文件读取工具去读，不要试图把几千条塞进对话。

【复杂查询怎么办】
本工具只做单次明确的查询，不做复杂聚合。多条件组合按两种方式处理：

一、多次调用自己拼（简单场景，推荐）
- 找出共同群里最近活跃的人：先 relations(kind="common_groups") 拿群列表，再 get_messages(targets=[...]) 批量拉消息，自己筛。
- 某些群的某些人的发言：sender 一次只能给一个人，多人就多调几次。
- 最近一周谁在群里说过话：get_messages 拉一周消息，自己按 sender 去重。
- 群里某个人的成员资料：relations(group_members) 拿不到的条件，配合 list_entities(scope="contact") 自己 join。

二、写脚本编排（量大或需要精确聚合）
项目目录下提供与 MCP 同参的 CLI，也可以直接在 Python 里调 service 层：
  wechatq list / messages / rel / search / moments / favorites / export / report / status / sync / index
  例：wechatq messages --targets 某群 --start 2026-11-01 --limit 500 --json
  Python：from src.service import Service; svc = Service(); svc.get_messages(targets=[...], limit=500)
注意：走 service 时不会自动刷新解密，需要的话先 svc.refresh() 或 svc.ensure_fresh(force=True)。

【几个容易踩的点】
- data_cutoff 不是最新消息时间，是数据源最后写入时间，差几分钟属于正常。
- 微信还没落盘的 WAL 内容谁都读不到，刚发的消息可能差一步，等几秒再试。
- 群里的非好友只有名字：本地没缓存过资料卡的人查不到微信号、地区、性别，这是微信本身的行为。
- 群管理员本地不存，标不了；群主能标（chat_room.owner）。
- 朋友圈与收藏有自己的关键词参数，不进 search 的全库索引。
- 语音识别依赖本地模型，模型缺失时结果里会有 warnings 告诉你模型该放哪。
"""

mcp = FastMCP(
    name="wechat-msg-mcp",
    instructions=_GUIDE,
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

# AI 使用指南

给接手的 AI 看的说明书：这个工具能干什么、怎么用、复杂需求怎么编排。

简短版已在 MCP 的 instructions 里每次连接自动下发；也可以直接调 start 工具，它返回同一份说明加当前数据状态。本文件是完整版，含代码示例与任务配方。

start 工具是平台无关的入口：不依赖 DSH 的 mcp_load，任何 MCP 客户端调它都能拿到用法。换平台时先调 start。

## 一、这是什么

把本机微信 4.x 的聊天库解密成明文 SQLite，再以只读 MCP 交给 AI 查询。

数据面：消息（约 142 万条）、联系人（3.3 万）、群（771）、朋友圈（3095 条动态加 1099 条互动）、收藏（198 条）。

三条边界：只读、不出本机、不发送任何消息。

## 二、怎么开始

MCP 已在客户端注册为 stdio 模式，直接 mcp_load 即可，不需要预热脚本、不占端口。

首次调用会比后续慢 1 到 2 秒，因为要增量解密一次；之后 60 秒内直接命中缓存。

需要绕过缓存立刻拉最新数据时调 sync(action="sync")。

## 三、七个工具

| 工具 | 干什么 | 关键参数 |
|---|---|---|
| start | 入口：返回说明书与数据状态 | 无 |
| list_entities | 列会话、联系人或群 | scope、keyword、since_days、only_group、only_unread、min_members、max_members、sort、limit、offset |
| get_messages | 读某个会话的消息 | targets、sender、start_date、end_date、types、keyword、order、limit、cursor、include_media、extract_media、output、output_dir、format |
| relations | 四种关系查询 | kind、subject、targets、since_date、until_date、limit |
| search | 一次搜实体与消息 | query、scope、since_date、until_date、limit、offset |
| sync | 数据状态与强制刷新 | action、force |
| list_moments | 朋友圈 | author、keyword、since_date、until_date、limit、offset、with_interactions |
| list_favorites | 收藏 | type、keyword、tag、since_date、until_date、limit、offset |

所有查询类工具都返回 refresh（本次刷新情况）与 data_cutoff（该数据源最后写入时间）。

## 四、各类消息怎么读

传 include_media=true 才会处理媒体内容：

| 类型 | 返回什么 | 说明 |
|---|---|---|
| 文字 | 全文 | 无 |
| 图片 | 图片内容本身 | AI 直接看图，不需要二次调用 |
| 表情 | 首帧图 | 同上 |
| 视频 | 封面图加时长 | media.kind=video |
| 语音 | 转写文字（voice_text） | 不内联音频 |
| 链接 | media.url 加标题描述 | url 可直接浏览器打开 |
| 转账 | media.amount 金额 | 取自消息里的金额字段 |
| 红包 | 可打开的 url | 金额本地不存，要拆开才知道 |
| 文件 | 副本路径加原路径 | 不需要参数，读消息时自动复制 |

文件消息是默认行为：读取时自动复制到本进程的导出目录，media.path 是副本路径，随时可读；source_path 是微信原路径。单文件上限 100MB，单次最多 20 个。

媒体内联有体积预算：图片总字节超过 6MB 时，超出的不内联，改成落盘给路径，media_inline.note 会说明还有几张。这是为了避免响应体过大导致连接断开。

想把媒体原件也存成文件时用 extract_media：留空不存，auto 存到导出目录，也可指定绝对路径。语音会从 SILK 转成可播放的 WAV。

## 五、结果太大怎么办

| 情况 | 行为 |
|---|---|
| 超过 200 行或 80KB | 自动落盘，正文只留前 3 条预览，auto_export 给路径 |
| 明确要大结果 | output="file"，格式 csv/json/md/html 任选 |
| 想放到指定目录 | output_dir 传绝对路径，该目录不参与自动清理 |
| 默认导出目录 | exports/run-<进程号>-<启动时间>/，12 小时后自动清理 |

拿到路径后请用文件读取工具分块读，不要试图把几千条塞进对话。

## 六、复杂查询：两种编排方式

工具本身只做单次明确查询，不做复杂聚合。多条件组合自己拼。

### 6.1 多次调用（简单场景）

| 需求 | 做法 |
|---|---|
| 某人所在群最近的消息 | relations(kind="common_groups", subject="某人") 拿群列表，再 get_messages(targets=[那些群]) |
| 某些群的某些人的发言 | sender 一次只能给一个人，几个人就调几次 |
| 最近一周谁在群里说过话 | get_messages 拉一周消息，自己按 sender 去重 |
| 群里某人的完整资料 | relations(group_members) 拿不到的字段，配 list_entities(scope="contact") 自己 join |
| 这个群最活跃的几个人 | relations(kind="activity") 直接给 by_day、by_hour、by_type、top_senders |

### 6.2 写脚本（量大或需要精确聚合）

项目目录下有与 MCP 同参的 CLI，也可以直接在 Python 里调 service 层。

命令行：

```text
wechatq list --scope group --sort members --min-members 100
wechatq messages --targets 某群 --start 2026-11-01 --limit 500 --json
wechatq rel --kind group_members --subject 某群
wechatq search 合同 --limit 20
wechatq export --targets 某群 --start 2025-01-01 --end 2025-12-31 --format csv
wechatq status
wechatq sync
```

Python 编排：

```python
import sys
sys.path.insert(0, r"D:\Github\wechat-msg-mcp")
from src.service import Service

svc = Service()                      # 默认读 decrypted/，不会自动刷新
svc.ensure_fresh(force=True)         # 需要最新数据时先刷一次

# 拿某人所有共同群
g = svc.relations("common_groups", "某人", limit=200)
groups = [x["id"] for x in g.get("groups", [])]

# 批量拉这些群最近的消息
msgs = svc.get_messages(targets=groups, start="2026-11-01", limit=500)

# 逐群统计发言人
from collections import Counter
for gid in groups:
    one = svc.get_messages(targets=[gid], limit=1000)
    c = Counter(m["sender_name"] for m in one.get("messages", []))
    print(gid, c.most_common(3))
```

注意：走 service 层不会自动刷新解密（MCP 层才有 TTL 逻辑），需要最新数据时显式调 ensure_fresh 或 refresh。

### 6.3 更深的编排

如果要做跨月聚合、逐人画像、批量导出这类活，不要用 MCP 工具一轮轮调（慢且吃 token），直接在项目里写脚本：读 decrypted/ 下的明文库，或用 service 层。

项目里已有现成例子可参考：src/report/annual.py 做年度报告，两遍扫描 126 万条 4 秒跑完。

## 七、CLI 速查

| 子命令 | 作用 |
|---|---|
| list | 列实体 |
| messages | 读消息 |
| rel | 关系查询 |
| search | 搜索 |
| moments | 朋友圈 |
| favorites | 收藏 |
| export | 导出到文件 |
| report | 年度报告 |
| status | 数据状态 |
| sync | 刷新解密与索引 |
| index | 构建或更新全文索引 |
| resolve | 名称解析，返回候选 |

所有子命令支持 --json、--root、--fresh（先刷新）、--no-fresh（不刷新）。

## 八、数据模型：什么在哪

| 数据 | 位置 | 备注 |
|---|---|---|
| 消息 | message/message_N.db 的 Msg_<md5(会话)> 表 | real_sender_id 指向 Name2Id 的 rowid |
| 联系人 | contact/contact.db 的 contact 表 | 备注 remark、昵称 nick_name、微信号 alias |
| 地区、性别、添加时间 | contact 表的 extra_buffer（protobuf） | 字段 5/6/7 地区、2 性别、41 添加时间 |
| 群主 | contact.db 的 chat_room.owner | 只有群主，管理员没有 |
| 群成员 | contact.db 的 chatroom_member | 只有 room_id 与 member_id，没有入群时间 |
| 群公告 | contact.db 的 chat_room_info_detail | 覆盖率约 39% |
| 朋友圈 | sns/sns.db 的 SnsTimeLine 与 SnsMessage_tmp3 | 前者动态，后者评论点赞 |
| 收藏 | favorite/favorite.db 的 fav_db_item | 标签在 fav_tag_db_item 与 fav_bind_tag_db_item |
| 全文索引 | _index/search.db | FTS5，135 万条文本 |

不可得：群管理员、群昵称（群内显示名）、结构化的 @ 提及。

## 九、容易踩的坑

| 现象 | 原因 |
|---|---|
| data_cutoff 比最新消息晚几分钟 | 它是数据源最后写入时间，不是消息时间 |
| 刚发的消息查不到 | 微信还没写入主库的 WAL，谁都读不到，等几秒再试 |
| 群里的人查不到地区和性别 | 非好友，本地没缓存过资料卡，微信本身就不给 |
| 群管理员标不出来 | 本地没有这个字段，不是没解析 |
| search 搜不到朋友圈和收藏 | 它们不进全库索引，要用各自的关键词参数 |
| 语音没有转写文字 | 语音模型没装，结果里 warnings 会说明模型该放哪 |
| 图片只有 kind 没有内容 | 本地没有原图或缓存，微信没下载过 |
| 少数文件 exists=false | 微信没下载或已清理，只剩文件名和大小 |

## 十、常见任务配方

| 想做的事 | 配方 |
|---|---|
| 今天有哪些群有新消息 | list_entities(scope="group", since_days=1, sort="last_time") |
| 某群最近一周的图片和链接 | get_messages(targets=[群], start_date=七天前, types=["image","link"], include_media=true) |
| 某人在哪些群活跃 | relations(kind="person_activity", subject="某人") |
| 某个词在哪些会话出现过 | search(query="词", scope="message") |
| 导出某群全年对话 | wechatq export --targets 某群 --start 2025-01-01 --end 2025-12-31 |
| 群成员报表 | relations(kind="group_members") 拿全量，再批量 get_messages 统计发言人 |
| 某人最近的朋友圈 | list_moments(author="某人", limit=20) |
| 我收藏的资料 | list_favorites(type="link", limit=50) |

## 十一、维护者注意

改工具参数或返回值时，同步改三处：src/mcp_tools.py 的描述、本文件、USAGE-DSH.md。三处不一致会让接手的 AI 用错参数。

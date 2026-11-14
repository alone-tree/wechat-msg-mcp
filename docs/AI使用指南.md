# AI 使用指南

给接手的 AI 看的说明书：这个工具能干什么、怎么用、复杂需求怎么编排。

MCP instructions 只放一句引导（实测 311 字），告诉 AI 先调 system(action=start)；完整说明书由这个工具返回（实测 4206 字）。本文件是更细的版本，含代码示例与任务配方。

system 工具是平台无关的入口：不依赖 DSH 的 mcp_load，任何 MCP 客户端调它都能拿到用法。换平台时先调 system(action=start)。

## 一、这是什么

把本机微信 4.x 的聊天库解密成明文 SQLite，再以只读 MCP 交给 AI 查询。

数据面：消息（约 142 万条）、联系人（3.3 万）、群（771）、朋友圈（3095 条动态加 1099 条互动）、收藏（198 条）。

三条边界：只读、不出本机、不发送任何消息。

## 二、怎么开始

MCP 已在客户端注册为 stdio 模式，直接 mcp_load 即可，不需要预热脚本、不占端口。

首次调用会比后续慢 1 到 2 秒，因为要增量解密一次；之后 60 秒内直接命中缓存。

需要绕过缓存立刻拉最新数据时调 system(action="sync")。

## 三、七个工具

| 工具 | 干什么 | 关键参数 |
|---|---|---|
| system | 系统入口：start 返说明书，status 看状态，sync 刷新 | action、force |
| list_entities | 列会话、联系人或群 | scope、keyword、since_days、only_group、only_unread、min_members、max_members、sort、limit、offset |
| get_messages | 读某个会话的消息 | targets、sender、start_date、end_date、types、keyword、order、limit、cursor、per_target_limit、detail、include_media、extract_media、output、output_dir、format |
| relations | 四种关系查询 | kind、subject、targets、since_date、until_date、limit |
| search | 一次搜实体与消息 | query、scope、since_date、until_date、limit、offset |
| list_moments | 朋友圈 | author、keyword、since_date、until_date、limit、offset、with_interactions |
| list_favorites | 收藏 | type、keyword、tag、since_date、until_date、limit、offset |

所有查询类工具都返回 refresh（本次刷新情况）与 data_until（该数据源最后写入时间）。信封带 ok：成功为 true；失败为 false 并附 error{code, message, candidates}，退出码非零，不再用空结果冒充没有数据。

列表类结果统一给四个字段：total（真实总数）、returned（本次返回条数）、has_more、next。只有 get_messages 的 next 是游标（传回 cursor），其余工具是 offset，relations 的 next 恒为 null。

## 三之半、谁是我与三档详细度

start 与 get_messages 返回里都有 self 字段：id 是账号 wxid，nickname 是微信昵称，alias 是微信号，note 说明怎么区分收发。

消息已经没有 direction 了，只看 from：本人发的固定为「我」，系统消息为空（系统消息本来没有发送者，不算未知），别人发的是显示名（备注优先）。发送者解析不出来时 from 为空并给 from_unknown=true，那是真异常。

详细度用 detail 切换：

| detail | 每条消息带什么 | 默认 |
|---|---|---|
| lite | id、time、from、text；非纯文本多一个 media；未指定 targets 时多一个 chat | MCP |
| full | 再加 sender_id、type、subtype、chat_id、chat_name，结果另给 senders 表 | CLI |
| raw | 再加 local_type、raw（原始 XML）、db | 排查时用 |

senders 表只在 full 与 raw 档出现，键是 wxid，值含 name、nickname、alias、friend。lite 想追 wxid 就升档，或干脆按 from 的名字查。

## 四、各类消息怎么读

text 是自足的：不读 media 也能看懂每条消息说了什么。voice_text 字段已取消，转写文字直接进 text。

| 类型 | text 长什么样 | media 另给什么 |
|---|---|---|
| 文字 | 全文 | 无 |
| 图片 | [图片] | 图片内容本身（MCP 内联）；只有落盘时才给 media.path，见本节末尾「path 什么时候出现」|
| 表情 | [表情] | 首帧图 |
| 语音 | 转写全文；没转写时 [语音 12 秒] | duration、transcribed；要原件用 extract_media |
| 视频 | [视频 16 秒] | duration；有封面时给 bytes 与 source=cover；本机 mp4 存在时给 path、size、exists（抽 200 条视频，12 条有 mp4，路径形如 msg/video/年月/哈希.mp4）；原件缺失时 missing_reason=no_local_video，此时 extract_media 会 skipped。cover_path 只在封面被落盘时才出现（内联时只给字节，不落盘）|
| 文件 | [文件] 文件名 | name、size、path、exists；MCP 工具层会自动把文件复制到本进程导出目录，media.path 指副本、source_path 存微信原路径，另给 file_export；直接用 Service 或 CLI 查时不复制，path 就是微信原目录 |
| 链接 | 链接标题 | url、title、desc |
| 转账 | [转账] 金额 | amount |
| 红包 | [红包] | url；金额本地不存，要拆开才知道 |
| 位置 | [位置] 地名 | 无 |
| 通话 | [通话] 时长 | 无 |
| 小程序与视频号 | 标题 | title、url |
| 聊天记录 | [聊天记录] 标题 | title |
| 系统 | [系统] 一句话 | 无；原始 XML 只在 raw 档 |

include_media 在 MCP 工具里默认 true；CLI 要显式加 --include-media，不加时语音只给「[语音 44 秒]」、图片只给「[图片]」，media 里标 missing_reason=not_requested。返回条数超过 50 时自动跳过媒体提取，避免为会被裁成预览的结果白做。

系统消息默认折叠成一句话：撤回、入群邀请、群改名、移出成员、付款、拍一拍都是。拍一拍从 app、channels 归到 system，显示成「甲 拍了拍 乙」，不要再按 app 类型去过滤。

文件消息要分两层看：MCP 工具层会自动把文件复制到本进程导出目录，media.path 指副本、source_path 存微信原路径，另给 file_export；直接用 Service 或 CLI 查时不复制，path 就是微信原目录。exists=false 说明本机没有这个文件，只能等微信下载。

media 也随 detail 伸缩：lite 只留摘要（文件名、链接标题），size、path、exists、链接描述这些要 full 档才有。所以拿不到路径时先看是不是还停在 lite。

媒体内联有体积预算：图片总字节超过 6MB 时，超出的不内联，改成落盘给路径，media_inline.note 会说明还有几张。这是为了避免响应体过大导致连接断开。

path 什么时候出现：图片与视频封面内联时只给字节，不落盘也不给 path；只有三种情况会落盘并回填路径——长截图切段（整图存 media.path）、内联体积预算超出、传了 extract_media。视频的 mp4 若本机存在，path 一直指向原文件。

拿不到图片或语音时，media 里一定带 missing_reason，按它决定下一步：

| missing_reason | 下一步 |
|---|---|
| no_local_image | 原图和缩略图都不在本机：这张图从没在微信里打开过，让用户在客户端点开再看 |
| no_local_file | 文件消息没落盘，同上 |
| no_local_video | 视频 mp4 原件没落到本地，封面可能还在 |
| no_image_key | 缺图片密钥文件，找维护者跑脚本，不是数据问题 |
| decode_failed | 文件在但解不出图，记下 local_id 反馈 |
| voice_model_missing | 语音模型没装，装了才有转写 |
| budget_exhausted | 内联图片体积预算用尽，这张改成落盘给路径 |
| too_many_rows | 条数超过 50 自动跳过，缩小 limit 或加 types 过滤 |
| not_requested | 这次的调用自己关了 include_media |

图片定位有三条路，命中任一条都算读到：message_resource 的落盘基名、hardlink 登记表、按消息自带的长度与宽高认文件。所以「登记表里没有」不等于「读不到」。

只有缩略图时会标 media.source=thumb 并在 media.note 说明。长截图（长宽比超过 3）会切成多段返回，media.tiles 是段数，相邻两段重叠约 80 像素，边界处重复的一两行属正常，不要当成两条内容；整图在 media.path。

顶层 media_missing 给出各原因的条数，用来判断是「没下载」还是「副本落后」，不要猜。

想把媒体原件也存成文件时用 extract_media：留空不存，auto 存到导出目录，也可指定绝对路径。语音会从 SILK 转成可播放的 WAV。

## 五、结果太大怎么办

| 情况 | 行为 |
|---|---|
| MCP 返回超过 200 条或 80000 字符 | 自动落盘，正文只留前 3 条预览，auto_export 给路径、行数与 is_partial |
| 明确要大结果 | output="file"，格式 csv/json/ndjson/md/text/html 任选 |
| 想放到指定目录 | output_dir 传绝对路径，该目录不参与自动清理 |
| 默认导出目录 | exports/run-<进程号>-<启动时间>/，12 小时后自动清理 |

output="file" 时工具自己翻页取满再落盘，返回的 export 里有 rows、bytes、format、columns、pages、is_partial，不用外面写循环。

| format | 形态 | 适合 |
|---|---|---|
| csv | 一行一条，全空列自动剔除 | 表格软件 |
| json | 一个数组 | 通用解析 |
| ndjson | 一行一个对象 | 喂脚本、流式处理 |
| md | 按会话分节，一行一条，末尾给本次条数与数据截止时间 | 给人读、贴进文档 |
| text | 纯文本分节，一行一条，末尾有同样的汇总 | 快速扫读 |
| html | 可直接打开的单页，按会话分节、一条一段，保留换行与媒体路径 | 交付或留档 |

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

命令行入口是 python -m src.cli（没有装出来的可执行文件）。默认输出 NDJSON，一行一个对象；加 --json 出紧凑信封，--pretty 缩进，--format text 或 md 出人读文本。

```text
python -m src.cli messages --targets 某群 --limit 100
python -m src.cli messages --targets 某群 --limit 100 --json
python -m src.cli messages --targets 某群 --detail lite --json
python -m src.cli list --scope group --sort members --limit 20 --json
python -m src.cli groups --members 甲 乙 --json
python -m src.cli rel --kind group_members --subject 某群 --json
python -m src.cli search 合同 --limit 20 --json
python -m src.cli export --targets 某群 --start 2025-01-01 --end 2025-12-31 --format md --out 某目录
python -m src.cli status --json
```

Python 编排：

```python
import sys
sys.path.insert(0, r"D:\Github\wechat-msg-mcp")
from src.service import Service

svc = Service()                      # 默认读 decrypted/ 并按 TTL 自动刷新
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
    c = Counter(m["from"] for m in one.get("messages", []))
    print(gid, c.most_common(3))
```

注意：Service 默认也会自动按 TTL 刷新（auto_refresh=True）；CLI 是靠传 auto_refresh=False 关掉的。Service 层的日期参数叫 start 与 end，CLI 是 --start 与 --end，MCP 是 start_date 与 end_date，三处名字不一样，照抄会 TypeError。

### 6.3 更深的编排

如果要做跨月聚合、逐人画像、批量导出这类活，不要用 MCP 工具一轮轮调（慢且吃 token），直接在项目里写脚本：读 decrypted/ 下的明文库，或用 service 层。

项目里已有现成例子可参考：src/report/annual.py 做年度报告，两遍扫描 126 万条 4 秒跑完。

## 七、CLI 速查

| 子命令 | 作用 |
|---|---|
| list | 列实体 |
| messages | 读消息 |
| groups | 按成员组合找群 |
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

所有子命令都支持 --json、--pretty、--root、--fresh（先刷新）、--no-fresh（不刷新）；--format 只有 messages 与 export 有，其余子命令要看人读结果就用 --pretty。

messages 另有 --detail lite/full/raw 与 --per-target-limit（多会话时每个会话上限，0 为不限）；messages 与 export 都用 --out 目录配 --output file 落盘。

groups 的用法：--members 甲 乙 默认要求两人都在同一群；加 --any-member 表示命中一人即可。

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
| data_until 比最新消息晚几分钟 | 它是数据源最后写入时间，不是消息时间 |
| types 写错直接报错 | 不再静默返回空结果；返回 error.code=unknown_types 并列出合法值，退出码 2 |
| CLI 同时用 --json 和 --include-media 报 bytes is not JSON serializable | 图片字节不能进 JSON；改用默认 NDJSON 输出，或媒体改走 MCP |
| 多目标查询在 lite 档分不清哪条属于哪个会话 | lite 只在未指定 targets 时给 chat；多目标请 --detail full 用 chat_name，或一个会话查一次 |
| 系统消息看起来只剩一句话 | 默认折叠，原始 XML 在 --detail raw 的 raw 字段 |
| 刚发的消息查不到 | 微信还没写入主库的 WAL，谁都读不到，等几秒再试 |
| 群里的人查不到地区和性别 | 非好友，本地没缓存过资料卡，微信本身就不给 |
| 群管理员标不出来 | 本地没有这个字段，不是没解析 |
| search 搜不到朋友圈和收藏 | 它们不进全库索引，要用各自的关键词参数 |
| 语音没有转写文字 | 语音模型没装，结果里 warnings 会说明模型该放哪 |
| 图片只有 kind 没有内容 | 看 media.missing_reason：no_local_image 是在微信里从没打开过，本机没有原件也没有缩略图 |
| 图片是 thumb 有点糊 | 原图没落盘，只有微信的缩略图；需要清晰版就在客户端点开一次 |
| refresh.failed_detail 出现 locked | 有进程占着旧副本，数据已写到 <名>.new，查询照常最新，无需处理 |
| 少数文件 exists=false | 微信没下载或已清理，只剩文件名和大小 |

## 十、常见任务配方

| 想做的事 | 配方 |
|---|---|
| 今天有哪些群有新消息 | list_entities(scope="group", since_days=1, sort="last_time") |
| 某群最近一周的图片和链接 | get_messages(targets=[群], start_date=七天前, types=["image","link"]) |
| 某人在哪些群活跃 | relations(kind="person_activity", subject="某人") |
| 某个词在哪些会话出现过 | search(query="词", scope="message") |
| 导出某群全年对话 | python -m src.cli export --targets 某群 --start 2025-01-01 --end 2025-12-31 --format md --out 某目录 |
| 两个人都在这群吗 | python -m src.cli groups --members 甲 乙（默认要求全部命中，加 --any-member 放宽） |
| 群成员报表 | relations(kind="group_members") 拿全量，再批量 get_messages 统计发言人 |
| 某人最近的朋友圈 | list_moments(author="某人", limit=20) |
| 我收藏的资料 | list_favorites(type="link", limit=50) |

## 十一、维护者注意

改工具参数或返回值时，同步改三处：src/mcp_tools.py 的描述、本文件、USAGE-DSH.md。三处不一致会让接手的 AI 用错参数。

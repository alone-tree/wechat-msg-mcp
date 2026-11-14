# 使用说明

七个 MCP 工具加一个同构 CLI，全部只读。新会话或首次接入先读 AGENTS.md。

## 一、启动

2026-11-14 起改为 stdio：不需要预先跑任何脚本，AI 端 mcp_load 时由客户端自己拉起进程（实测约 1 秒），也不再占用端口。

只有想把数据提前刷到最新时才需要跑：

```powershell
powershell -ExecutionPolicy Bypass -File .\start.ps1
```

它现在只做两件事：用 keys.json 增量解密变化过的库、增量更新全文索引。它不再常驻服务。

需要常驻 HTTP 服务（例如其它客户端要连 18907）时：

```powershell
powershell -ExecutionPolicy Bypass -File .\start.ps1 -Serve
```

日常手动刷新：

```powershell
uv run python scripts\quick_decrypt.py          # 增量，只解变化过的库
uv run python scripts\quick_decrypt.py --full   # 全量重解，约 10 秒
uv run python -m src.cli sync                   # 解密加索引，与 MCP system(action=sync) 同源
```

## 二、数据新鲜度

查询类工具在调用前会自动增量刷新，不需要手动干预。

| 项 | 默认 | 说明 |
|---|---|---|
| 刷新间隔 | 60 秒 | 环境变量 WECHAT_REFRESH_TTL 调整；设 0 则每次查询都刷；设负数关闭自动刷新 |
| 增量范围 | mtime 比副本新的库 | 日常几个库约 0.5 到 2.5 秒；全量 32 库约 10 秒 |
| 失败处理 | 降级不阻断 | 刷新出错照常返回现有数据，原因写在 refresh 字段 |
| 强制刷新 | system(action="sync") | 或 CLI 加 --fresh |

返回里的 refresh 字段说明本次做了什么：

| 字段 | 含义 |
|---|---|
| refreshed | 本次是否执行了刷新 |
| skipped | 没刷的原因：ttl、locked、disabled、no_source、error |
| changed_dbs | 实际重新解密的库；skipped=ttl 时是上一次刷新改动的库，字段名两种分支一致 |
| seconds | 刷新耗时 |
| ttl_left | 距下次允许刷新的秒数，已扣掉上一次刷新本身的耗时 |
| failed | 解密失败的库 |
| failed_detail | 失败库到原因的映射：too_small / read_error / hmac_mismatch / verify_fail / locked |
| shadow | 正本被占用、改写到旁路副本的库（见下文），内容照常是最新的 |
| index | 有变化时附带的索引增量结果 |

注意：TTL 内不重复检查数据源，所以数据源消失时看到的仍是 skipped=ttl。排查这类问题先设 WECHAT_REFRESH_TTL=0 或调 sync 强制刷新。

注意：微信自身的 WAL 还没 checkpoint 的内容，任何文件级解密都读不到。所以「最新」指的是最近一次落盘的快照。

副本被占用时：某个长驻进程打开了 decrypted/ 下的旧副本，Windows 就换不了文件。这时刷新把新内容写到 <名>.new，读取方自动用两者里 mtime 更新的那个，查询拿到的一直是最新数据，failed 为空、shadow 里列出被绕开的库。占用者退出后，下一次刷新替换正本并删掉旁路副本，不需要人工处理。

## 三、MCP 工具

七个工具：system、list_entities、get_messages、relations、search、list_moments、list_favorites。查询类工具返回 data_cutoff（该数据源最后写入时间）与 refresh（本次刷新情况）。

### system

系统入口，三种固定动作，新会话先调 action=start。

| action | 作用 |
|---|---|
| start（默认） | 返回完整使用说明（guide 字段）与当前数据状态 |
| status | 只看数据新鲜度 |
| sync | 立即增量刷新解密数据与索引，force 控制是否忽略 TTL |

平台无关：任何 MCP 客户端都能调，不依赖特定宿主的引导能力。换平台时只要客户端支持 MCP，这个工具就能告诉你该怎么用。

### list_entities

列会话、联系人或群。

| 参数 | 说明 |
|---|---|
| scope | session（默认）/ contact / group / all |
| keyword | 匹配名称、备注、昵称、wxid |
| since_days | 只返回该天数内有消息的实体 |
| only_group / only_unread | 只看群或只看有未读 |
| min_members / max_members | 群成员数区间 |
| sort | last_time（默认）/ members / name |
| limit / offset | 分页 |

返回字段含 gender（性别）与 region（地区）。性别来自 contact.extra_buffer 的 protobuf 字段 2：1 男、2 女、0 未设置。实测好友 100% 有资料包，群成员与非好友约 78%，本地没缓存过资料卡的（只在群里见过）会返回空。库里真正填了性别的比例本身不高，全库 26718 条资料中男 711、女 860。

返回字段含 id、kind、name、remark、nickname、alias、is_friend、member_count、owner_id、owner_name、notice、last_active_at、last_message、unread，以及 total、has_more。

### get_messages

读消息，默认最新优先。

| 参数 | 说明 |
|---|---|
| targets | 会话名或 ID 列表，可混人与群；省略则取最近 30 天全部会话 |
| sender | 发言人，支持 wxid、备注、昵称 |
| start_date / end_date | YYYY-MM-DD |
| types | text/image/voice/video/emoticon/file/link/mini_program/quote/system |
| keyword | 正文关键词 |
| order | desc（默认）或 asc |
| limit | 最多 1000 |
| cursor | 上一页的 next_cursor |
| output | inline（默认）或 file |
| format | output=file 时的 csv/json/md/html |
| include_media | 默认 true。图片、表情、视频封面作为图片内容直接内联返回，AI 一次调用就能看到图；语音给转写文字。返回条数超过 50 条时自动跳过媒体提取（那种量级会被截成预览）。传 false 可关闭 |
| extract_media | 额外把媒体原文件存成文件：留空不存，auto 存到本进程导出目录（图片 media/、语音 voice/、视频 video/），也可传绝对目录 |

文件类消息（PDF、表格、压缩包等）不需要参数：读取时自动复制到本进程 exports/run-*/files/，media.path 给副本路径，source_path 保留微信原路径；单文件限 100MB，单次最多 20 个，超过上限的在 media.note 里说明。

语音只有 include_media=true 才给转写文字（默认只有 kind）；需要音频原件时传 extract_media 加 include_media，自动从 SILK 转成 16kHz WAV（可直接播放，体积约为 SILK 的 28 倍，因为 WAV 是不压缩的原始采样），转码失败才回退存 .silk。视频目前只能拿 kind；封面内联与 mp4 原件导出尚未打通（extract_media 对视频无效）。

语音识别模型未安装时不再静默：结果里会出现 warnings 提示与 voice_model_missing 计数，告知模型应放到 decrypted/models/sensevoice/。

返回含 total、returned、has_more、next_cursor、messages、unresolved（目标解析失败时给候选）、hints（按实际内容给出的使用提示）。

另有 self 与 senders 两个字段：

| 字段 | 内容 |
|---|---|
| self | 账号主人：id（wxid）、nickname、alias、note（怎么区分收发） |
| senders | 发言人映射表，键是 wxid，值含 name、nickname、alias、friend |

每条消息只带 sender_name（自己发的显示「我」）与 direction，不再逐条重复 sender_id；要 wxid 就去 senders 按名字反查。

include_media 的媒体约定：

| 类型 | media 字段 |
|---|---|
| 图片 | kind、source（dat/cache/thumb）、bytes、inlined |
| 视频 | kind、duration、source=cover、bytes、inlined |
| 语音 | kind、duration、transcribed，另给 voice_text |
| 链接 | kind=link、url、title、desc、openable |
| 转账 | kind=transfer、amount、des、pay_sub_type |
| 红包 | kind=red_packet、url、des，amount 为 null 并注明需拆开 |
| 小程序与视频号 | kind、url、title、openable |
| 文件 | name、size、path、exists |

体积上限：内联图片原始字节总计 6MB（传 base64 后约 8MB）。超过的图片不内联，改成落盘并在 media.path 给路径，同时用 media_inline.note 说明还有多少张未内联，这样正常情况不会撞到宿主 10MB 断连。

图片内联只在本地有原图或缓存时成立。拿不到字节时 media 里会有 missing_reason 说明原因，不再只有 kind：

| missing_reason | 含义 | 怎么办 |
|---|---|---|
| hardlink_row_missing | 本地登记库里没有这个 md5，通常是副本落后或微信还没写登记 | system(action="sync", force=true) 后重查 |
| file_not_on_disk | 登记了，但 .dat 不在本地（微信按需下载，没点开过就临时不存在） | 在微信里点开该图再看 |
| no_image_key | 缺 decrypted/media_keys.json | 管理员跑 scripts\extract_image_key.py |
| decode_failed | 文件在但解不出图（格式变体） | 记录消息后反馈 |
| no_md5 | 消息里没有图片 md5 | 这类是异常数据 |
| voice_model_missing | 语音识别模型没装 | 模型放到 decrypted/models/sensevoice/ |
| no_audio | 语音库里没有这条音频 | 微信侧未落盘 |
| not_requested / too_many_rows | 显式关了媒体提取，或条数超过 50 条自动跳过 | 传 include_media=true 并缩小范围 |

顶层的 media_missing 给出各类原因的张数，一次就能判断是「没下载」还是「副本落后」。

inline 模式下若 JSON 文本超过 80000 字符（注意是字符不是字节，中文约占 2 到 3 字节）或 200 条，会自动落盘并在 auto_export 里给路径、行数与原因，messages 只留前 3 条预览。

### relations

| kind | subject | 说明 |
|---|---|---|
| group_members | 群 | 群成员列表，含备注、昵称、微信号、好友关系、签名 |
| common_groups | 人 | 我和这个人的共同群，含群内文本消息数 |
| person_activity | 人 | 这个人在各群的发言数，targets 可限定群清单 |
| activity | 群或人 | 单会话精确统计：总数、按天、按小时、按类型、Top 发言人 |

跨群聚合走本地索引，返回体里 basis 标明口径；单会话 activity 是精确扫描。四类结果都带 total（真实总数）、returned 与 has_more；目标不存在时返回 error 与 unresolved 候选。

### search

一次搜实体与消息：scope=all/entity/message，消息带命中片段，实体区分命中字段（备注、昵称、微信号、群名）。消息结果带 total、has_more、next_offset，类型统一为字符串；用 offset 配合 next_offset 翻页。


### list_moments

查朋友圈动态。参数：author（人/自己）、keyword、since_date、until_date、limit、offset、with_interactions（默认 true）。返回 moment 含 author_name、time、text、media（url）、likes、comments、comment_count。

### list_favorites

查收藏。参数：type（text/image/voice/video/link/file/chat_record/note）、keyword、tag、since_date、until_date、limit、offset。返回 item 含 type、title、desc、url、source_user、time、tags。

## 四、导出与落盘

六个查询工具都支持相同的三个参数（sync 除外）：

| 参数 | 取值 | 说明 |
|---|---|---|
| output | inline（默认）或 file | file 无条件落盘 |
| output_dir | 路径 | 留空写本进程的 exports/run-<pid>-<启动时间>/ |
| format | json/csv/md/html | get_messages 默认 csv，其余默认 json |

目录规则：

| 项 | 规则 |
|---|---|
| 隔离 | 每个进程写自己的 run 目录，多个实例互不覆盖 |
| 清理 | 进程首次导出时扫一遍，删 exports/ 下超过 12 小时的文件（含 run 目录与旧版本散落在根目录的文件），再删空目录 |
| AI 指定目录 | 写到 output_dir 指定位置，不参与自动清理 |
| 文件名 | 前缀-时间戳-序号.格式，同秒不撞名 |

返回里的 export 或 auto_export 含 path、files、rows、bytes；auto_export 多一个 reason。

想看最近导出了什么，直接列出 exports/ 下最新的 run 目录即可。

## 五、CLI（与 MCP 同参）

```text
wechatq list --scope group --sort members --min-members 100
wechatq messages --targets 某群 --start 2026-11-01 --limit 20
wechatq messages --targets 某群 --types file --limit 10
wechatq rel --kind group_members --subject 某群
wechatq rel --kind common_groups --subject 某人
wechatq rel --kind person_activity --subject 某人 --targets 群A 群B
wechatq rel --kind activity --subject 某群
wechatq search 合同 --limit 20
wechatq moments --limit 20
wechatq moments --author 某人 --since 2026-01-01
wechatq favorites --type link --limit 10
wechatq favorites --tag 学习
wechatq export --targets 某群 --start 2025-01-01 --end 2025-12-31 --format csv
wechatq index --rebuild
wechatq report --year 2025 --out reports/2025
wechatq status
wechatq sync
wechatq resolve 旧群名
```

未安装到环境时用 python -m src.cli 调用同一套命令。加 --json 输出结构化结果。

刷新开关（所有子命令通用）：

| 参数 | 作用 |
|---|---|
| --fresh | 无视 TTL，执行前先刷一次 |
| --no-fresh | 本次不自动刷新，用现有数据（适合脚本连续跑多条） |

## 六、索引

索引库 decrypted/_index/search.db，按 (库, 表) 记 local_id 水位，刷新时只补新增。当前规模约 135 万条文本消息、约 400 MB，全量重建 37 到 50 秒，查询毫秒级。

重建：wechatq index --rebuild

注意：如果 decrypted/ 被整个删掉，第一次查询会自动全量解密（实测 11.7 秒）并全量建索引（实测 49.7 秒），合计约 62 秒，会超过客户端单次调用超时。这种场景建议先手动跑一次 quick_decrypt.py 与 wechatq index，再用工具。

## 七、排查

| 症状 | 处理 |
|---|---|
| mcp_load 失败 | 看返回的错误；确认 .venv 里的 python 能跑 `python -m src.server --transport stdio` |
| 查询比平时慢 | 看返回的 refresh：refreshed=true 说明本次触发了解密，秒数正常 |
| 数据没更新 | 看 refresh.skipped：ttl 等几十秒；locked 说明另一个进程在刷；no_source 是找不到微信目录；error 看 error 字段 |
| 搜不到旧消息 | 索引可能未建或过期，跑 wechatq index 后重查 |
| 数据停在某个时间 | 微信未 checkpoint 的 WAL 读不到；切一下聊天窗口或重启微信再刷新 |
| 目标名找不到 | 用 wechatq resolve 取候选；群改名后会返回相似名 |
| 文件消息路径为空 | 该文件可能未落本地；exists=false 时只能等微信下载 |
| 图片只有 kind 没有字节 | 看 media.missing_reason：hardlink_row_missing 先 sync 后重查；file_not_on_disk 是在微信里没打开过 |
| refresh.failed_detail 里出现 locked | 有进程占着旧副本；数据已写到 <名>.new，查询照常是最新的，重启占用进程后自动归位 |
| 刷新报密钥错误 | 密钥失效，用管理员运行 scripts\decrypt_standalone.py 重新提取 |
| 想确认锁没卡死 | 看 decrypted\_index\refresh.lock；超过 120 秒会被视为残留并自动回收 |
| 端口被占用 | 仅 -Serve 模式有这问题；改端口并同步客户端 URL |

## 八、已知限制

- 微信尚未 checkpoint 的 WAL 内容读不到。刷新判据只看主库 mtime，副本也有对应的 -wal 文件但恒为 0 字节。
- 群管理员、群昵称、@提及结构化标记，本地库里没有。
- 朋友圈与收藏只读本地已同步的数据，不会触发微信重新拉取。
- 好友地区与添加时间是解 protobuf 的尽力而为，部分联系人为空。
- 不指定 targets 的查询默认限最近 30 天，避免全库扫描。
- 跨群聚合（common_groups、person_activity）以索引中的文本消息为基数，媒体消息不计入。
- 图片与语音默认就会内联（include_media 默认 true，超过 50 条自动跳过）；本地未落盘的图片只有缩略图或什么也没有，原因写在 media.missing_reason。
- 只解密有 keys.json 密钥的账号。
- 聊天正文、wxid、群名不写入日志；真实名称只放 private-docs/。

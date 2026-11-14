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

七个工具：system、list_entities、get_messages、relations、search、list_moments、list_favorites。查询类工具返回 data_until（该数据源最后写入时间）与 refresh（本次刷新情况）。

信封统一带 ok：成功 true；失败 false，并附 error{code, message, candidates}，退出码非零。失败不再用「空结果」表达，遇到空数组又拿不准时先看 ok。

列表类结果统一四个字段：total（真实总数）、returned（本次返回条数）、has_more、next。只有 get_messages 的 next 是游标（传回 cursor），其余是 offset，relations 的 next 恒为 null。

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

返回字段含 id、kind、name、name_source、remark、nickname、alias、is_friend、gender、region、add_time、member_count、owner_id、owner_name、notice、last_active_at、last_message、unread，以及 total、returned、has_more、next。

群名不用群 ID 顶替，按备注 > 群昵称 > 成员昵称拼接 > 「群聊」兜底，name_source 标明来源。实测取值：remark、nickname、members、fallback，另有 id 表示只剩 ID 可用。只有 name_source=members 时才带 members_digest（形如「甲、乙、丙等 8 人」）。兜底名字未与微信客户端截图逐条核对。

### get_messages

读消息，默认最新优先。

| 参数 | 说明 |
|---|---|
| targets | 会话名或 ID 列表，可混人与群；省略则取最近 30 天全部会话 |
| sender | 发言人，支持 wxid、备注、昵称 |
| start_date / end_date | YYYY-MM-DD |
| types | card、channels、chat_record、emoticon、file、image、link、location、mini_program、quote、red_packet、revoked、system、text、transfer、video、voice、voip。写错直接报错并列出合法值（error.code=unknown_types），不再静默返回空 |
| keyword | 正文关键词 |
| order | desc（默认）或 asc |
| limit | 最多 1000，MCP 侧默认 200 |
| cursor | 上一页返回的 next |
| detail | lite / full / raw，CLI 默认 full，MCP 默认 lite，字段见下文三档表 |
| per_target_limit | 多会话时每个会话最多取几条，默认 0 表示不单独限制；小于 limit 时才截断，并在 warnings 里点名哪个会话少了几条，且翻页也拿不到 |
| output | inline（默认）或 file |
| format | output=file 时的 csv/json/ndjson/md/text/html |
| include_media | MCP 默认 true。图片、表情、视频封面作为图片内容直接内联返回，AI 一次调用就能看到图；语音给转写文字。返回条数超过 50 条时自动跳过媒体提取（那种量级会被截成预览）。传 false 可关闭。CLI 侧的开关是 --include-media，默认不开，不加时语音只给时长文案、图片只给「[图片]」 |
| extract_media | 额外把媒体原文件存成文件：留空不存，auto 存到本进程导出目录（图片 media/、语音 voice/、视频 video/），也可传绝对目录 |

文件类消息（PDF、表格、压缩包等）不需要参数。MCP 工具层读到文件消息会自动复制到本进程导出目录：media 同时给 name、size、path（副本）、exists、source_path（微信原目录）、exported，信封另有 file_export 汇总。直接用 Service 或 CLI 查时不复制，path 就是微信原目录。2026-11-14 用 MCP 实测得到 source_path 与 file_export，用 CLI 实测只有 kind、name、size、path、exists，两者口径不同，按调用方式看。

media 的字段随 detail 伸缩：lite 只留 kind 与 name，size、path、exists 要 full 或 raw 档才出现。

需要音频原件时传 extract_media 加 include_media，自动从 SILK 转成 16kHz WAV（可直接播放，体积约为 SILK 的 28 倍，因为 WAV 是不压缩的原始采样），转码失败才回退存 .silk。视频只给封面图与时长，mp4 原件缺失时 missing_reason=no_local_video，extract_media 对视频无效。

语音识别模型未安装时不再静默：结果里会出现 warnings 提示与 voice_model_missing 计数，告知模型应放到 decrypted/models/sensevoice/。

返回含 ok、detail、total、returned、has_more、next（游标，翻页时传回 cursor）、messages、unresolved（目标解析失败时给候选）、hints、warnings、refresh。

另有 self 与 senders 两个字段：

| 字段 | 内容 |
|---|---|
| self | 账号主人：id（wxid）、nickname、alias、note（怎么区分收发） |
| senders | 发言人映射表，键是 wxid，值含 name、nickname、alias、friend；只在 full 与 raw 档出现 |

消息不再有 direction。from 是显示名：本人固定「我」，系统消息为空（系统消息不算未知发送者），别人是备注优先的名字。发送者解析不出来时 from 为空并给 from_unknown=true。要 wxid 就升到 full 档用 senders 反查。

消息字段三档：

| detail | 每条消息的字段 | 默认 |
|---|---|---|
| lite | id、time、from、text；非纯文本多 media；未指定 targets 时多 chat | MCP |
| full | 另加 sender_id、type、subtype、chat_id、chat_name | CLI |
| raw | 另加 local_type、raw、db | 排查 |

text 自足，不读 media 也能看懂：图片「[图片]」、语音转写全文或「[语音 12 秒]」、文件「[文件] 名字」、链接给标题、转账「[转账] 金额」、红包「[红包]」、位置「[位置] 地名」、通话、系统消息「[系统] 一句话」。voice_text 字段已取消，转写直接进 text。

系统消息默认折叠：revokemsg、delchatroommember、sysmsgtemplate（邀请、改名，模板变量换成真实昵称）、paymsg、plain 各折叠成一句；mmchatroomtopmsg 给「群公告或置顶消息有更新」；拍一拍从 app、channels 归入 system，显示成「甲 拍了拍 乙」。原始 XML 只在 raw 档。

include_media 的媒体约定：

| 类型 | media 字段 |
|---|---|
| 图片 | kind、source（dat/cache/thumb）、bytes、inlined |
| 视频 | kind、duration；有封面时 bytes 与 source=cover；本机 mp4 存在时给 path、size、exists（抽 200 条视频 12 条有 mp4）；原件缺失时 missing_reason=no_local_video。封面只有落盘时才写 cover_path，内联时只给字节 |
| 语音 | kind、duration、transcribed；转写文字在 text，不再给 voice_text |
| 链接 | kind=link、url、title、desc、openable |
| 转账 | kind=transfer、amount、des、pay_sub_type |
| 红包 | kind=red_packet、url、des，amount 为 null 并注明需拆开 |
| 小程序与视频号 | kind、url、title、openable |
| 文件 | MCP：name、size、path（副本）、exists、source_path、exported，另有 file_export；CLI 或 Service：name、size、path（微信原目录）、exists |

体积上限：内联图片原始字节总计 6MB（传 base64 后约 8MB）。超过的图片不内联，改成落盘并在 media.path 给路径，同时用 media_inline.note 说明还有多少张未内联，这样正常情况不会撞到宿主 10MB 断连。

图片内联只在本地有原图或缓存时成立。拿不到字节时 media 里会有 missing_reason 说明原因，不再只有 kind：

| missing_reason | 含义 | 怎么办 |
|---|---|---|
| no_local_image | 原图与缩略图都找不到：这张图从未在微信里打开过，本机一个字节都没有 | 在微信客户端点开这张图，再查一次 |
| no_local_file | 文件消息没有落到本地 | 让对方重发或在微信里点开下载 |
| no_local_video | 视频 mp4 原件没落本地，封面可能还有 | 在微信里播放一次再查 |
| no_image_key | 缺 decrypted/media_keys.json | 管理员跑 scripts\extract_image_key.py |
| decode_failed | 文件在但解不出图（格式变体） | 记录消息后反馈 |
| voice_model_missing | 语音识别模型没装 | 模型放到 decrypted/models/sensevoice/ |
| budget_exhausted | 内联图片 6MB 预算用尽 | 按 media.path 读落盘文件 |
| no_audio | 语音库里没有这条音频 | 微信侧未落盘 |
| not_requested / too_many_rows | 显式关了媒体提取，或条数超过 50 条自动跳过 | 传 include_media=true 并缩小范围 |

图片定位是三级兜底，命中任何一级都能读到：先按 message_resource.db 里记的落盘基名找，再查 hardlink 登记表，最后拿消息 XML 自带的长度与宽高在磁盘上认文件。所以登记表没有记录的新图片同样能读。

只有缩略图时会给 media.source=thumb 并在 media.note 里说明「原图未落盘，返回的是缩略图」。长宽比超过 3 的长截图会按 1600 高切成多段返回（media.tiles 是段数），相邻两段重叠约 80 像素，切缝正好落在文字行上时上下两段都能看到完整的一行；整图另存 media.path，避免客户端把整图压成几十像素宽后一个字段都看不清。

顶层的 media_missing 给出各类原因的张数，一次就能判断是「没下载」还是「副本落后」。

inline 模式下若 JSON 文本超过 80000 字符（字符不是字节，中文约占 2 到 3 字节）或 200 条，会自动落盘并在 auto_export 里给路径、行数与原因，messages 只留前 3 条预览。这是 MCP 工具层的行为（mcp_tools 的 AUTO_EXPORT_ROWS=200、AUTO_EXPORT_BYTES=80000）。CLI 不做自动落盘：实测 400 条 full 档、178419 字符照样直接打到 stdout，写脚本时要自己估规模，该落盘就显式加 --output file。

### relations

| kind | subject | 说明 |
|---|---|---|
| group_members | 群 | 群成员列表，含备注、昵称、微信号、好友关系、签名 |
| common_groups | 人 | 我和这个人的共同群，含群内文本消息数 |
| person_activity | 人 | 这个人在各群的发言数，targets 可限定群清单 |
| activity | 群或人 | 单会话精确统计：总数、按天、按小时、按类型、Top 发言人 |

跨群聚合走本地索引，返回体里 basis 标明口径；单会话 activity 是精确扫描。四类结果都带 total（真实总数）、returned 与 has_more；目标不存在时返回 error 与 unresolved 候选。

### search

一次搜实体与消息：scope=all/entity/message，消息带命中片段，实体区分命中字段（备注、昵称、微信号、群名）。信封给 total{entities, messages}、has_more、next 与 counts{本次返回数}，消息类型已经是字符串。翻页直接把 next 传给 offset，实测两页无重复；注意第 2 页起 total.entities 会变 null，看 messages 计数即可。


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
| format | csv/json/ndjson/md/text/html | export 默认 csv；messages 走 stdout 时默认 ndjson。md 与 text 按会话分节、一行一条，末尾给本次条数与数据截止时间；csv 自动剔除全空列 |

目录规则：

| 项 | 规则 |
|---|---|
| 隔离 | 每个进程写自己的 run 目录，多个实例互不覆盖 |
| 清理 | 进程首次导出时扫一遍，删 exports/ 下超过 12 小时的文件（含 run 目录与旧版本散落在根目录的文件），再删空目录 |
| AI 指定目录 | 写到 output_dir 指定位置，不参与自动清理 |
| 文件名 | 前缀-时间戳-序号.格式，同秒不撞名 |

返回里的 export 或 auto_export 含 path、files、rows、bytes、format、columns、is_partial、basis、tables；auto_export 多一个 reason，主表之外还有旁挂集合时另给 extra 与 extra_collections。

output=file 时工具单遍流式导出：每张会话表只读一次、按时间顺序产出，多会话用堆归并，30.2 万条的群实测 6.5 秒；is_partial=true 表示撞到 20 万行上限没取全，可以用时间段或 targets 分批。导出固定 full 档（lite 自动升级），保证 CSV 里有 chat_name 与 type。

想看最近导出了什么，直接列出 exports/ 下最新的 run 目录即可。

## 五、CLI（与 MCP 同参）

```text
python -m src.cli list --scope group --sort members --limit 20
python -m src.cli messages --targets 某群 --start 2026-11-01 --limit 20
python -m src.cli messages --targets 某群 --types file --limit 10 --json
python -m src.cli messages --targets 某群 --detail lite --json
python -m src.cli messages --targets 群A 群B --per-target-limit 50 --json
python -m src.cli groups --members 甲 乙
python -m src.cli groups --members 甲 乙 --any-member
python -m src.cli rel --kind group_members --subject 某群 --json
python -m src.cli rel --kind common_groups --subject 某人 --json
python -m src.cli rel --kind person_activity --subject 某人 --targets 群A 群B --json
python -m src.cli rel --kind activity --subject 某群 --json
python -m src.cli search 合同 --limit 20 --offset 20 --json
python -m src.cli moments --limit 20
python -m src.cli moments --author 某人 --since 2026-01-01
python -m src.cli favorites --type link --limit 10
python -m src.cli favorites --tag 学习
python -m src.cli export --targets 某群 --start 2025-01-01 --end 2025-12-31 --format md --out 某目录
python -m src.cli index --rebuild
python -m src.cli report --year 2025 --out reports/2025
python -m src.cli status --json
python -m src.cli sync
python -m src.cli resolve 旧群名
```

入口固定是 python -m src.cli（本机也可写 .venv\Scripts\python.exe -m src.cli），没有 wechatq 可执行文件。

输出形态：

| 参数 | 作用 |
|---|---|
| 不带参数 | NDJSON，一行一个对象，可直接管道给脚本 |
| --json | 紧凑信封，单行 |
| --pretty | 缩进信封 |
| --format text / md | 人读文本，按会话分节、一行一条。messages 还接受 ndjson/json/csv，html 只有 export 支持，messages 传 html 会被参数校验挡下；export 的 html 也按会话分节，不再出表格 |
| --out 目录 | 配 --output file 指定落盘目录 |
| --detail lite/full/raw | 消息详细度，默认 full |
| --per-target-limit N | 多会话时每个会话上限，0 为不限 |

--format 只有 messages 与 export 支持；其余子命令想要人读结果只能用 --pretty。

刷新开关（所有子命令通用）：

| 参数 | 作用 |
|---|---|
| --fresh | 无视 TTL，执行前先刷一次 |
| --no-fresh | 本次不自动刷新，用现有数据（适合脚本连续跑多条） |

## 六、索引

索引库 decrypted/_index/search.db，按 (库, 表) 记 local_id 水位，刷新时只补新增。当前规模约 135 万条文本消息、约 400 MB，全量重建 37 到 50 秒，查询毫秒级。

重建：python -m src.cli index --rebuild

注意：如果 decrypted/ 被整个删掉，第一次查询会自动全量解密（实测 11.7 秒）并全量建索引（实测 49.7 秒），合计约 62 秒，会超过客户端单次调用超时。这种场景建议先手动跑一次 quick_decrypt.py 与 python -m src.cli index，再用工具。

## 七、排查

| 症状 | 处理 |
|---|---|
| mcp_load 失败 | 看返回的错误；确认 .venv 里的 python 能跑 `python -m src.server --transport stdio` |
| 查询比平时慢 | 看返回的 refresh：refreshed=true 说明本次触发了解密，秒数正常 |
| 数据没更新 | 看 refresh.skipped：ttl 等几十秒；locked 说明另一个进程在刷；no_source 是找不到微信目录；error 看 error 字段 |
| 搜不到旧消息 | 索引可能未建或过期，跑 python -m src.cli index 后重查 |
| 数据停在某个时间 | 微信未 checkpoint 的 WAL 读不到；切一下聊天窗口或重启微信再刷新 |
| 目标名找不到 | 用 python -m src.cli resolve 取候选；群改名后会返回相似名 |
| 文件消息路径为空 | 该文件可能未落本地；exists=false 时只能等微信下载 |
| 报 unknown_types | types 取值写错了，按 error.candidates 里的合法值改 |
| CLI 加 --include-media 时有媒体字节 | 已修：CLI 弹出字节不内联，只在 stderr 提示；要看图走 MCP 或落盘 |
| 多目标查询在 lite 档看不出消息属于哪个会话 | lite 只在未指定 targets 时给 chat；多目标升 --detail full 用 chat_name，或一个会话查一次 |
| 系统消息只有一句话 | 默认折叠，原文在 --detail raw 的 raw 字段 |
| 图片只有 kind 没有字节 | 看 media.missing_reason：hardlink_row_missing 先 sync 后重查；file_not_on_disk 是在微信里没打开过 |
| refresh.failed_detail 里出现 locked | 有进程占着旧副本；数据已写到 <名>.new，查询照常是最新的，重启占用进程后自动归位 |
| 刷新报密钥错误 | 密钥失效，用管理员运行 scripts\decrypt_standalone.py 重新提取 |
| 想确认锁没卡死 | 看 decrypted\_index\refresh.lock；超过 120 秒会被视为残留并自动回收 |
| 端口被占用 | 仅 -Serve 模式有这问题；改端口并同步客户端 URL |

## 八、已知限制

- 微信尚未 checkpoint 的 WAL 内容读不到。刷新判据只看主库 mtime，副本也有对应的 -wal 文件但恒为 0 字节。
- 群管理员、群昵称、@提及结构化标记，本地库里没有。
- 消息没有收发方向字段，只能看 from（本人固定为「我」）判断是谁发的。
- 群名兜底（备注 > 群昵称 > 成员昵称拼接 > 「群聊」）未与微信客户端截图逐条核对，兜底名可能与客户端显示不同。
- CLI 的 --json 与 --include-media 同时命中图片时会抛 bytes 序列化错误；默认 NDJSON 输出不受影响，媒体完整能力走 MCP。
- 视频原件导出已可用：本机有 mp4 时 extract_media 会复制到导出目录并把 media.path 改成副本、source_path 留原件（单测 tests/test_media_export.py 覆盖；本机样本里多数视频没有 mp4，那种情况会 skipped 并标 no_local_video）。
- 朋友圈与收藏只读本地已同步的数据，不会触发微信重新拉取。
- 好友地区与添加时间是解 protobuf 的尽力而为，部分联系人为空。
- 不指定 targets 的查询默认限最近 30 天，避免全库扫描。
- 跨群聚合（common_groups、person_activity）以索引中的文本消息为基数，媒体消息不计入。
- 图片与语音默认就会内联（include_media 默认 true，超过 50 条自动跳过）。从没在微信里打开过的图片，本机只有缩略图甚至什么都没有：有缩略图时会给图并在 media.note 说明，什么都没有时 missing_reason=no_local_image。
- 云端拉原图暂不可行：聊天图片的 cdnthumburl 是微信私有 TLV，不是 URL，本地库也不存媒体地址；公开资料只有朋友圈那套明文 URL 方案。想看清晰原图就在微信客户端点开一次，文件落盘后重新查询即可。
- 只解密有 keys.json 密钥的账号。
- 聊天正文、wxid、群名不写入日志；真实名称只放 private-docs/。

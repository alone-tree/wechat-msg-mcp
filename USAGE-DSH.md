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
uv run python -m src.cli sync                   # 解密加索引，与 MCP 的 sync 同源
```

## 二、数据新鲜度

查询类工具在调用前会自动增量刷新，不需要手动干预。

| 项 | 默认 | 说明 |
|---|---|---|
| 刷新间隔 | 60 秒 | 环境变量 WECHAT_REFRESH_TTL 调整；设 0 则每次查询都刷；设负数关闭自动刷新 |
| 增量范围 | mtime 比副本新的库 | 日常几个库约 0.5 到 2.5 秒；全量 32 库约 10 秒 |
| 失败处理 | 降级不阻断 | 刷新出错照常返回现有数据，原因写在 refresh 字段 |
| 强制刷新 | sync(action="sync") | 或 CLI 加 --fresh |

返回里的 refresh 字段说明本次做了什么：

| 字段 | 含义 |
|---|---|
| refreshed | 本次是否执行了刷新 |
| skipped | 没刷的原因：ttl、locked、disabled、no_source、error |
| changed_dbs | 实际重新解密的库；skipped=ttl 时是上一次刷新改动的库，字段名两种分支一致 |
| seconds | 刷新耗时 |
| ttl_left | 距下次允许刷新的秒数，已扣掉上一次刷新本身的耗时 |
| failed | 解密失败的库 |
| index | 有变化时附带的索引增量结果 |

注意：TTL 内不重复检查数据源，所以数据源消失时看到的仍是 skipped=ttl。排查这类问题先设 WECHAT_REFRESH_TTL=0 或调 sync 强制刷新。

注意：微信自身的 WAL 还没 checkpoint 的内容，任何文件级解密都读不到。所以「最新」指的是最近一次落盘的快照。

## 三、MCP 工具

七个工具：list_entities、get_messages、relations、search、sync、list_moments、list_favorites。所有返回都带 data_cutoff（数据截止时间）与 refresh（本次刷新情况）。

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
| include_media | true 时图片/表情/视频封面落盘为 JPEG 并返回路径，语音转写为文字；本次最多 8 条图片与 8 条语音 |

返回含 total、returned、has_more、next_cursor、messages、unresolved（目标解析失败时给候选）。文件消息的 media 里带落盘路径，取不到时 exists=false；include_media=true 时图片与表情带 JPEG 路径，语音带 voice_text。

inline 模式下若返回超过 80 KB 或 200 条，会自动落盘并在 auto_export 里给路径、行数与原因，messages 只留前 3 条预览。

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

### sync

action=status 返回数据截止时间、库文件 mtime、联系人、群、索引行数，以及 TTL 剩余；action=sync 立即刷新解密数据与索引，force 默认 true。其余查询工具已自动刷新，这个工具用于显式要求立刻拉最新。

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
| 清理 | 进程首次导出时扫一遍，删 run 目录下超过 12 小时的文件，再删空目录 |
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
- 图片与语音需显式传 include_media=true 才会内联；本地未落盘的图片只能拿到缩略图或什么都没有。
- 只解密有 keys.json 密钥的账号。
- 聊天正文、wxid、群名不写入日志；真实名称只放 private-docs/。

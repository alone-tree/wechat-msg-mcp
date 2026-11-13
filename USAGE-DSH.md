# 使用说明

五个 MCP 工具加一个同构 CLI，全部只读。新会话或首次接入先读 AGENTS.md。

## 一、启动

```powershell
powershell -ExecutionPolicy Bypass -File .\start.ps1
```

脚本做三件事：用 keys.json 刷新解密数据、增量更新全文索引、在 18907 端口起 MCP 服务。看到 Application startup complete 即就绪。

日常手动刷新：

```powershell
uv run python scripts\quick_decrypt.py
uv run python -m src.cli index
```

服务端口 18907，检查：Get-NetTCPConnection -LocalPort 18907 -State Listen

## 二、MCP 工具

五个工具：list_entities、get_messages、relations、search、sync，另有 list_moments 与 list_favorites。所有返回都带 data_cutoff（数据截止时间）。

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

### relations

| kind | subject | 说明 |
|---|---|---|
| group_members | 群 | 群成员列表，含备注、昵称、微信号、好友关系、签名 |
| common_groups | 人 | 我和这个人的共同群，含群内文本消息数 |
| person_activity | 人 | 这个人在各群的发言数，targets 可限定群清单 |
| activity | 群或人 | 单会话精确统计：总数、按天、按小时、按类型、Top 发言人 |

跨群聚合走本地索引，返回体里 basis 标明口径；单会话 activity 是精确扫描。

### search

一次搜实体与消息：scope=all/entity/message，消息带命中片段，实体区分命中字段（备注、昵称、微信号、群名）。

### sync

action=status 返回数据截止时间、库文件 mtime、联系人、群、索引行数；action=sync 调 quick_decrypt 刷新并增量更新索引（需要 WECHAT_FILES_DIR）。

### list_moments

查朋友圈动态。参数：author（人/自己）、keyword、since_date、until_date、limit、offset、with_interactions（默认 true）。返回 moment 含 author_name、time、text、media（url）、likes、comments、comment_count。

### list_favorites

查收藏。参数：type（text/image/voice/video/link/file/chat_record/note）、keyword、tag、since_date、until_date、limit、offset。返回 item 含 type、title、desc、url、source_user、time、tags。

## 三、CLI（与 MCP 同参）

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

## 四、索引

索引库 decrypted/_index/search.db，按 (库, 表) 记 local_id 水位，刷新时只补新增。当前规模约 135 万条文本消息、约 400 MB，全量重建约 37 秒，查询毫秒级。

重建：wechatq index --rebuild

## 五、排查

| 症状 | 处理 |
|---|---|
| MCP 连不上 | 看 18907 是否监听；start.ps1 是否报错 |
| 搜不到旧消息 | 索引可能未建或过期，跑 wechatq index 后重查 |
| 数据停在昨天 | 微信未 checkpoint 的 WAL 读不到；切一下聊天窗口或重启微信再刷新 |
| 目标名找不到 | 用 wechatq resolve 取候选；群改名后会返回相似名 |
| 文件消息路径为空 | 该文件可能未落本地；exists=false 时只能等微信下载 |
| 刷新失败 | 密钥可能失效，用管理员运行 scripts\decrypt_standalone.py |
| 端口被占用 | 改 start.ps1 末尾端口，并同步客户端 URL |

## 六、已知限制

- 群管理员、群昵称、@提及结构化标记，本地库里没有。
- 朋友圈与收藏只读本地已同步的数据，不会触发微信重新拉取。
- 好友地区与添加时间是解 protobuf 的尽力而为，部分联系人为空。
- 不指定 targets 的查询默认限最近 30 天，避免全库扫描。
- 跨群聚合（common_groups、person_activity）以索引中的文本消息为基数，媒体消息不计入。
- 图片与语音需显式传 include_media=true 才会内联；本地未落盘的图片只能拿到缩略图或什么都没有。
- 只解密有 keys.json 密钥的账号。
- 聊天正文、wxid、群名不写入日志；真实名称只放 private-docs/。

# wechat-msg-mcp 使用说明（DSH 集成版）

> **AI 注意：在 mcp_load 之前必须先读完本文档。** 本文包含启动方法、工具用法和排查手册，全部来自 2026-10-16 的实战踩坑，不要凭印象操作。

---

## 一、架构一句话

服务进程（start.ps1 启动，监听 8907）→ mcp_register（地址簿，已持久注册）→ mcp_load（接线）→ mcp_call（传话）。
服务进程与 DSH 是两个独立程序，靠 localhost HTTP 通信；服务没起，后面全部失败。

## 二、启动服务（每次会话使用前的第 0 步）

1. 运行 `D:\Github\wechat-msg-mcp\start.ps1`（**普通权限即可**，双击或 pwsh 均可）。
2. 启动脚本会自动用 keys.json 刷新解密数据（约 1-2 分钟），然后监听 `http://localhost:8907/mcp`。
3. 看到日志出现 "Application startup complete" 即就绪。服务是常驻进程，**已在跑就不要重复启动**。
4. 检查是否就绪：`Get-NetTCPConnection -LocalPort 8907 -State Listen`

## 三、DSH 侧接入

1. 注册名 `wechat-msg-mcp`，URL `http://127.0.0.1:18907/mcp`（已持久注册，无需重复注册）。
2. `mcp_load name=wechat-msg-mcp` → 拿到 11 个工具 → `mcp_call name=wechat-msg-mcp tool=<工具名> args={...}`。
3. **服务进程重启后，当前会话必须重新 mcp_load**（连接是会话级的）。

## 四、工具速查（10 个）

| 工具 | 用途 | 关键参数 |
|------|------|----------|
| check_status | 诊断数据源是否就绪，遇问题先跑它 | 无 |
| list_contacts | 列联系人/群，按关键词过滤 | keyword, include_groups |
| find_contact | 查单个联系人详情 | name |
| list_group_members | 查群成员及公开资料（备注名/微信昵称/自定义微信号/与你的关系/地区/签名） | group_name, only_with_wechat_id, limit |
| get_sessions | 最近会话列表 | limit |
| get_chat_history | 拉某会话聊天记录（时间升序），语音直接转文字、图片直接内联、视频给封面 | contact_name, start_date, end_date, limit(≤500), offset |
| search_messages | 全局/定向关键词搜索；定向支持多会话清单（只给文字，不内联图片） | keyword, contact_name 或 contact_names(多群/多私聊), start_date, end_date |
| get_recent_messages | 跨会话拉最近 N 天；可按指定清单遍历，语音转文字+图片/视频封面内联 | days, group_only, chat_names(清单) |
| export_voice | 导出一条语音为 wav 临时文件（备用，正常不需要，聊天流里已直接出文字） | contact_name, local_id |
| summarize_chat | 单会话统计（条数/时段/成员排行） | contact_name, period |
| chat_stats_overview | 跨会话活跃度总览 | period, top_n |

语音、图片、表情、视频（2026-11-11 起，2026-11-12 扩容）：
- 语音：sherpa-onnx + SenseVoiceSmall int8 纯本地转写，聊天记录里直接显示 `（语音 长23秒）转写文字`；转写失败显示 `（语音 长23秒 转写失败）`。转写缓存 `decrypted/exports/voice_transcribe_cache.json`（按内容指纹，可随时删）。模型在 `decrypted/models/sensevoice/`。
- 图片：三条来源按清晰度依次降级，全部纯本地：
  1. `dat`：消息 `<md5>` → hardlink 登记库 → `msg/attach/<会话>/<年月>/Img/` 下的原图/`_h`高清（同一目录还有 `_t` 缩略图，留到最后才用）→ 账号级图片钥匙解密。
  2. `cache`：微信渲染缓存 `cache/<年月>/Message/<md5(会话)>/ImageTemp/<类型>_<消息时间戳>_mid_temp_convert`，是 wxgf 容器（HEVC 裸流），用 PyAV 解首帧，通常是完整分辨率。缓存被微信清理后失效。
  3. `thumb`：`_t.dat` 小缩略图，往往只有几十像素宽，只够看出“这里有个图”。
  拿到字节后统一转标准 JPEG（长边≤1600）内联。单次查询最多 5 张。
- 图片不足时会显式标注：`[图片 local_id=N]（仅缩略图 58x144，原图未落本地，看不清细节）`。看到这条标注说明图太小读不出字，不要凭猜测下结论，需要看清就让用户先在微信里点开该图（会把图下载到本地），然后重拉这段记录。
- 表情（type 47）：消息XML里的下载链接 → 拉取原始表情（GIF/PNG）→ aeskey 做 AES-CBC 解密（IV=密钥本身）→ 取首帧转 JPEG → 内联。单次查询单独限 2 张。链接过期或格式未知时只留 `[表情 local_id=N]` 标记。原图缓存 `decrypted/exports/emoticon/`。
- 视频（type 43）：只给封面，不给视频内容。本地封面是明文 JPEG，位于 `msg/video/<年月>/<文件基名>.jpg`（基名由 hardlink 的 `video_hardlink_info_v4` 按视频 md5 查得）。文本行显示 `[视频 长24秒 local_id=N]（视频，仅封面图，无音频内容）`，单次查询最多 3 条。
- 重复媒体去重：同一张图/同一个视频在多个会话出现时只内联一次，其余只保留文本标记，结尾提示 `（另有 N 条重复媒体未重复内联）`。避免同一张图发在十几个群里把预算刷光、挤掉真正的新图。
- 图片钥匙：`decrypted/media_keys.json`，由 `scripts/extract_image_key.py` 以管理员提取（微信看图时钥匙才驻留内存）。老图片若解不出会自动退到缩略图；微信重启后如大量图片解不出，重新点开几张图再跑一次提取脚本即可。
- 语音/图片/表情/视频标记里的 `local_id` 仍保留在文本行里，便于指代具体消息。

注意：日期格式 `YYYY-MM-DD`；返回为时间升序；群聊里自己的消息显示昵称（如"子柯"），私聊里显示"我"或昵称。

## 五、排查手册（按症状）

**症状 1：mcp_load 报 connection refused / fetch failed**
服务没起或端口不对。运行 start.ps1；确认 18907 在监听（见第二节第 4 条）。

**症状 2：mcp_load 报 Not Found / 起服务时报端口占用（Errno 10048）**
18907 被其他程序占用。用 `Get-NetTCPConnection -LocalPort 18907` 查占用进程；若是无关程序，换端口（改 start.ps1 末尾端口 + `mcp_register` 更新 URL）。

**症状 2b：mcp_call 报 Session not found**
服务重启过，当前会话的 MCP 连接已失效。重新 `mcp_load name=wechat-msg-mcp` 即可（不要只 peek）。

**症状 3：查不到某个联系人/群（明明应该有）**
多半是解密数据过期。重跑 start.ps1 自动刷新；确认项目根有 `keys.json`。

**症状 4：quick_decrypt 刷新时全部 ❌**
密钥失效（微信重装/大版本更新后会发生）。修复：**以管理员身份**运行 `scripts\decrypt_standalone.py`（需微信正在运行），重新提取密钥。

**症状 5：解密输出写到了奇怪的位置**
两个脚本都已改为遵守 `WECHAT_DECRYPTED_DIR` 环境变量（start.ps1 设为项目内 `decrypted/`）。若又出现，检查是否有第三方路径覆盖了该变量。

**症状 6：发送者名字显示为无关联系人 / "未知发送者"**
2026-10-16 已修复（reader.py 按 Name2Id 分库解析 real_sender_id）。若复现，检查 `src/db/reader.py` 的 `_resolve_local_sender` 和 `_build_local_sender_map`。

**症状 7：某联系人能找到但拉不出消息**
已知未解边缘 case（样本：Laura）。先确认会话列表里确有该会话，再用 username 直接查一次；仍无则归档为已知问题。

## 六、已知限制（非 bug，不要当成故障修）

1. 微信运行中未 checkpoint（落盘）的最后几条消息可能暂时缺失，之后刷新会出现。
2. 链接/图片/系统消息会带出原始 XML 片段（可读性差但内容不失真）。
3. 只解密 `F:\xwechat_files` 下有 keys.json 密钥的账号（当前=独树号）。其他账号不在数据源内。
4. 密钥与聊天数据是敏感信息：`keys.json`、`decrypted/` 已被 .gitignore 排除，**严禁提交或外传**。
5. 增量同步是惰性触发的：每次工具调用时检查源库 mtime（60 秒冷却），不是后台定时器。message 库与 contact.db 都在同步范围内（2026-11-11 起），新联系人/新群约 1 分钟内自动可见，无需重启服务。
6. search_messages 定向搜索（contact_name 传群ID/群名）2026-11-11 已修复：直接按 md5 定位 Msg_ 表，不再依赖 Name2Id 反查，且定向时不做每表 500 行截断。全局搜索仍保留 500 行/表限流，只适合查近期消息。
7. get_recent_messages 新增 chat_names 参数（会话清单），传入时按清单直查、不受"最近活跃前 30 个会话"限制，适合按固定学员名单遍历。
8. 微信没下载到本地的图片/视频只有 CDN 引用，本地无副本。图片的 `cdnthumburl` 是 TLV blob（不是 URL），拼不出带鉴权的下载地址（实测各 endpoint 全 400）；企业微信转发的 `wwfile.work.weixin.qq.com` 直链要客户端会话（403）。所以这类图只能等用户在微信里点开一次，触发下载后再读。
9. 极少数超大长截图（dat 头里 xor_size 恰好是 1MB 的那种）解不出原图，会退到微信渲染缓存或几十像素的缩略图，文本行会带标注说明。别把它当钥匙过期处理。
10. 群成员资料的可用性受「是否好友」限制（2026-11-12 实测全库）：好友 983 人里 89% 有自定义微信号、86% 有备注；群内非好友 26843 人里 99.99% 只有微信昵称，仅 6 人有微信号、2 人有备注。性别在 contact.db 里根本没有对应字段（全库扫描无候选列），取不到。群昵称（群内显示名）本地未存，也在 `chat_room.ext_buffer` 里找不到。

## 七、关键文件地图

| 文件 | 作用 |
|------|------|
| start.ps1 | 启动入口：自动刷新 + 起服务（端口 18907） |
| keys.json | 解密密钥（敏感，勿提交勿外传） |
| decrypted/ | 解密后的明文库（敏感，勿提交勿外传） |
| scripts/quick_decrypt.py | 日常刷新（免管理员，用已存密钥） |
| scripts/decrypt_standalone.py | 密钥提取+全量解密（需管理员+微信运行） |
| src/server.py | MCP 服务本体（工具注册、reader 选择、增量同步） |
| src/db/reader.py | SQLite 读取与渲染（发送者解析、定向搜索修复在此） |

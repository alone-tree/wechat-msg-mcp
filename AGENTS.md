# wechat-msg-mcp

把本机微信 4.x 的聊天库解密成明文 SQLite，再以只读 MCP 交给 AI 查询。目标是让 AI 像查数据库一样查微信：群、联系人、消息、关系、统计，不漏数据、不装懂、不越界。

仅本人私下使用，不考虑兼容别人的环境。仍然保持公开仓库。

新会话的第一件事是读本文件，然后读 docs/索引.md，再按主题进入对应主文档。

## 一、设计信条

这七条决定一切取舍，与具体实现无关，改代码前先对照。

1. 数据说话。任何结论以 decrypted/ 里的库为准，字段名、表名、行数都要查证，不凭印象、不猜结构。查不到的字段直接说不可得，不编字段名。
2. 只读优先。读微信，不写微信。写操作只允许落在 decrypted/（解密副本本身）、exports/、decrypted/_index/、decrypted/exports/。
3. 可组合优于堆工具。宁可少量参数丰富的工具，也不要十几个只能干一件事的工具；工具越多，AI 越容易用错。
4. 透明优于沉默。截断要说，用 has_more 与 total；数据旧要说，用 data_until；找不到要说，给候选而不是空结果。静默降级是最大的坑。
5. 导出优于大响应。结果一大就落文件，MCP 只回路径、条数、时间范围。几百上千条塞进响应体，最后一定被宿主截断。
6. 先文档后代码。规则、设计、任务、历史各归其位，同一规则只写一处。改代码前先把文档改对。
7. 少即是多。不做当前用不上的功能。近实时与发消息不在范围；朋友圈与收藏已按只读方式接入，没有新增数据同步流程。

## 二、红线

违反其中任何一条就是错误，不需要讨论。

- 不修改微信源库；decrypted/ 只能由解密流程写入，查询与导出不得改写。
- 聊天正文、wxid、真实群名不写进日志、不写进公开文档、不写进示例。
- 真实名称、群 ID、金额、截图路径只允许出现在 private-docs/。
- 历史提交里的旧密钥已经判定无风险，不再讨论，不再提议改写历史。
- 不擅自扩大授权：部署、推送、删除文件、改远程仓库，都要用户明确同意。
- 不编造事实、不虚构执行结果，未验证的结论必须标未验证。

## 三、工作公约

| 场景 | 做法 |
|---|---|
| 开新会话 | 本文件，再 docs/索引.md，再按主题进主文档 |
| 动手前 | 任务清单确认范围，重构方案确认设计；不清楚就问，不猜 |
| 改代码前 | 先改对应设计文档，再改代码 |
| 改完 | 更新任务状态、写开发日志、本地提交；不推送 |
| 验证 | 用真实数据对拍，结果分已实现、已验证、未验证三档 |
| 提交前 | 分区内编辑工具会保留原 mtime，git status 可能漏报同尺寸改动；提交前用 git hash-object 与 HEAD 对比，或先 touch 再 add |
| 汇报 | 先结论后证据，给文件与行号；表格与图优先于长段落 |
| 找不到依据 | 说不知道，并给出下一步查证方式 |
| 并发协作 | 本仓库可能同时有多个 AI 在改。动手前先 git status 与 git log，看清别人的未提交改动与最新提交；改完只 add 自己改过的具体文件，禁止 git add -A 或 git add .；提交前再查一次，发现别人的改动混进来就拆开 |

## 四、项目地图

### 文档

| 文件 | 职责 | 入库 |
|---|---|---|
| AGENTS.md | 总纲：信条、红线、工作公约、地图、关键事实 | 是 |
| README.md | 项目简介与快速开始 | 是 |
| USAGE-DSH.md | 运行手册：启动、刷新、工具速查、排障、已知限制 | 是 |
| docs/索引.md | 文档总索引，每个文件的职责与不写什么 | 是 |
| docs/AI使用指南.md | 给接手的 AI 看的完整说明书：工具速查、媒体读法、编排范例、任务配方、坑 | 是 |
| docs/总体设计.md | 当前实现的数据流、模块职责、数据可得性、性能边界 | 是 |
| docs/任务清单.md | 唯一任务表 T01 至 T31，含优先级、阶段、状态 | 是 |
| docs/开发日志.md | 按日期的变更、原因、验证与提交号 | 是 |
| docs/交接.md | 当前进度、已定接口与下一步 | 是 |
| docs/重构方案-2026-11-13.md | 设计与阶段验收，已实施至阶段 10 | 是 |
| docs/重构方案-消息模型与输出规范-2026-11-14.md | 消息模型、命名规范、系统折叠、群名兜底、三档详细度与出口统一，已实施（提交 9dd2156） | 是 |
| docs/工具输入输出全表.md | 七个工具的输入参数与输出字段逐条实测 | 是 |
| docs/业务场景与程序不匹配.md | 业务复盘产出的 M1 至 M10 原始清单 | 是 |
| private-docs/README.md | 私人资料的规则、命名与索引 | 否 |
| private-docs/业务场景不匹配-真实案例.md | M1 至 M5 的真实群名、群ID、条数、复现参数 | 否 |

### 文件树

```text
wechat-msg-mcp/
├── AGENTS.md                   本文件：总纲
├── README.md                   项目简介与快速开始
├── USAGE-DSH.md                运行手册：启动、刷新、排障
├── pyproject.toml              依赖与入口（wechat-mcp）
├── uv.lock                     依赖版本锁定
├── start.ps1                   刷新解密；加 -Serve 才起服务
├── .gitignore                  忽略密钥、解密数据、日志、私人文档
├── docs/
│   ├── 索引.md                 文档总索引
│   ├── 总体设计.md             现状：数据流、模块、数据可得性
│   ├── 任务清单.md             T01 至 T31
│   ├── 开发日志.md             按日期记录
│   ├── 重构方案-2026-11-13.md  设计与阶段验收
│   ├── 重构方案-消息模型与输出规范-2026-11-14.md  消息模型与输出规范
│   ├── 工具输入输出全表.md     七个工具的输入输出实测
│   ├── 业务场景与程序不匹配.md 业务复盘原始清单
│   ├── AI使用指南.md          AI 使用说明书：工具、媒体、编排、配方
│   └── 交接.md                 当前进度与下一步
├── src/
│   ├── __init__.py
│   ├── server.py               MCP 入口（薄壳，7 个工具）
│   ├── guide.py               短 instructions + 完整说明书（后者由 system 返回）
│   ├── mcp_tools.py            七个 MCP 工具的参数与 JSON 包装
│   ├── service.py              service 门面，MCP 与 CLI 共用
│   ├── config.py               路径、密钥、解密目录、账号 wxid、微信目录探测
│   ├── decrypt.py              页级解密、增量刷新、原子替换
│   ├── freshness.py            刷新链：TTL、锁、降级
│   ├── entities.py             实体查询：群、人、会话与名称候选
│   ├── messages.py             消息查询：游标、多目标、类型、文件路径
│   ├── search.py               实体与消息统一搜索
│   ├── moments.py              朋友圈：解析、筛选、评论挂载
│   ├── favorites.py            收藏：解析、类型与标签筛选
│   ├── relations.py            群成员、共同群、跨群发言、活跃统计
│   ├── export.py               导出 csv/json/ndjson/md/text/html
│   ├── render.py               消息人读渲染：按会话分节、一行一条
│   ├── sysmsg.py               系统消息折叠：模板、撤回、拍一拍
│   ├── media_files.py          文件消息落盘路径
│   ├── media_inline.py         图片字节提取、语音转写、按需落盘
│   ├── sync.py                 数据状态报告（刷新已移到 freshness）
│   ├── cli.py                  python -m src.cli 命令（与 MCP 同参，默认 NDJSON）
│   ├── db/
│   │   ├── __init__.py
│   │   ├── open_.py            只读打开（immutable）
│   │   ├── locator.py          数据源定位
│   │   ├── sender.py           发送者解析与显示名
│   │   ├── types.py            local_type 解包与 49 子类型
│   │   ├── image.py            图片与表情解密（供媒体内联）
│   │   ├── resource.py         消息到本地落盘文件名的映射（message_resource）
│   │   └── voice.py            语音转写（供媒体内联）
│   ├── index/
│   │   └── __init__.py         FTS5 构建、增量、查询、候选表
│   └── report/
│       ├── __init__.py         年度报告入口 run()
│       ├── annual.py           两遍扫描与六份产物
│       └── metrics.py          聚合、类型细分、渲染
├── scripts/
│   ├── __init__.py
│   ├── decrypt_db.py           通用页级解密
│   ├── decrypt_standalone.py   Windows 密钥提取加全量解密
│   ├── decrypt_wxecho.py       独立解密实现
│   ├── extract_key.py          macOS 密钥提取
│   ├── extract_key_windows.py  Windows 密钥提取（备用）
│   ├── extract_image_key.py    图片密钥提取（管理员）
│   ├── quick_decrypt.py        日常刷新（增量，--full 全量）
│   ├── convert_wxecho_keys.py  密钥格式转换
│   ├── check_sync.py           同步自检
│   └── check_contact_db.py     联系人库自检
├── private-docs/               真实数据留存，已忽略，不入库
│   ├── README.md               规则与索引
│   └── 业务场景不匹配-真实案例.md
├── tests/                      单元测试（共 102 项）
├── reports/                    年度报告产物，已忽略
├── exports/                    导出产物，已忽略
├── decrypted/                  解密产物约 1.2GB，已忽略
├── .venv/                      Python 环境，已忽略
├── .deja/                      外部工具目录，已忽略
├── keys.json                   解密密钥，已忽略
├── wxecho_keys.json            旧密钥文件，已忽略、已停止跟踪
├── verify_mcp.py               临时验证脚本，已忽略
├── verify_query.py             临时验证脚本，已忽略
└── *.log                       运行日志，已忽略
```

## 五、关键事实速查

| 项 | 值 |
|---|---|
| 服务端口 | 18907，仅 -Serve 时启用；默认 stdio 由客户端拉起 |
| 启动方式 | mcp_load 自动拉起，实测约 1 秒，无需预热；也有 system(action=start) 给任意客户端拿说明书 |
| 刷新机制 | 查询前按 TTL（默认 60 秒）增量刷新，失败降级不阻断 |
| 导出 | 每进程写 exports/run-<pid>-<时间>/，12 小时自动清理；AI 可用 output_dir 指定目录，指定目录不清理 |
| 媒体内联 | include_media=true 时图片、表情、视频封面以图片内容直接返回，语音给转写文字；内联预算 6MB，超出部分落盘给路径 |
| 消息字段 | 不带收发方向；from 是显示名（自己固定「我」，系统消息为空），text 自足；lite/full/raw 三档，MCP 默认 lite、CLI 默认 full |
| 系统消息 | 默认折叠成「[系统] 一句话」，原始 XML 只在 raw 档；拍一拍归 system |
| 群名兜底 | 备注 > 群昵称 > 成员昵称拼接（等 N 人）>「群聊」；name_source 标明来源，未与客户端截图对照 |
| 分页 | 统一 total、returned、has_more、next；消息的 next 是游标，其余是 offset；data_until 是数据截止时间 |
| 单次查询耗时 | TTL 内约 0.15 到 0.4 秒，会话越大越慢（小会话 0.14 秒，三千行会话取 50 条约 0.35 秒）；CLI 端到端再加约 1.2 秒进程启动；缓存覆盖联系人、群成员、发送者表、会话表与分表位置 |
| 增量解密 | 只解 mtime 比副本新的库，日常约 0.5 到 2.5 秒；全量约 10 秒 |
| 副本句柄规则 | 任何模块都不得长期持有 decrypted/ 下数据库的 sqlite 连接，一律开-查-关并加 immutable=1。Windows 不共享 DELETE，长驻句柄会让所有刷新永久失败（2026-11-14 踩过） |
| 副本被占用 | 刷新写 <名>.new 旁路副本，读取方按 mtime 取最新（latest_copy）；占用者退出后下一次刷新自动归位 |
| 媒体失败原因 | media.missing_reason 标明 no_local_image、no_image_key 等；refresh.failed_detail 标明 too_small、hmac_mismatch、locked 等 |
| 微信数据 | F:\xwechat_files，账户 wxid_ 开头，db_storage 下按模块分目录 |
| 解密输出 | decrypted/，模块分目录：contact/、message/、session/、sns/、favorite/ 等 |
| 规模 | 消息约 142 万条，12 个 message 分库，约 3140 张会话分表，联系人约 3.3 万，群 771 个（740 个有成员关系与群主，288 个有公告） |
| 其他数据面 | 朋友圈 3095 条动态与 1099 条互动；收藏 198 条与 32 个标签；好友地区覆盖 1030/1389 |
| 不可得字段 | 群管理员、群昵称、@提及结构化标记，三项本地不存在 |
| 一次性事实 | 群主在 chat_room.owner，群公告在 chat_room_info_detail，群成员在 chatroom_member |
| 媒体文件定位 | 先查 message_resource.db 的 MessageResourceInfo.packed_info（消息到落盘基名），再查 hardlink 登记表，最后按消息自带长度与宽高认文件；三者都不中才算没有 |
| 长截图 | 长宽比超过 3 的截图保底宽 600，返回时按 1600 高切段、相邻段留 80 像素重叠，避免客户端压成几十像素宽后读不出字，也避免切缝切断一行字 |
| 索引 | decrypted/_index/search.db，约 135 万条文本、约 400 MB，全量重建 37 到 50 秒 |

常用命令：

| 用途 | 命令 |
|---|---|
| 启动服务 | mcp_load（stdio 自动拉起）；要常驻 HTTP 则 start.ps1 -Serve |
| 刷新解密 | uv run python scripts\quick_decrypt.py（增量加 --full 全量） |
| 强制刷新 | python -m src.cli sync 或 MCP system(action="sync") |
| 脚本批量查 | python -m src.cli messages --targets 某群 --limit 100（默认 NDJSON，--json 出信封）|
| 按成员找群 | python -m src.cli groups --members 甲 乙 |
| 全量重解（需管理员与微信运行） | uv run python scripts\decrypt_standalone.py |
| 检查服务端口 | Get-NetTCPConnection -LocalPort 18907 -State Listen |

## 六、当前状态

重构已完成阶段 1 到 10，以及 2026-11-14 的消息模型与输出规范：七个 MCP 工具（system、list_entities、get_messages、relations、search、list_moments、list_favorites）全部可用，消息去方向、正文自足、系统消息折叠、群名兜底、三档详细度、分页与导出重做；CLI 入口是 python -m src.cli，默认 NDJSON，新增 groups --members；年度报告 4.4 秒跑完 2025 年；102 项单测通过。未验证项：群名兜底截断未与微信客户端截图对照（客户端用群昵称，本地只有备注与昵称）。

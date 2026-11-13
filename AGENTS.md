# wechat-msg-mcp

把本机微信 4.x 的聊天库解密成明文 SQLite，再以只读 MCP 交给 AI 查询。目标是让 AI 像查数据库一样查微信：群、联系人、消息、关系、统计，不漏数据、不装懂、不越界。

仅本人私下使用，不考虑兼容别人的环境。仍然保持公开仓库。

新会话的第一件事是读本文件，然后读 docs/索引.md，再按主题进入对应主文档。

## 一、设计信条

这七条决定一切取舍，与具体实现无关，改代码前先对照。

1. 数据说话。任何结论以 decrypted/ 里的库为准，字段名、表名、行数都要查证，不凭印象、不猜结构。查不到的字段直接说不可得，不编字段名。
2. 只读优先。读微信，不写微信。写操作只允许落在 exports/、decrypted/_index/、decrypted/exports/。
3. 可组合优于堆工具。宁可五个参数丰富的工具，也不要十几个只能干一件事的工具；工具越多，AI 越容易用错。
4. 透明优于沉默。截断要说，用 has_more 与 total；数据旧要说，用 data_cutoff；找不到要说，给候选而不是空结果。静默降级是最大的坑。
5. 导出优于大响应。结果一大就落文件，MCP 只回路径、条数、时间范围。几百上千条塞进响应体，最后一定被宿主截断。
6. 先文档后代码。规则、设计、任务、历史各归其位，同一规则只写一处。改代码前先把文档改对。
7. 少即是多。不做当前用不上的功能。近实时与发消息不在范围；朋友圈与收藏已按只读方式接入，没有新增数据同步流程。

## 二、红线

违反其中任何一条就是错误，不需要讨论。

- 不修改微信源库，不修改 decrypted/ 下的数据文件。
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
| 汇报 | 先结论后证据，给文件与行号；表格与图优先于长段落 |
| 找不到依据 | 说不知道，并给出下一步查证方式 |

## 四、项目地图

### 文档

| 文件 | 职责 | 入库 |
|---|---|---|
| AGENTS.md | 总纲：信条、红线、工作公约、地图、关键事实 | 是 |
| README.md | 项目简介与快速开始 | 是 |
| USAGE-DSH.md | 运行手册：启动、刷新、工具速查、排障、已知限制 | 是 |
| docs/索引.md | 文档总索引，每个文件的职责与不写什么 | 是 |
| docs/总体设计.md | 当前实现的数据流、模块职责、数据可得性、性能边界 | 是 |
| docs/任务清单.md | 唯一任务表 T01 至 T18，含优先级、阶段、状态 | 是 |
| docs/开发日志.md | 按日期的变更、原因、验证与提交号 | 是 |
| docs/交接.md | 当前进度、已定接口与下一步 | 是 |
| docs/重构方案-2026-11-13.md | 讨论稿：5 工具重构的设计与阶段验收，未实施 | 是 |
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
├── start.ps1                   Windows 启动：刷新解密并起服务（18907）
├── .gitignore                  忽略密钥、解密数据、日志、私人文档
├── docs/
│   ├── 索引.md                 文档总索引
│   ├── 总体设计.md             现状：数据流、模块、数据可得性
│   ├── 任务清单.md             T01 至 T18
│   ├── 开发日志.md             按日期记录
│   ├── 重构方案-2026-11-13.md  讨论稿，未实施
│   ├── 业务场景与程序不匹配.md 业务复盘原始清单
│   └── 交接.md                 当前进度与下一步
├── src/
│   ├── __init__.py
│   ├── server.py               MCP 入口（薄壳，7 个工具）
│   ├── mcp_tools.py            七个 MCP 工具的参数与 JSON 包装
│   ├── service.py              service 门面，MCP 与 CLI 共用
│   ├── config.py               路径、密钥、解密目录、账号 wxid
│   ├── entities.py             实体查询：群、人、会话与名称候选
│   ├── messages.py             消息查询：游标、多目标、类型、文件路径
│   ├── search.py               实体与消息统一搜索
│   ├── moments.py              朋友圈：解析、筛选、评论挂载
│   ├── favorites.py            收藏：解析、类型与标签筛选
│   ├── relations.py            群成员、共同群、跨群发言、活跃统计
│   ├── export.py               导出 json/csv/md/html
│   ├── media_files.py          文件消息落盘路径
│   ├── media_inline.py         图片落盘、表情首帧、语音转写
│   ├── sync.py                 数据源状态与刷新
│   ├── cli.py                  wechatq 命令（与 MCP 同参）
│   ├── db/
│   │   ├── __init__.py
│   │   ├── open_.py            只读打开（immutable）
│   │   ├── locator.py          数据源定位
│   │   ├── sender.py           发送者解析与显示名
│   │   ├── types.py            local_type 解包与 49 子类型
│   │   ├── image.py            图片与表情解密（供媒体内联）
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
│   ├── quick_decrypt.py        日常刷新（免管理员）
│   ├── convert_wxecho_keys.py  密钥格式转换
│   ├── check_sync.py           同步自检
│   └── check_contact_db.py     联系人库自检
├── private-docs/               真实数据留存，已忽略，不入库
│   ├── README.md               规则与索引
│   └── 业务场景不匹配-真实案例.md
├── tests/                      单元测试（共 32 项）
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
| 服务端口 | 18907，MCP HTTP 入口 /mcp |
| 微信数据 | F:\xwechat_files，账户 wxid_ 开头，db_storage 下按模块分目录 |
| 解密输出 | decrypted/，模块分目录：contact/、message/、session/、sns/、favorite/ 等 |
| 规模 | 消息约 142 万条，12 个 message 分库，约 3140 张会话分表，联系人约 3.3 万，群 771 个（740 个有成员关系与群主，288 个有公告） |
| 其他数据面 | 朋友圈 3095 条动态与 1099 条互动；收藏 198 条与 32 个标签；好友地区覆盖 1030/1389 |
| 不可得字段 | 群管理员、群昵称、@提及结构化标记，三项本地不存在 |
| 一次性事实 | 群主在 chat_room.owner，群公告在 chat_room_info_detail，群成员在 chatroom_member |
| 索引 | decrypted/_index/search.db，约 135 万条文本、约 400 MB，全量重建约 37 秒 |

常用命令：

| 用途 | 命令 |
|---|---|
| 启动服务 | powershell -ExecutionPolicy Bypass -File .\start.ps1 |
| 刷新解密 | uv run python scripts\quick_decrypt.py |
| 全量重解（需管理员与微信运行） | uv run python scripts\decrypt_standalone.py |
| 检查服务端口 | Get-NetTCPConnection -LocalPort 18907 -State Listen |

## 六、当前状态

重构已完成阶段 1 到 7：七个 MCP 工具（list_entities、get_messages、relations、search、sync、list_moments、list_favorites）全部可用；联系人新增地区与添加时间；旧 reader/tools/windows_reader 已删；CLI 与 MCP 同参；年度报告 4.4 秒跑完 2025 年；32 项单测通过。

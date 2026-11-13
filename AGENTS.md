# wechat-msg-mcp

微信 4.x 聊天记录只读 MCP 服务。本机解密微信数据库，向 AI 客户端提供群、联系人、消息与统计的查询能力。数据来源是本机已解密的 SQLite，全部查询只读。本工具仅供私人使用，无需考虑外部兼容。

新会话阅读顺序：本文件 → docs/索引.md → 按主题进入唯一主文档。

## 硬约束

- 只读：不修改微信源库与 decrypted/ 下的数据；写操作只允许发生在 exports/、decrypted/_index/ 与 decrypted/exports/。
- 隐私：聊天正文、wxid、真实群名不得写入日志或公开文档；真实名称只允许出现在 private-docs/。
- 历史提交中的旧微信密钥不构成泄密风险，用户已明确不需要处理。以后不要再提，也不要再提议改写历史。
- 文档：同一规则只在一个文件里定义，文件归属见 docs/索引.md。改代码前先改文档。

## 项目地图

### 文档

| 文件 | 职责 | 入库 |
|---|---|---|
| AGENTS.md | 本文件，项目地图与硬约束 | 是 |
| README.md | 项目简介与快速开始 | 是 |
| USAGE-DSH.md | 运行手册：启动、刷新、工具速查、排障、已知限制 | 是 |
| docs/索引.md | 文档总索引，每个文件的职责与不写什么 | 是 |
| docs/总体设计.md | 当前实现的数据流、模块职责、数据可得性、性能边界 | 是 |
| docs/任务清单.md | 唯一任务表 T01 至 T15，含优先级、阶段、状态 | 是 |
| docs/开发日志.md | 按日期的变更、原因、验证与提交号 | 是 |
| docs/重构方案-2026-11-13.md | 讨论稿：5 工具重构的设计与阶段验收，未实施 | 是 |
| docs/业务场景与程序不匹配.md | 业务复盘产出的 M1 至 M10 原始清单 | 是 |
| private-docs/README.md | 私人资料的规则、命名与索引 | 否 |
| private-docs/业务场景不匹配-真实案例.md | M1 至 M5 的真实群名、群ID、条数、复现参数 | 否 |

### 文件树

```text
wechat-msg-mcp/
├── AGENTS.md                   AI 入口：地图、硬约束、常用命令
├── README.md                   项目简介与快速开始
├── USAGE-DSH.md                运行手册：启动、刷新、排障
├── pyproject.toml              依赖与入口（wechat-mcp）
├── uv.lock                     依赖版本锁定
├── start.ps1                   Windows 启动：刷新解密并起服务（18907）
├── .gitignore                  忽略密钥、解密数据、日志、私人文档
├── docs/
│   ├── 索引.md                 文档总索引
│   ├── 总体设计.md             现状：数据流、模块、数据可得性
│   ├── 任务清单.md             T01 至 T15
│   ├── 开发日志.md             按日期记录
│   ├── 重构方案-2026-11-13.md  讨论稿，未实施
│   └── 业务场景与程序不匹配.md 业务复盘原始清单
├── src/
│   ├── __init__.py
│   ├── server.py               MCP 入口、工具注册、增量同步
│   ├── config.py               路径、密钥、解密目录检查
│   ├── db/
│   │   ├── __init__.py
│   │   ├── models.py           数据模型与文本渲染
│   │   ├── reader.py           本地 SQLite 查询
│   │   ├── image.py            图片三源降级、DAT 与表情解密
│   │   ├── voice.py            语音转写
│   │   └── windows_reader.py   HTTP 回退（待删）
│   └── tools/
│       ├── __init__.py
│       ├── contacts.py         联系人与群
│       ├── messages.py         消息、搜索、媒体内联
│       └── summary.py          统计
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
├── decrypted/                  解密产物约 1.2GB，已忽略
├── .venv/                      Python 环境，已忽略
├── .deja/                      外部工具目录，已忽略
├── keys.json                   解密密钥，已忽略
├── wxecho_keys.json            旧密钥文件，已忽略、已停止跟踪
├── verify_mcp.py               临时验证脚本，已忽略
├── verify_query.py             临时验证脚本，已忽略
└── *.log                       运行日志，已忽略
```

## 常用命令

| 用途 | 命令 |
|---|---|
| 启动服务 | powershell -ExecutionPolicy Bypass -File .\start.ps1 |
| 刷新解密 | uv run python scripts\quick_decrypt.py |
| 全量重解（需管理员与微信运行） | uv run python scripts\decrypt_standalone.py |
| 服务端口 | 18907 |
| 数据源 | F:\xwechat_files，解密输出 decrypted\ |

## 当前状态

实现与 11 个工具可用，但存在 15 项待修问题，见 docs/任务清单.md。重构方案已定稿未实施，等用户确认后从阶段 1 开始。

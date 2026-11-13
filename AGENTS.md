# wechat-msg-mcp

微信 4.x 聊天记录只读 MCP 服务。本机解密微信数据库，向 AI 客户端提供群、联系人、消息与统计的查询能力。数据来源是本机已解密的 SQLite，全部查询只读。

## 硬约束

- 只读：不修改微信源库与 decrypted/ 下的数据；写操作只允许发生在 exports/、decrypted/_index/ 与 decrypted/exports/。
- 隐私：聊天正文、wxid、真实群名不得写入日志或公开文档；真实名称只允许出现在 private-docs/。
- 历史提交中的旧微信密钥不构成泄密风险，用户已明确不需要处理。以后不要再提，也不要再提议改写历史。
- 文档：同一规则只在一个文件里定义，文件归属见 docs/索引.md。改代码前先改文档。

## 文档导航

| 文件 | 职责 |
|---|---|
| docs/索引.md | 文档总索引，先看这里再找文件 |
| docs/总体设计.md | 当前实现的设计、边界与数据可得性 |
| docs/任务清单.md | 待修与待做的唯一任务表，含优先级与状态 |
| docs/开发日志.md | 按日期的变更与原因记录 |
| docs/重构方案-2026-11-13.md | 讨论稿：5 工具重构方案，尚未实施 |
| docs/业务场景与程序不匹配.md | 业务复盘产出的 10 处不匹配，已并入任务清单 |
| USAGE-DSH.md | 启动、刷新、排障与 DSH 接入 |
| README.md | 项目简介与快速开始 |

## 常用命令

| 用途 | 命令 |
|---|---|
| 启动服务 | powershell -ExecutionPolicy Bypass -File .\start.ps1 |
| 刷新解密 | uv run python scripts\quick_decrypt.py |
| 服务端口 | 18907 |
| 数据目录 | F:\xwechat_files，解密输出 decrypted\ |

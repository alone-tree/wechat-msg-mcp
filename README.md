# 微信聊天记录 MCP Server

把本机微信 4.x 的聊天数据库解密成明文 SQLite，再以 MCP 形式提供给 AI 客户端查询。只读访问，数据不出本机。

支持 Windows（主用）与 macOS。Windows 模式不依赖任何第三方分析工具。

## 能力

| 类别 | 能力 |
|---|---|
| 联系人 | 列出与搜索联系人和群，查单个联系人详情 |
| 群 | 列出群成员及其公开资料：备注、微信昵称、自定义微信号、与你的关系、地区、签名 |
| 会话 | 最近会话列表，带最后一条消息与未读数 |
| 消息 | 按会话与日期区间读取，语音自动转文字，图片自动内联，视频给封面 |
| 搜索 | 全局或指定单个、多个会话的关键词搜索 |
| 统计 | 单会话统计与跨会话活跃度总览 |
| 媒体 | 图片原图与高清降级、表情解密、语音导出 |

工具清单与参数以 USAGE-DSH.md 为准。

消息交付有两种形态：一是给 AI 在对话里读，默认 lite 档，每条只有 id、时间、发送者、正文与媒体；二是给脚本批量处理，走 CLI，默认 NDJSON 一行一条。要看 ID、类型与发送者映射时传 detail="full"，排查时传 detail="raw" 拿原始 XML。

## 命令行

MCP 是同参同义的薄壳，脚本场景直接用 CLI，默认输出 NDJSON，接管道与循环最省事：

```powershell
python -m src.cli messages --targets 某群 --start 2026-11-01 --limit 100
python -m src.cli list --scope group --sort members --limit 20 --json
python -m src.cli groups --members 甲 乙 --json
python -m src.cli messages --targets 某群 --format text
python -m src.cli export --targets 某群 --format md --out exports/mine
```

## 快速开始（Windows）

环境：Windows 10 与 11、微信 4.x 已登录、Python 3.11 以上、uv。

```powershell
uv sync
powershell -ExecutionPolicy Bypass -File .\start.ps1
```

首次运行会检测 decrypted/ 是否存在：不存在则提取密钥并全量解密；存在则只解源库比副本新的部分。刷新完不进常驻服务，直接用下面的 stdio 配置接入。

想提前把数据刷到最新时重跑一次即可（增量，日常一两秒）：

```powershell
uv run python scripts\quick_decrypt.py
```

需要常驻 HTTP 服务时加 -Serve，监听 http://localhost:18907/mcp。

## 接入客户端

推荐 stdio：客户端自己拉起进程，不用预热、不占端口。

```json
{
  "mcpServers": {
    "wechat-msg-mcp": {
      "command": "D:\\Github\\wechat-msg-mcp\\.venv\\Scripts\\python.exe",
      "args": ["-m", "src.server", "--transport", "stdio"],
      "cwd": "D:\\Github\\wechat-msg-mcp",
      "env": {
        "WECHAT_FILES_DIR": "F:\\xwechat_files",
        "WECHAT_DECRYPTED_DIR": "D:\\Github\\wechat-msg-mcp\\decrypted"
      }
    }
  }
}
```

也可以用常驻 HTTP 服务（需先 start.ps1 -Serve）：

```json
{
  "mcpServers": {
    "wechat-msg-mcp": {
      "url": "http://127.0.0.1:18907/mcp"
    }
  }
}
```

MCP 协议要求地址为 localhost 或 127.0.0.1，不能填局域网 IP。

## 配置

| 环境变量 | 作用 | 默认 |
|---|---|---|
| WECHAT_FILES_DIR | 微信数据目录 | start.ps1 内设置，本机为 F:\xwechat_files |
| WECHAT_DECRYPTED_DIR | 解密输出目录 | 项目内 decrypted/ |

## 项目结构

完整文件树、每个文件的职责与入库状态见 AGENTS.md 的「文件树」一节；文档各自的职责见 docs/索引.md。

## 文档

| 文件 | 内容 |
|---|---|
| docs/索引.md | 文档总索引 |
| docs/总体设计.md | 当前实现的数据流、模块与数据可得性 |
| docs/任务清单.md | 待修任务与优先级 |
| USAGE-DSH.md | 启动、刷新、工具速查与排障手册 |
| docs/重构方案-2026-11-13.md | 5 工具重构方案，讨论稿 |

## 安全说明

本工具只读访问微信数据，不修改源库，不发送任何消息。keys.json、wxecho_keys.json、decrypted/ 与 private-docs/ 均已加入 .gitignore。解密后的数据包含个人隐私，禁止提交或外传。请仅用于处理你本人合法持有的数据。

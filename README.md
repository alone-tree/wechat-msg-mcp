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

## 快速开始（Windows）

环境：Windows 10 与 11、微信 4.x 已登录、Python 3.11 以上、uv。

```powershell
uv sync
powershell -ExecutionPolicy Bypass -File .\start.ps1
```

首次运行会检测 decrypted/ 是否存在：不存在则提取密钥并全量解密；存在则用已有密钥增量刷新，然后启动服务，监听 http://localhost:18907/mcp。

日常刷新只在需要最新消息时执行：

```powershell
uv run python scripts\quick_decrypt.py
```

服务端口是 18907。若被占用，改 start.ps1 末尾的端口，并同步更新客户端里的 URL。

## 接入客户端

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

```text
src/
  server.py          MCP 服务入口与工具注册
  config.py          路径与密钥
  db/reader.py       SQLite 查询
  db/models.py       数据模型与文本渲染
  db/image.py        图片与表情解密
  db/voice.py        语音转写
  db/windows_reader.py  HTTP 回退（计划删除）
  tools/             工具实现
scripts/             密钥提取与解密
docs/                设计与任务文档
start.ps1            启动脚本
```

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

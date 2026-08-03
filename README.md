# 微信聊天记录 MCP Server

为 AI 客户端（Kiro / Claude Desktop 等）提供微信聊天记录的只读查询能力。

支持平台：
- **macOS**：微信 4.x，通过 lldb 提取密钥后本地解密数据库读取
- **Windows**：微信 4.x，通过 [WeChatDataAnalysis](https://github.com/LifeArchiveProject/WeChatDataAnalysis) 后端 HTTP API 读取

> **免责声明**：本工具仅用于个人数据备份与分析，请勿用于侵犯他人隐私。使用前请确保符合相关法律法规及微信服务条款。

---

## Windows 快速开始

### 环境要求

- Windows 10/11，微信 4.x（已登录）
- Python 3.10+，[uv](https://docs.astral.sh/uv/) 包管理器

### 步骤

**第一步：安装依赖**

```
uv sync
uv add yara-python pymem pefile
```

**第二步：首次解密数据库（需要管理员权限）**

以管理员权限运行 start.ps1，首次启动会自动检测并解密：

```powershell
powershell -ExecutionPolicy Bypass -File .\start.ps1
```

或手动运行解密脚本（管理员权限）：

```
uv run python scripts\decrypt_standalone.py
```

密钥自动提取并保存到 `keys.json`，数据库解密到 `decrypted/`。

**之后日常更新解密（无需管理员权限）：**

```
uv run python scripts\quick_decrypt.py
```

**第三步：启动 MCP Server（HTTP 模式）**

```powershell
powershell -ExecutionPolicy Bypass -File .\start.ps1
```

服务启动后监听在 `http://localhost:8765/mcp`。

**第四步：配置客户端**

编辑 `~/.kiro/settings/mcp.json`：

```json
{
  "mcpServers": {
    "wechat": {
      "url": "http://localhost:8765/mcp",
      "disabled": false
    }
  }
}
```

**第五步：验证**

在 Kiro 里调用 `check_status` 工具，确认联系人数量正常。

> **注意**：
> - MCP Server 以管理员权限运行时，密钥失效后会自动重新提取（10分钟冷却）
> - 消息同步约有 1~5 分钟延迟（依赖微信 WAL checkpoint 频率）
> - 如需立即获取最新消息，运行 `uv run python scripts\quick_decrypt.py` 后重启 Server
> - WeChatDataAnalysis 不再是必须依赖，仅作为回退方案

---

### Windows 回退模式（WeChatDataAnalysis）

若无法使用本地解密（如首次解密失败），可以使用 WeChatDataAnalysis 作为数据源：

1. 安装并启动 [WeChatDataAnalysis](https://github.com/LifeArchiveProject/WeChatDataAnalysis/releases/latest)，确保微信已登录
2. 后端自动运行在 `http://localhost:10392`
3. 启动 start.ps1，Server 会自动检测并使用 HTTP API 模式

---

## macOS 快速开始

### 环境要求

- macOS 11+，Apple Silicon（M1/M2/M3/M4）
- 微信 4.x（已登录），Python 3.10+
- [uv](https://docs.astral.sh/uv/) 包管理器
- Xcode Command Line Tools：`xcode-select --install`

### 整体流程

```
微信 Mac 客户端（运行中）
    ↓ scripts/extract_key.py（lldb 提取密钥）
keys.json（AES 密钥）
    ↓ scripts/decrypt_db.py（解密数据库）
decrypted/（标准 SQLite 文件）
    ↓ MCP Server（只读查询）
Kiro / Claude Desktop
```

### 步骤

**第一步：安装依赖**

```bash
uv sync
```

**第二步：微信重新签名（仅需一次，微信更新后重做）**

```bash
sudo codesign --force --deep --sign - /Applications/WeChat.app
```

**第三步：提取数据库密钥**

确保微信已登录并运行：

```bash
sudo python3 scripts/extract_key.py
```

密钥自动保存到 `keys.json`。

**第四步：解密数据库**

```bash
python3 scripts/decrypt_db.py
```

解密结果在 `decrypted/` 目录。

**第五步：配置客户端（stdio 模式，Kiro 自动启动）**

编辑 `~/.kiro/settings/mcp.json`：

```json
{
  "mcpServers": {
    "wechat": {
      "command": "uv",
      "args": [
        "--directory", "/path/to/wechat-msg-mcp",
        "run", "python", "-m", "src.server"
      ],
      "disabled": false
    }
  }
}
```

Claude Desktop 编辑 `~/Library/Application Support/Claude/claude_desktop_config.json`，格式相同。

**第五步（可选）：HTTP 模式**

如需以 HTTP 方式暴露（手动启动，客户端用 url 连接）：

```bash
uv run python -m src.server --transport streamable-http --host 0.0.0.0 --port 8765
```

然后 mcp.json 改为：

```json
{
  "mcpServers": {
    "wechat": {
      "url": "http://localhost:8765/mcp",
      "disabled": false
    }
  }
}
```

---

## 可用 Tools

| Tool | 说明 |
|------|------|
| `check_status` | 检查 Server 状态（数据源是否就绪） |
| `list_contacts` | 列出联系人和群聊 |
| `find_contact` | 查找联系人详细信息 |
| `get_sessions` | 获取最近会话列表 |
| `get_chat_history` | 获取与某人/群的聊天记录 |
| `search_messages` | 全局关键词搜索 |
| `get_recent_messages` | 获取最近 N 天的消息 |
| `summarize_chat` | 汇总某会话的统计信息 |
| `chat_stats_overview` | 所有会话的总览统计 |

### 使用示例

```
check_status()
list_contacts(keyword="王")
get_chat_history(contact_name="张总", start_date="2026-07-21")
search_messages(keyword="合同", contact_name="项目群")
summarize_chat(contact_name="工作群", period="this_week")
chat_stats_overview(period="last_30_days", top_n=10)
```

---

## 项目结构

```
wechat-msg-mcp/
├── src/
│   ├── server.py               # MCP Server 主入口（自动按平台和模式选择 Reader）
│   ├── config.py               # 路径配置（macOS）
│   ├── db/
│   │   ├── models.py           # 数据模型
│   │   ├── reader.py           # 本地 SQLite 读取（Mac + Windows 本地模式）
│   │   └── windows_reader.py   # Windows HTTP API 读取（回退模式）
│   └── tools/
│       ├── contacts.py
│       ├── messages.py
│       └── summary.py
├── scripts/
│   ├── extract_key.py          # macOS 密钥提取（lldb）
│   ├── decrypt_db.py           # 数据库解密（Mac & Windows 通用）
│   ├── decrypt_standalone.py   # Windows 独立解密（含密钥提取，需管理员）
│   ├── quick_decrypt.py        # Windows 快速重解密（用已有密钥，无需管理员）
│   └── extract_key_windows.py  # Windows 密钥提取（备用，内存扫描）
├── start.ps1                   # Windows HTTP 模式启动脚本（自动首次解密）
├── pyproject.toml
├── uv.lock
└── README.md
```

---

## 常见问题

**Q（Windows）：`无法连接到 WeChatDataAnalysis 后端`**  
A：请先启动 WeChatDataAnalysis，确保微信已登录，然后重试。

**Q（Windows）：如何修改后端地址？**  
A：设置环境变量 `WCDA_BASE_URL=http://localhost:你的端口`。

**Q（macOS）：`task_for_pid failed`**  
A：微信未完成 ad-hoc 重签名，或 extract_key.py 没有以 sudo 运行。

**Q（macOS）：解密后 SQLite 校验失败**  
A：密钥捕获时机不对，退出微信、运行 extract_key.py，然后重新登录微信。

**Q（macOS）：微信更新后密钥失效**  
A：重新签名微信，重新运行 extract_key.py 和 decrypt_db.py。

---

## 安全说明

- 本工具**只读访问**数据，不修改任何微信文件
- `keys.json`、`wxecho_keys.json`、`decrypted/` 已加入 `.gitignore`，不会提交到代码库
- Windows 模式下所有数据通过本地 HTTP API 读取，不离开本机

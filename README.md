# 微信聊天记录 MCP Server

为 AI 客户端（Kiro / Claude Desktop 等）提供微信聊天记录的只读查询能力。

支持平台：
- **macOS**：微信 4.x，通过 lldb 提取密钥后本地解密数据库读取
- **Windows**：微信 4.x，本地内存提取密钥 + 本地解密数据库读取（**不依赖 WeChatDataAnalysis**）

> **免责声明**：本工具仅用于个人数据备份与分析，请勿用于侵犯他人隐私。使用前请确保符合相关法律法规及微信服务条款。

---

## Windows 快速开始

### 环境要求

- Windows 10/11，微信 4.x（已登录且正在运行）
- Python 3.10+，[uv](https://docs.astral.sh/uv/) 包管理器
- **管理员权限**（首次解密必须，用于读取微信进程内存）

### 步骤

**第一步：安装依赖**

```powershell
uv sync
uv add yara-python pymem pefile
```

**第二步：按需修改 start.ps1 中的路径**

用文本编辑器打开 `start.ps1`，确认以下两行是否与你的机器一致：

```powershell
# 微信聊天数据目录（xwechat_files 的实际路径，因人而异）
$env:WECHAT_FILES_DIR = "C:\Users\你的用户名\xwechat_files"

# 可选：自定义解密输出目录（默认为项目内 decrypted/）
# $env:WECHAT_DECRYPTED_DIR = "D:\你的路径\decrypted"
```

> 微信数据目录默认在 `C:\Users\<用户名>\xwechat_files` 或 `C:\Users\<用户名>\Documents\WeChat Files`，
> 请根据你的实际路径修改。

**第三步：首次启动（自动解密 + 启动 Server）**

以**管理员权限**运行 PowerShell，执行：

```powershell
powershell -ExecutionPolicy Bypass -File .\start.ps1
```

- 首次运行会检测到 `decrypted/` 目录不存在，自动运行 `decrypt_standalone.py` 提取密钥并解密
- 密钥保存到 `keys.json`，数据库解密到 `decrypted/`
- 解密完成后自动启动 MCP Server，监听 `http://localhost:8765/mcp`

**之后日常启动：**

```powershell
powershell -ExecutionPolicy Bypass -File .\start.ps1
```

已有解密数据库时直接启动 Server，无需重新解密。

**手动更新解密（获取最新消息）：**

```powershell
# 无需管理员权限，用 keys.json 里已有密钥重新解密
uv run python scripts\quick_decrypt.py
```

**第四步：配置客户端**

编辑 `~/.kiro/settings/mcp.json`（Kiro）或 `claude_desktop_config.json`（Claude Desktop）：

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

> **注意**：MCP url 必须是 `localhost` 或 `https`，不能填局域网 IP（如 `10.x.x.x`）。
> 如需跨机访问，参考下方"迁移到新机器"章节。

**第五步：验证**

在 Kiro 里调用 `check_status` 工具，确认状态显示"本地解密数据库"而非"WeChatDataAnalysis"。

---

### Windows 消息同步说明

- 消息同步有 **1~5 分钟延迟**，依赖微信自动 WAL checkpoint 频率
- 微信切换聊天窗口会触发 checkpoint，之后运行 `quick_decrypt.py` 可立即获取最新消息
- MCP Server 以管理员权限运行时，密钥失效后会**自动重新提取**（10 分钟冷却）

---

### Windows 回退模式（WeChatDataAnalysis）

若本地解密失败（如首次提取密钥失败），可以使用 WeChatDataAnalysis 作为数据源：

1. 安装并启动 [WeChatDataAnalysis](https://github.com/LifeArchiveProject/WeChatDataAnalysis/releases/latest)，确保微信已登录
2. 后端自动运行在 `http://localhost:10392`
3. 启动 start.ps1，Server 会自动检测并使用 HTTP API 模式

---

## 迁移到新机器（Windows）

在新机器上部署时，以下几点需要手动确认：

### 1. 修改 start.ps1 中的数据目录

```powershell
# 根据新机器实际路径修改
$env:WECHAT_FILES_DIR = "C:\Users\新用户名\xwechat_files"
```

### 2. Weixin.dll 自动查找失败时手动指定

脚本通过注册表 `HKLM\SOFTWARE\WOW6432Node\Tencent\WeChat\InstallPath` 自动查找 Weixin.dll。
如果自动查找失败，先手动搜索：

```powershell
Get-ChildItem "C:\Program Files\Tencent" -Recurse -Filter "Weixin.dll" -ErrorAction SilentlyContinue | Select-Object FullName
```

找到路径后，设置环境变量再运行解密：

```powershell
$env:WECHAT_FILES_DIR = "C:\Users\你的用户名\xwechat_files"
$env:WEIXIN_DLL = "C:\Program Files\Tencent\Weixin\install\x.x.x\Weixin.dll"
uv run python scripts\decrypt_standalone.py
```

### 3. 依赖安装

```powershell
uv sync
uv add yara-python pymem pefile
```

### 4. 检查注册表（诊断用）

```powershell
Get-ItemProperty -Path "HKLM:\SOFTWARE\WOW6432Node\Tencent\WeChat" -Name "InstallPath" -ErrorAction SilentlyContinue
Get-ItemProperty -Path "HKLM:\SOFTWARE\Tencent\WeChat" -Name "InstallPath" -ErrorAction SilentlyContinue
```

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
│   ├── server.py               # MCP Server 主入口（优先本地 SQLite，回退 HTTP API）
│   ├── config.py               # 路径配置
│   ├── db/
│   │   ├── models.py           # 数据模型
│   │   ├── reader.py           # 本地 SQLite 读取（Mac + Windows 本地模式）
│   │   └── windows_reader.py   # Windows HTTP API 读取（WeChatDataAnalysis 回退）
│   └── tools/
│       ├── contacts.py
│       ├── messages.py
│       └── summary.py
├── scripts/
│   ├── extract_key.py          # macOS 密钥提取（lldb）
│   ├── decrypt_db.py           # 数据库解密（Mac & Windows 通用）
│   ├── decrypt_standalone.py   # Windows 独立解密（含密钥提取，需管理员权限）
│   ├── quick_decrypt.py        # Windows 快速重解密（用已有密钥，无需管理员）
│   └── extract_key_windows.py  # Windows 密钥提取（备用）
├── start.ps1                   # Windows 启动脚本（首次自动解密 + 启动 Server）
├── pyproject.toml
├── uv.lock
└── README.md
```

---

## 常见问题

**Q（Windows）：找不到 Weixin.dll**  
A：脚本通过注册表自动定位，若失败请手动搜索并设置环境变量：
```powershell
Get-ChildItem "C:\Program Files\Tencent" -Recurse -Filter "Weixin.dll" -ErrorAction SilentlyContinue | Select-Object FullName
$env:WEIXIN_DLL = "找到的完整路径\Weixin.dll"
uv run python scripts\decrypt_standalone.py
```

**Q（Windows）：找不到 xwechat_files 目录**  
A：需要在 start.ps1 里修改 `WECHAT_FILES_DIR` 为你机器上的实际路径。也可以在运行前设置环境变量：
```powershell
$env:WECHAT_FILES_DIR = "C:\Users\你的用户名\xwechat_files"
```

**Q（Windows）：端口 8765 被占用**  
A：已有 Server 实例在运行，关闭后重新启动 start.ps1。或修改端口：
```powershell
uv run python -m src.server --transport streamable-http --host 0.0.0.0 --port 8766
```

**Q（Windows）：`check_status` 显示需要 WeChatDataAnalysis**  
A：说明本地 `decrypted/` 目录不存在或为空，需要先运行 `decrypt_standalone.py`（管理员权限）。

**Q（Windows）：`decrypt_standalone.py` 无输出直接退出**  
A：可能是权限问题（未以管理员运行），或微信未运行。确保微信已登录，并以管理员权限的 PowerShell 执行。

**Q（Windows）：MCP 配置了 IP 地址报错**  
A：MCP 协议要求 url 必须是 `localhost` 或 `https://`，不支持局域网 IP。Server 和客户端须在同一台机器上运行。

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
- Windows 模式所有数据在本机本地读取，不离开本机

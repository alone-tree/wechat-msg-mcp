# 微信聊天记录 MCP Server

基于 Mac 本地数据库，为 AI 客户端（Kiro / Claude Desktop 等）提供微信聊天记录的只读查询能力。

> **免责声明**：本工具仅用于个人数据备份与分析，请勿用于侵犯他人隐私。使用前请确保符合相关法律法规及微信服务条款。

---

## 整体流程

```
微信 Mac 客户端（运行中）
    ↓ scripts/extract_key.py（lldb 提取密钥）
keys.json（AES 密钥）
    ↓ scripts/decrypt_db.py（解密数据库）
decrypted/（标准 SQLite 文件）
    ↓ MCP Server（只读查询）
Kiro / Claude Desktop
```

---

## 环境要求

- macOS 11+，Apple Silicon（M1/M2/M3/M4）
- 微信 4.x（已登录，聊天记录已同步）
- Python 3.10+
- [uv](https://docs.astral.sh/uv/) 包管理器
- Xcode Command Line Tools：`xcode-select --install`

---

## 安装依赖

```bash
cd /path/to/wechat-message
uv sync
```

---

## 第一步：微信重新签名（仅需一次）

微信默认使用 Apple 开发者证书签名，lldb 无法附加。需要换成 ad-hoc 签名：

```bash
# 先退出微信
sudo codesign --force --deep --sign - /Applications/WeChat.app
```

签名完成后重新打开微信并登录。  
**注意：每次微信更新后需重新签名。**

---

## 第二步：提取数据库密钥

```bash
# 确保微信正在运行且已登录
sudo python3 scripts/extract_key.py
```

脚本会自动附加到微信进程，捕获 SQLCipher 密钥，保存到 `keys.json`。

### 手动提取（备选方案）

如果自动提取失败，可使用 lldb 手动操作：

```bash
# 1. 以 sudo 启动 lldb 并附加微信
sudo lldb -n WeChat

# 2. 在 lldb 中执行：
(lldb) breakpoint set --name sqlite3_key
(lldb) breakpoint command add 1
# 输入以下 Python 代码，空行结束：
    import lldb
    frame = lldb.frame
    rsi = frame.FindRegister("rsi").GetValueAsUnsigned()
    rdx = frame.FindRegister("rdx").GetValueAsUnsigned()
    error = lldb.SBError()
    key = frame.thread.process.ReadMemory(rsi, rdx, error)
    if error.Success():
        print(f"KEY: {key.hex()}")
    
(lldb) continue

# 3. 微信登录后会打印类似：
#    KEY: aabbccdd...（64+ 字节的 hex）

# 4. 将输出的 hex 保存到 keys.json：
```

```json
{
  "key_0": "x'<你复制的hex字符串>'"
}
```

---

## 第三步：解密数据库

```bash
python3 scripts/decrypt_db.py
```

解密后的数据库保存在 `decrypted/` 目录：

```
decrypted/
├── contact/contact.db      # 联系人
├── session/session.db      # 会话列表
├── message/
│   ├── message_0.db        # 聊天记录（分片）
│   ├── message_1.db
│   └── ...
└── group/group.db          # 群组信息
```

---

## 第四步：配置 MCP Server

### Kiro

编辑 `~/.kiro/settings/mcp.json`，添加：

```json
{
  "mcpServers": {
    "wechat": {
      "command": "uv",
      "args": [
        "--directory",
        "/Users/wangfan/IdeaProjects/code/zhenling/wechat-message",
        "run",
        "python",
        "-m",
        "src.server"
      ],
      "disabled": false
    }
  }
}
```

### Claude Desktop

编辑 `~/Library/Application Support/Claude/claude_desktop_config.json`：

```json
{
  "mcpServers": {
    "wechat": {
      "command": "uv",
      "args": [
        "--directory",
        "/Users/wangfan/IdeaProjects/code/zhenling/wechat-message",
        "run",
        "python",
        "-m",
        "src.server"
      ]
    }
  }
}
```

### 自定义解密目录

如果解密数据库不在默认的 `decrypted/` 目录，设置环境变量：

```bash
export WECHAT_DECRYPTED_DIR=/your/custom/path
```

或在 MCP 配置的 `env` 字段中指定：

```json
{
  "mcpServers": {
    "wechat": {
      "command": "uv",
      "args": ["--directory", "/path/to/project", "run", "python", "-m", "src.server"],
      "env": {
        "WECHAT_DECRYPTED_DIR": "/your/decrypted/path"
      }
    }
  }
}
```

---

## 可用 Tools

| Tool | 说明 |
|------|------|
| `check_status` | 检查 Server 状态和数据库就绪情况 |
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
# 查找联系人
list_contacts(keyword="王")

# 获取最近7天与某客户的聊天
get_chat_history(contact_name="张总", start_date="2026-07-21")

# 搜索关键词
search_messages(keyword="合同", contact_name="项目群")

# 生成本周聊天统计
summarize_chat(contact_name="工作群", period="this_week")

# 查看最活跃的10个会话
chat_stats_overview(period="last_30_days", top_n=10)
```

---

## 项目结构

```
wechat-message/
├── src/
│   ├── server.py           # MCP Server 主入口（8个 Tools）
│   ├── config.py           # 路径配置和目录管理
│   ├── db/
│   │   ├── models.py       # 数据模型（Contact / Message / Session）
│   │   └── reader.py       # 数据库读取层
│   └── tools/
│       ├── contacts.py     # 联系人 Tools
│       ├── messages.py     # 消息 Tools
│       └── summary.py      # 统计汇总 Tools
├── scripts/
│   ├── extract_key.py      # 密钥提取（lldb）
│   └── decrypt_db.py       # 数据库解密
├── decrypted/              # 解密后的数据库（运行后生成）
├── keys.json               # 提取的密钥（运行后生成）
└── pyproject.toml
```

---

## 常见问题

**Q: `task_for_pid failed`**  
A: 微信未完成 ad-hoc 重签名，或 extract_key.py 没有以 sudo 运行。

**Q: 解密后 SQLite 校验失败**  
A: 密钥捕获时机不对，重新退出微信、运行 extract_key.py，然后重新登录微信。

**Q: 联系人列表为空**  
A: contact.db 解密失败，检查 decrypted/contact/ 目录下是否有 contact.db，并确认文件大小非零。

**Q: 消息内容显示 `[内容解析失败]`**  
A: 该条消息使用了 zstd 压缩，确认已安装 `zstandard` 依赖（`uv sync` 会自动安装）。

**Q: 微信更新后密钥失效**  
A: 重新签名微信（`sudo codesign --force --deep --sign - /Applications/WeChat.app`），重新运行 extract_key.py 和 decrypt_db.py。

---

## 安全说明

- 本工具**只读访问**解密后的数据库，不修改任何微信文件
- `keys.json` 和 `decrypted/` 目录包含敏感数据，已加入 `.gitignore`
- 建议将 `decrypted/` 放在加密磁盘分区或通过 `WECHAT_DECRYPTED_DIR` 指向安全位置

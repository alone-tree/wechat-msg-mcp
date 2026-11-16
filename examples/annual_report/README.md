# 年度报告示例

这是一个独立示例，不属于 wechat-msg-mcp 包，包与 CLI 也没有引用它。
删掉整个 examples/annual_report 目录，工具照常工作。

用途：扫描已解密的微信聊天库，汇总成一份年度报告，产物是六份文件：
report.json、report.md、people.csv、groups.csv、monthly.csv、messages.csv。
需要别的口径时，直接改这里的代码，或者照着重写一份。

## 运行

在仓库根目录，需要项目依赖与 decrypted/ 数据：

```powershell
.venv/Scripts/python.exe examples/annual_report/__main__.py --year 2025 --out reports/2025
```

也可以用 uv run python examples/annual_report/__main__.py --year 2025。
默认输出到 reports/<year>/；报告里只用哈希与地址簿显示名，不含 wxid 与真实群名。

## 测试

```powershell
uv run --with pytest pytest examples/annual_report/tests -q
```

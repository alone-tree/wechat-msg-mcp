Write-Host "Starting WeChat MCP Server on http://localhost:8765/mcp ..."
Write-Host "Make sure WeChatDataAnalysis is running first."
Write-Host "Press Ctrl+C to stop."
Write-Host ""
& "C:\Users\王帆\.local\bin\uv.exe" --directory "D:\work\code\wechat-msg-mcp" run python -m src.server --transport streamable-http --host 0.0.0.0 --port 8765

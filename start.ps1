$isAdmin = ([Security.Principal.WindowsPrincipal][Security.Principal.WindowsIdentity]::GetCurrent()).IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)
if (-not $isAdmin) {
    Write-Host "WARNING: Not running as Administrator." -ForegroundColor Yellow
    Write-Host "Automatic key refresh will not work without admin rights." -ForegroundColor Yellow
    Write-Host ""
}
$uvPath = Join-Path $env:USERPROFILE ".local\bin\uv.exe"
if ($PSScriptRoot -ne $null -and $PSScriptRoot -ne "") {
    $projectDir = $PSScriptRoot
} else {
    $projectDir = Split-Path -Parent $MyInvocation.MyCommand.Definition
}

# 可选：自定义微信数据目录（新机器路径不同时设置）
$env:WECHAT_FILES_DIR = "F:\xwechat_files"

# 解密输出目录：2026-10-16 起统一用项目内 decrypted（与 keys.json、服务读取目录一致）。
# 旧外部目录 D:\Github\wechat-msg-mcp-data\decrypted 是 9 月 12 日的过期快照，已弃用。
$env:WECHAT_DECRYPTED_DIR = Join-Path $projectDir "decrypted"

$decryptedDir = if ($env:WECHAT_DECRYPTED_DIR) { $env:WECHAT_DECRYPTED_DIR } else { Join-Path $projectDir "decrypted" }
$contactDb = Join-Path $decryptedDir "contact\contact.db"
if (-not (Test-Path $contactDb)) {
    Write-Host "First run detected, decrypting databases..." -ForegroundColor Cyan
    Write-Host "This may take a minute. Please wait." -ForegroundColor Cyan
    Write-Host ""
    & $uvPath --directory $projectDir run python scripts\decrypt_standalone.py
    if ($LASTEXITCODE -ne 0) {
        Write-Host "Decryption failed. Please check the error above." -ForegroundColor Red
        pause
        exit 1
    }
    Write-Host ""
    Write-Host "Decryption complete." -ForegroundColor Green
    Write-Host ""
}
elseif (Test-Path (Join-Path $projectDir "keys.json")) {
    # 每次启动自动刷新：用已保存的密钥重新解密最新数据（无需管理员，微信开着效果最好）
    Write-Host "Auto-refreshing decrypted data with saved keys (quick_decrypt)..." -ForegroundColor Cyan
    & $uvPath --directory $projectDir run python scripts\quick_decrypt.py
    if ($LASTEXITCODE -ne 0) {
        Write-Host "Auto-refresh failed (non-fatal), serving existing decrypted data." -ForegroundColor Yellow
    }
    Write-Host ""
}
Write-Host "Updating search index (incremental)..." -ForegroundColor Cyan
& $uvPath --directory $projectDir run python -m src.cli index
if ($LASTEXITCODE -ne 0) {
    Write-Host "Index update failed (non-fatal), search may be stale." -ForegroundColor Yellow
}
Write-Host ""
Write-Host "Starting WeChat MCP Server on http://localhost:18907/mcp ..."
Write-Host "Press Ctrl+C to stop."
Write-Host ""
& $uvPath --directory $projectDir run python -m src.server --transport streamable-http --host 0.0.0.0 --port 18907

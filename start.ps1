param([switch]$Serve)

# 2026-11-14：MCP 改为 stdio 模式，默认只刷新数据不进常驻服务。
# 用 -Serve 才起 HTTP 服务（兼容旧用法与其它客户端）。

$uvPath = Join-Path $env:USERPROFILE ".local\bin\uv.exe"
if ($PSScriptRoot -ne $null -and $PSScriptRoot -ne "") {
    $projectDir = $PSScriptRoot
} else {
    $projectDir = Split-Path -Parent $MyInvocation.MyCommand.Definition
}

# 微信数据目录：换机器时改这里
$env:WECHAT_FILES_DIR = "F:\xwechat_files"
# 解密输出目录：与 keys.json、服务读取目录一致
$env:WECHAT_DECRYPTED_DIR = Join-Path $projectDir "decrypted"

$decryptedDir = $env:WECHAT_DECRYPTED_DIR
$contactDb = Join-Path $decryptedDir "contact\contact.db"
if (-not (Test-Path $contactDb)) {
    Write-Host "First run detected, decrypting all databases..." -ForegroundColor Cyan
    Write-Host "Needs Administrator and WeChat running. May take a minute." -ForegroundColor Cyan
    Write-Host ""
    & $uvPath --directory $projectDir run python scripts\decrypt_standalone.py
    if ($LASTEXITCODE -ne 0) {
        Write-Host "Decryption failed. Please check the error above." -ForegroundColor Red
        pause
        exit 1
    }
    Write-Host "Decryption complete." -ForegroundColor Green
}
elseif (Test-Path (Join-Path $projectDir "keys.json")) {
    Write-Host "Refreshing decrypted data (incremental)..." -ForegroundColor Cyan
    & $uvPath --directory $projectDir run python scripts\quick_decrypt.py
    if ($LASTEXITCODE -ne 0) {
        Write-Host "Refresh failed (non-fatal), serving existing data." -ForegroundColor Yellow
    }
}
else {
    Write-Host "keys.json not found, cannot refresh. Run scripts\decrypt_standalone.py first." -ForegroundColor Yellow
}

Write-Host ""
Write-Host "Updating search index (incremental)..." -ForegroundColor Cyan
& $uvPath --directory $projectDir run python -m src.cli index
if ($LASTEXITCODE -ne 0) {
    Write-Host "Index update failed (non-fatal), search may be stale." -ForegroundColor Yellow
}

if ($Serve) {
    Write-Host ""
    Write-Host "Starting persistent HTTP server on http://127.0.0.1:18907/mcp ..." -ForegroundColor Cyan
    Write-Host "Press Ctrl+C to stop." -ForegroundColor Cyan
    Write-Host ""
    & $uvPath --directory $projectDir run python -m src.server --transport streamable-http --host 127.0.0.1 --port 18907
}
else {
    Write-Host ""
    Write-Host "Data refreshed. MCP runs in stdio mode: the client spawns it on demand, no server needed." -ForegroundColor Green
    Write-Host "Add -Serve if you want the persistent HTTP server instead." -ForegroundColor Gray
}

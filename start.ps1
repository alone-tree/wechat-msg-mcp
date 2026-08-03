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
$decryptedDir = Join-Path $projectDir "decrypted"
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
Write-Host "Starting WeChat MCP Server on http://localhost:8765/mcp ..."
Write-Host "Press Ctrl+C to stop."
Write-Host ""
& $uvPath --directory $projectDir run python -m src.server --transport streamable-http --host 0.0.0.0 --port 8765

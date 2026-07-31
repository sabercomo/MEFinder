$ErrorActionPreference = "Stop"

Write-Host ""
Write-Host "This legacy entry now opens the MinerU API Token setup." -ForegroundColor Cyan
Write-Host "The current MinerU precise parsing API requires an API Token." -ForegroundColor Yellow
Write-Host "Access Key ID / Secret Access Key cannot be used in its place." -ForegroundColor Yellow
Write-Host ""

$tokenSetupPath = Join-Path $PSScriptRoot "setup_mineru_token.ps1"
if (-not (Test-Path $tokenSetupPath)) {
    throw "Token setup script not found: $tokenSetupPath"
}

& $tokenSetupPath

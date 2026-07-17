$ErrorActionPreference = "Stop"

$repoRoot = Resolve-Path (Join-Path $PSScriptRoot "..")
$configDir = Join-Path $repoRoot "config"
$configPath = Join-Path $configDir "mineru_api.local.json"

New-Item -ItemType Directory -Force -Path $configDir | Out-Null

Write-Host ""
Write-Host "MinerU / OpenDataLab API key setup" -ForegroundColor Cyan
Write-Host "Do not use a key that has been posted in chat or screenshots." -ForegroundColor Yellow
Write-Host ""

$accessKeyId = Read-Host "Paste NEW Access Key ID"
if ([string]::IsNullOrWhiteSpace($accessKeyId)) {
    throw "Access Key ID cannot be empty."
}

$secureSecret = Read-Host "Paste NEW Secret Access Key (input is hidden)" -AsSecureString
$secretPtr = [Runtime.InteropServices.Marshal]::SecureStringToBSTR($secureSecret)
try {
    $secretAccessKey = [Runtime.InteropServices.Marshal]::PtrToStringBSTR($secretPtr)
}
finally {
    [Runtime.InteropServices.Marshal]::ZeroFreeBSTR($secretPtr)
}

if ([string]::IsNullOrWhiteSpace($secretAccessKey)) {
    throw "Secret Access Key cannot be empty."
}

$apiBase = Read-Host "API base URL (press Enter to leave blank)"
$secureToken = Read-Host "Bearer Token (press Enter if MinerU only gave AK/SK)" -AsSecureString
$tokenPtr = [Runtime.InteropServices.Marshal]::SecureStringToBSTR($secureToken)
try {
    $token = [Runtime.InteropServices.Marshal]::PtrToStringBSTR($tokenPtr)
}
finally {
    [Runtime.InteropServices.Marshal]::ZeroFreeBSTR($tokenPtr)
}

$config = [ordered]@{
    access_key_id = $accessKeyId.Trim()
    secret_access_key = $secretAccessKey.Trim()
    token = $token.Trim()
    api_base = $apiBase.Trim()
    note = "Local private MinerU/OpenDataLab credentials. Do not share this file."
}

$json = $config | ConvertTo-Json -Depth 4
[System.IO.File]::WriteAllText($configPath, $json + [Environment]::NewLine, [System.Text.Encoding]::UTF8)

Write-Host ""
Write-Host "Saved:" -ForegroundColor Green
Write-Host $configPath
Write-Host ""
Write-Host "You can close this window." -ForegroundColor Green
Read-Host "Press Enter to exit"

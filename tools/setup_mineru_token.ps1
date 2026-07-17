$ErrorActionPreference = "Stop"

$repoRoot = Resolve-Path (Join-Path $PSScriptRoot "..")
$configDir = Join-Path $repoRoot "config"
$configPath = Join-Path $configDir "mineru_api.local.json"

New-Item -ItemType Directory -Force -Path $configDir | Out-Null

Write-Host ""
Write-Host "MinerU Bearer Token setup" -ForegroundColor Cyan
Write-Host "Use this only for the MinerU API Token / Bearer Token, not the Access Key ID." -ForegroundColor Yellow
Write-Host ""

if (Test-Path $configPath) {
    $text = [System.IO.File]::ReadAllText($configPath, [System.Text.Encoding]::UTF8)
    $config = $text | ConvertFrom-Json
}
else {
    $config = [pscustomobject]@{}
}

$secureToken = Read-Host "Paste MinerU Bearer Token (input is hidden)" -AsSecureString
$tokenPtr = [Runtime.InteropServices.Marshal]::SecureStringToBSTR($secureToken)
try {
    $token = [Runtime.InteropServices.Marshal]::PtrToStringBSTR($tokenPtr)
}
finally {
    [Runtime.InteropServices.Marshal]::ZeroFreeBSTR($tokenPtr)
}

if ([string]::IsNullOrWhiteSpace($token)) {
    throw "Bearer Token cannot be empty."
}

$out = [ordered]@{}
foreach ($name in $config.PSObject.Properties.Name) {
    if ($name -ne "token") {
        $out[$name] = $config.$name
    }
}
$out["token"] = $token.Trim()
if (-not $out.Contains("api_base")) {
    $out["api_base"] = ""
}
if (-not $out.Contains("note")) {
    $out["note"] = "Local private MinerU/OpenDataLab credentials. Do not share this file."
}

$json = $out | ConvertTo-Json -Depth 6
[System.IO.File]::WriteAllText($configPath, $json + [Environment]::NewLine, [System.Text.Encoding]::UTF8)

Write-Host ""
Write-Host "Saved Bearer Token to:" -ForegroundColor Green
Write-Host $configPath
Write-Host ""
Write-Host "You can close this window." -ForegroundColor Green
Read-Host "Press Enter to exit"

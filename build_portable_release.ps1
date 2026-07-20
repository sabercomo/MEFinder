param(
    [string]$Version = "0.1.0"
)

$ErrorActionPreference = "Stop"
$ProjectRoot = Split-Path -Parent $MyInvocation.MyCommand.Path
$ReleaseRoot = Join-Path $ProjectRoot "release"
$PackageName = "MEFinder-v$Version-windows-portable"
$StagePath = Join-Path $ReleaseRoot $PackageName
$ZipPath = Join-Path $ReleaseRoot "$PackageName.zip"
$HashPath = "$ZipPath.sha256.txt"
$DistPath = Join-Path $ProjectRoot "dist\MEFinder"

$releaseFull = [IO.Path]::GetFullPath($ReleaseRoot).TrimEnd('\')
$stageFull = [IO.Path]::GetFullPath($StagePath)
if (-not $stageFull.StartsWith($releaseFull + '\', [StringComparison]::OrdinalIgnoreCase)) {
    throw "Unsafe release staging path: $stageFull"
}

Push-Location $ProjectRoot
try {
    py -3 -m PyInstaller desktop.spec --clean --noconfirm
    if ($LASTEXITCODE -ne 0) { throw "PyInstaller build failed." }

    New-Item -ItemType Directory -Force -Path $ReleaseRoot | Out-Null
    if (Test-Path -LiteralPath $StagePath) {
        Remove-Item -LiteralPath $StagePath -Recurse -Force
    }
    if (Test-Path -LiteralPath $ZipPath) {
        Remove-Item -LiteralPath $ZipPath -Force
    }
    if (Test-Path -LiteralPath $HashPath) {
        Remove-Item -LiteralPath $HashPath -Force
    }

    Copy-Item -LiteralPath $DistPath -Destination $StagePath -Recurse
    New-Item -ItemType Directory -Force -Path (Join-Path $StagePath "data") | Out-Null
    New-Item -ItemType Directory -Force -Path (Join-Path $StagePath "config") | Out-Null
    New-Item -ItemType File -Force -Path (Join-Path $StagePath "portable.flag") | Out-Null

    Copy-Item -LiteralPath "config\pdf_imports.empty.json" -Destination (Join-Path $StagePath "config\pdf_imports.json")
    Copy-Item -LiteralPath "config\mineru_api.local.example.json" -Destination (Join-Path $StagePath "config\mineru_api.local.example.json")
    Copy-Item -LiteralPath "PORTABLE_README.md" -Destination (Join-Path $StagePath "README.md")
    py -3 -m tools.create_empty_index (Join-Path $StagePath "data\index.sqlite3")
    if ($LASTEXITCODE -ne 0) { throw "Blank index creation failed." }

    $forbiddenNames = @(
        "mineru_api.local.json",
        "preferences.json",
        "pdf_imports.json.pre-restore",
        "index.json",
        "desktop.log"
    )
    $forbidden = Get-ChildItem -LiteralPath $StagePath -Recurse -Force | Where-Object {
        $forbiddenNames -contains $_.Name -or $_.Name -eq "corpus" -or $_.FullName -like "*\corpus\*"
    }
    if ($forbidden) {
        $paths = ($forbidden | ForEach-Object FullName) -join "`n"
        throw "Release contains private or generated data:`n$paths"
    }

    $blankIndex = Get-Item -LiteralPath (Join-Path $StagePath "data\index.sqlite3")
    if ($blankIndex.Length -gt 1MB) {
        throw "Blank index is unexpectedly large: $($blankIndex.Length) bytes"
    }

    py -3 -m tools.create_portable_zip $StagePath $ZipPath
    if ($LASTEXITCODE -ne 0) { throw "Portable ZIP creation failed." }
    $hash = (Get-FileHash -Algorithm SHA256 -LiteralPath $ZipPath).Hash.ToLowerInvariant()
    Set-Content -LiteralPath $HashPath -Encoding Ascii -Value "$hash  $PackageName.zip"

    $zip = Get-Item -LiteralPath $ZipPath
    Write-Output "Portable release: $($zip.FullName)"
    Write-Output "Size: $($zip.Length) bytes"
    Write-Output "SHA256: $hash"
}
finally {
    Pop-Location
}

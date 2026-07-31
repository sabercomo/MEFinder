param(
    [string]$Version = "",
    [string]$ISCCPath = "",
    [string]$PythonExe = ""
)

$ErrorActionPreference = "Stop"
$ProjectRoot = Split-Path -Parent $MyInvocation.MyCommand.Path
$ReleaseRoot = Join-Path $ProjectRoot "release"
$DistPath = Join-Path $ProjectRoot "dist\MEFinder"

function Find-InnoCompiler {
    param([string]$ExplicitPath)

    $candidates = @()
    if ($ExplicitPath) { $candidates += $ExplicitPath }
    if ($env:ISCC_PATH) { $candidates += $env:ISCC_PATH }

    $command = Get-Command ISCC.exe -ErrorAction SilentlyContinue
    if ($command) { $candidates += $command.Source }

    if (${env:ProgramFiles(x86)}) {
        $candidates += (Join-Path ${env:ProgramFiles(x86)} "Inno Setup 7\ISCC.exe")
        $candidates += (Join-Path ${env:ProgramFiles(x86)} "Inno Setup 6\ISCC.exe")
    }
    if ($env:ProgramFiles) {
        $candidates += (Join-Path $env:ProgramFiles "Inno Setup 7\ISCC.exe")
        $candidates += (Join-Path $env:ProgramFiles "Inno Setup 6\ISCC.exe")
    }
    if ($env:LOCALAPPDATA) {
        $candidates += (Join-Path $env:LOCALAPPDATA "Programs\Inno Setup 7\ISCC.exe")
        $candidates += (Join-Path $env:LOCALAPPDATA "Programs\Inno Setup 6\ISCC.exe")
    }

    foreach ($candidate in ($candidates | Select-Object -Unique)) {
        if ($candidate -and (Test-Path -LiteralPath $candidate -PathType Leaf)) {
            return [IO.Path]::GetFullPath($candidate)
        }
    }
    throw "Inno Setup compiler (ISCC.exe) was not found. Install Inno Setup 6.3+ or 7, or pass -ISCCPath."
}

Push-Location $ProjectRoot
try {
    if ($PythonExe) {
        $pythonCommand = $PythonExe
        $pythonLauncherArgs = @()
    }
    else {
        $pythonCommand = "py"
        $pythonLauncherArgs = @("-3")
    }

    $pythonInfoOutput = & $pythonCommand @pythonLauncherArgs -c "import struct, sys; print(f'{sys.version_info.major}.{sys.version_info.minor}.{sys.version_info.micro}|{struct.calcsize(chr(80)) * 8}')"
    if ($LASTEXITCODE -ne 0) { throw "Could not start the selected Python interpreter." }
    $pythonInfo = ($pythonInfoOutput | Out-String).Trim().Split('|')
    if ($pythonInfo.Count -ne 2) { throw "Could not determine the selected Python version and architecture." }
    if ([Version]$pythonInfo[0] -lt [Version]"3.11.0") {
        throw "Windows releases require Python 3.11 or newer; selected interpreter is $($pythonInfo[0])."
    }
    if ($pythonInfo[1] -ne "64") {
        throw "Windows releases require 64-bit Python; selected interpreter is $($pythonInfo[1])-bit."
    }

    $sourceVersionOutput = & $pythonCommand @pythonLauncherArgs -c "from src.me_finder import __version__; print(__version__)"
    if ($LASTEXITCODE -ne 0) { throw "Could not read src.me_finder.__version__." }
    $sourceVersion = ($sourceVersionOutput | Out-String).Trim()
    if ($Version -and $Version -ne $sourceVersion) {
        throw "-Version '$Version' does not match src.me_finder.__version__ '$sourceVersion'."
    }
    $Version = $sourceVersion
    if ($Version -notmatch '^\d+\.\d+\.\d+$') {
        throw "Release version must use numeric major.minor.patch form: $Version"
    }

    $PackageName = "MEFinder-v$Version-windows-setup"
    $InstallerPath = Join-Path $ReleaseRoot "$PackageName.exe"
    $HashPath = "$InstallerPath.sha256.txt"
    $releaseFull = [IO.Path]::GetFullPath($ReleaseRoot).TrimEnd('\')
    $installerFull = [IO.Path]::GetFullPath($InstallerPath)
    if (-not $installerFull.StartsWith($releaseFull + '\', [StringComparison]::OrdinalIgnoreCase)) {
        throw "Unsafe installer output path: $installerFull"
    }

    $compiler = Find-InnoCompiler -ExplicitPath $ISCCPath

    & $pythonCommand @pythonLauncherArgs -m unittest `
        tests.test_anchor_metadata `
        tests.test_api_fallback_recovery `
        tests.test_backup_service `
        tests.test_batch_document_removal `
        tests.test_citations `
        tests.test_database_resilience `
        tests.test_large_index_resilience `
        tests.test_library_startup_performance `
        tests.test_pdf_import_config `
        tests.test_import_config_concurrency `
        tests.test_long_filename_import `
        tests.test_pdf_match_anchors `
        tests.test_page_display `
        tests.test_runtime_page_mapping `
        tests.test_search_match_spans `
        tests.test_vision_api `
        tests.test_search_controls_and_views `
        tests.test_structured_reader `
        tests.test_structured_reader_frontend `
        tests.test_structured_reader_web `
        tests.test_batch_directory_import `
        tests.test_calibration_library_ui `
        tests.test_directory_scan `
        tests.test_import_queue `
        tests.test_import_resume_mineru `
        tests.test_import_resume_queue `
        tests.test_import_resume_vision `
        tests.test_import_resume_web `
        tests.test_mineru_config `
        tests.test_portable_index_rebuild `
        tests.test_windows_desktop `
        tests.test_update_service `
        tests.test_windows_version_info `
        tests.test_windows_packaging `
        tests.test_platform_open `
        tests.test_theme_system `
        tests.test_desktop_portable
    if ($LASTEXITCODE -ne 0) { throw "Feature tests failed; installer was not built." }

    $nodeCommand = Get-Command node -ErrorAction SilentlyContinue
    if ($nodeCommand) {
        & $nodeCommand.Source --check "src\me_finder\static\app.js"
        if ($LASTEXITCODE -ne 0) { throw "app.js syntax check failed." }
        & $nodeCommand.Source --check "src\me_finder\static\reader.js"
        if ($LASTEXITCODE -ne 0) { throw "reader.js syntax check failed." }
    }

    & $pythonCommand @pythonLauncherArgs -m PyInstaller desktop.spec --clean --noconfirm
    if ($LASTEXITCODE -ne 0) { throw "PyInstaller build failed." }
    $appExecutables = @(Get-ChildItem -LiteralPath $DistPath -Filter "*.exe" -File)
    if ($appExecutables.Count -ne 1) {
        throw "PyInstaller output must contain exactly one application executable; found $($appExecutables.Count)."
    }

    New-Item -ItemType Directory -Force -Path (Join-Path $DistPath "data") | Out-Null
    New-Item -ItemType Directory -Force -Path (Join-Path $DistPath "config") | Out-Null
    Copy-Item -LiteralPath "config\pdf_imports.empty.json" -Destination (Join-Path $DistPath "config\pdf_imports.json") -Force
    Copy-Item -LiteralPath "config\mineru_api.local.example.json" -Destination (Join-Path $DistPath "config\mineru_api.local.example.json") -Force
    & $pythonCommand @pythonLauncherArgs -m tools.create_empty_index (Join-Path $DistPath "data\index.sqlite3")
    if ($LASTEXITCODE -ne 0) { throw "Blank index creation failed." }

    $forbiddenNames = @(
        "mineru_api.local.json",
        "vision_api.local.json",
        "preferences.json",
        "pdf_imports.json.pre-restore",
        "index.json",
        "desktop.log",
        "portable.flag"
    )
    $forbidden = Get-ChildItem -LiteralPath $DistPath -Recurse -Force | Where-Object {
        $forbiddenNames -contains $_.Name -or
        ($_.Name -like "*.local.json" -and $_.Name -notlike "*.local.example.json") -or
        $_.Name -eq "corpus" -or
        $_.FullName -like "*\corpus\*" -or
        (($_.Extension -eq ".sqlite3" -or $_.Extension -eq ".db") -and
            $_.FullName -ne (Join-Path $DistPath "data\index.sqlite3"))
    }
    if ($forbidden) {
        $paths = ($forbidden | ForEach-Object FullName) -join "`n"
        throw "Installer payload contains private or generated data:`n$paths"
    }

    $imports = Get-Content -LiteralPath (Join-Path $DistPath "config\pdf_imports.json") -Raw | ConvertFrom-Json
    if (@($imports.documents).Count -ne 0) {
        throw "Installer pdf_imports.json is not empty."
    }
    $blankIndex = Get-Item -LiteralPath (Join-Path $DistPath "data\index.sqlite3")
    if ($blankIndex.Length -gt 1MB) {
        throw "Blank index is unexpectedly large: $($blankIndex.Length) bytes"
    }

    New-Item -ItemType Directory -Force -Path $ReleaseRoot | Out-Null
    if (Test-Path -LiteralPath $InstallerPath) { Remove-Item -LiteralPath $InstallerPath -Force }
    if (Test-Path -LiteralPath $HashPath) { Remove-Item -LiteralPath $HashPath -Force }

    & $compiler "/DAppVersion=$Version" "installer\MEFinder.iss"
    if ($LASTEXITCODE -ne 0) { throw "Inno Setup compilation failed." }
    if (-not (Test-Path -LiteralPath $InstallerPath -PathType Leaf)) {
        throw "Inno Setup did not create the expected installer: $InstallerPath"
    }

    $hash = (Get-FileHash -Algorithm SHA256 -LiteralPath $InstallerPath).Hash.ToLowerInvariant()
    Set-Content -LiteralPath $HashPath -Encoding Ascii -Value "$hash  $PackageName.exe"

    $installer = Get-Item -LiteralPath $InstallerPath
    Write-Output "Windows installer: $($installer.FullName)"
    Write-Output "Size: $($installer.Length) bytes"
    Write-Output "SHA256: $hash"
    Write-Output "Checksum sidecar: $HashPath"
}
finally {
    Pop-Location
}

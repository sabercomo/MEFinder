param(
    [string]$Version = "",
    [string]$PythonExe = "",
    [string]$PackagerPythonExe = ""
)

$ErrorActionPreference = "Stop"
$ProjectRoot = Split-Path -Parent $MyInvocation.MyCommand.Path
$ReleaseRoot = Join-Path $ProjectRoot "release"
$DistPath = Join-Path $ProjectRoot "dist\MEFinder"
$LocalDataRoot = Join-Path $ProjectRoot "dist\MEFinderData"
$LocalDataMarker = Join-Path $DistPath "data_root.txt"
$McpDistPath = Join-Path $ProjectRoot "build\mcp-sidecar-dist"
$McpWorkPath = Join-Path $ProjectRoot "build\mcp-sidecar-work"
$McpSourcePath = Join-Path $McpDistPath "MEFinderMCP.exe"

function Restore-LocalDevelopmentDataMarker {
    if (-not (Test-Path -LiteralPath $DistPath -PathType Container)) { return }
    New-Item -ItemType Directory -Force -Path $LocalDataRoot | Out-Null
    Set-Content -LiteralPath $LocalDataMarker -Encoding UTF8 -NoNewline `
        -Value ([IO.Path]::GetFullPath($LocalDataRoot))
}

$releaseFull = [IO.Path]::GetFullPath($ReleaseRoot).TrimEnd('\')

Push-Location $ProjectRoot
try {
    $env:NO_PROXY = (@($env:NO_PROXY, "127.0.0.1", "localhost") -join ",").Trim(",")

    if ($PythonExe) {
        $pythonCommand = $PythonExe
        $pythonLauncherArgs = @()
    }
    else {
        $pythonCommand = "py"
        $pythonLauncherArgs = @("-3")
    }
    $packagerPythonCommand = if ($PackagerPythonExe) {
        $PackagerPythonExe
    } else {
        $pythonCommand
    }
    $packagerPythonArgs = if ($PackagerPythonExe) { @() } else { $pythonLauncherArgs }

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

    $PackageName = "MEFinder-v$Version-windows-portable"
    $StagePath = Join-Path $ReleaseRoot $PackageName
    $ZipPath = Join-Path $ReleaseRoot "$PackageName.zip"
    $HashPath = "$ZipPath.sha256.txt"

    $stageFull = [IO.Path]::GetFullPath($StagePath)
    if (-not $stageFull.StartsWith($releaseFull + '\', [StringComparison]::OrdinalIgnoreCase)) {
        throw "Unsafe release staging path: $stageFull"
    }

    & $pythonCommand @pythonLauncherArgs -m unittest tests.test_anchor_metadata tests.test_api_fallback_recovery tests.test_backup_service tests.test_backup_file_picker tests.test_batch_document_removal tests.test_calibration_library_ui tests.test_chunked_upload tests.test_citations tests.test_cnki_citation tests.test_journal_metadata_lookup tests.test_foreign_book_lookup tests.test_crossref_lookup tests.test_book_metadata_lookup tests.test_data_location tests.test_database_resilience tests.test_desktop_portable tests.test_desktop_shell_controller tests.test_fts_search_scalability tests.test_large_index_resilience tests.test_library_startup_performance tests.test_mineru_config tests.test_mineru_accounts tests.test_mineru_accounts_web tests.test_mineru_local_settings tests.test_mineru_local_provider tests.test_mineru_engine_import_bridge tests.test_parser_settings_controller tests.test_import_job_controller tests.test_import_parser_executor tests.test_import_orchestrator tests.test_pdf_import_config tests.test_import_config_concurrency tests.test_preferences_concurrency tests.test_long_filename_import tests.test_pdf_match_anchors tests.test_page_display tests.test_runtime_page_mapping tests.test_scan_directory_picker tests.test_search_match_spans tests.test_search_occurrence_identity tests.test_search_service tests.test_api_request_limits tests.test_source_streaming tests.test_app_context tests.test_database_page_anchors tests.test_index_publication_guard tests.test_normalization tests.test_vision_api tests.test_search_controls_and_views tests.test_structured_reader tests.test_structured_reader_frontend tests.test_structured_reader_web tests.test_batch_directory_import tests.test_document_package_import tests.test_directory_scan tests.test_import_queue tests.test_import_resume_mineru tests.test_import_resume_queue tests.test_import_resume_vision tests.test_import_resume_web tests.test_portable_index_rebuild tests.test_theme_system tests.test_frontend_assets tests.test_frontend_pure_logic tests.test_windows_packaging tests.test_runtime_location tests.test_literature_verification_service tests.test_mcp_v1_baseline tests.test_mcp_server tests.test_mcp_quality tests.test_mcp_documentation tests.test_mcp_packaging tests.test_mcp_concurrency
    if ($LASTEXITCODE -ne 0) { throw "Feature tests failed; release was not built." }

    $nodeCommand = Get-Command node -ErrorAction SilentlyContinue
    if ($nodeCommand) {
        # app.js 已按功能拆分到 static\js\，逐个检查以免新增文件漏检。
        $frontendScripts = @(Get-ChildItem -LiteralPath "src\me_finder\static\js" -Filter "*.js" -File | Sort-Object Name)
        if ($frontendScripts.Count -eq 0) { throw "static\js contains no JavaScript files." }
        foreach ($script in $frontendScripts) {
            & $nodeCommand.Source --check $script.FullName
            if ($LASTEXITCODE -ne 0) { throw "$($script.Name) syntax check failed." }
        }
        & $nodeCommand.Source --check "src\me_finder\static\reader.js"
        if ($LASTEXITCODE -ne 0) { throw "reader.js syntax check failed." }
    }

    & $packagerPythonCommand @packagerPythonArgs -m PyInstaller desktop.spec --clean --noconfirm
    if ($LASTEXITCODE -ne 0) { throw "PyInstaller build failed." }
    if (Test-Path -LiteralPath $McpDistPath) { Remove-Item -LiteralPath $McpDistPath -Recurse -Force }
    if (Test-Path -LiteralPath $McpWorkPath) { Remove-Item -LiteralPath $McpWorkPath -Recurse -Force }
    & $packagerPythonCommand @packagerPythonArgs -m PyInstaller mcp_sidecar.spec --clean --noconfirm --distpath $McpDistPath --workpath $McpWorkPath
    if ($LASTEXITCODE -ne 0) { throw "MCP sidecar PyInstaller build failed." }
    if (-not (Test-Path -LiteralPath $McpSourcePath -PathType Leaf)) {
        throw "PyInstaller did not create MEFinderMCP.exe."
    }
    Copy-Item -LiteralPath $McpSourcePath -Destination (Join-Path $DistPath "MEFinderMCP.exe") -Force

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
    Copy-Item -LiteralPath "LICENSE" -Destination (Join-Path $StagePath "LICENSE") -Force
    Copy-Item -LiteralPath "THIRD_PARTY_NOTICES.txt" -Destination (Join-Path $StagePath "THIRD_PARTY_NOTICES.txt") -Force
    Copy-Item -LiteralPath "THIRD_PARTY_LICENSES" -Destination (Join-Path $StagePath "THIRD_PARTY_LICENSES") -Recurse -Force
    $pythonLicenseOutput = & $pythonCommand @pythonLauncherArgs -c "from pathlib import Path; import sys; print(Path(sys.base_prefix) / 'LICENSE.txt')"
    if ($LASTEXITCODE -ne 0) { throw "Could not locate the selected Python runtime license." }
    $pythonLicensePath = ($pythonLicenseOutput | Out-String).Trim()
    if (-not (Test-Path -LiteralPath $pythonLicensePath -PathType Leaf)) {
        throw "Selected Python runtime license was not found: $pythonLicensePath"
    }
    Copy-Item -LiteralPath $pythonLicensePath -Destination (Join-Path $StagePath "THIRD_PARTY_LICENSES\Python-runtime-LICENSE.txt") -Force
    foreach ($licensePath in @("LICENSE", "THIRD_PARTY_NOTICES.txt", "THIRD_PARTY_LICENSES", "THIRD_PARTY_LICENSES\Python-runtime-LICENSE.txt")) {
        if (-not (Test-Path -LiteralPath (Join-Path $StagePath $licensePath))) {
            throw "Required license material is missing from the portable payload: $licensePath"
        }
    }
    Copy-Item -LiteralPath "installer\PORTABLE_README.md" -Destination (Join-Path $StagePath "README.md")
    $blankIndexPath = Join-Path $StagePath "data\index.sqlite3"
    & $pythonCommand @pythonLauncherArgs -m tools.create_empty_index $blankIndexPath
    if ($LASTEXITCODE -ne 0) { throw "Blank index creation failed." }
    & $pythonCommand @pythonLauncherArgs -c "import sqlite3, sys; connection = sqlite3.connect(sys.argv[1]); table = connection.execute('SELECT 1 FROM sqlite_master WHERE name = ?', ('paragraphs_fts',)).fetchone(); connection.close(); raise SystemExit(0 if table is not None else 1)" $blankIndexPath
    if ($LASTEXITCODE -ne 0) {
        throw "Build failed: this Python/SQLite runtime has no FTS5 trigram support."
    }
    & $packagerPythonCommand @packagerPythonArgs -m tools.smoke_mcp_sidecar (Join-Path $StagePath "MEFinderMCP.exe") $StagePath
    if ($LASTEXITCODE -ne 0) { throw "Packaged MCP sidecar smoke test failed." }

    $forbiddenNames = @(
        "mineru_api.local.json",
        "vision_api.local.json",
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

    $blankIndex = Get-Item -LiteralPath $blankIndexPath
    if ($blankIndex.Length -gt 1MB) {
        throw "Blank index is unexpectedly large: $($blankIndex.Length) bytes"
    }

    & $pythonCommand @pythonLauncherArgs -m tools.create_portable_zip $StagePath $ZipPath
    if ($LASTEXITCODE -ne 0) { throw "Portable ZIP creation failed." }
    & $pythonCommand @pythonLauncherArgs -c "import sys, zipfile; names=set(zipfile.ZipFile(sys.argv[1]).namelist()); root=sys.argv[2] + '/'; required={root + 'LICENSE', root + 'THIRD_PARTY_NOTICES.txt'}; raise SystemExit(0 if required <= names and any(name.startswith(root + 'THIRD_PARTY_LICENSES/') for name in names) else 1)" $ZipPath $PackageName
    if ($LASTEXITCODE -ne 0) { throw "Portable ZIP does not contain the required license materials." }
    $hash = (Get-FileHash -Algorithm SHA256 -LiteralPath $ZipPath).Hash.ToLowerInvariant()
    Set-Content -LiteralPath $HashPath -Encoding Ascii -Value "$hash  $PackageName.zip"

    $zip = Get-Item -LiteralPath $ZipPath
    Write-Output "Portable release: $($zip.FullName)"
    Write-Output "Size: $($zip.Length) bytes"
    Write-Output "SHA256: $hash"
}
finally {
    try {
        # PyInstaller recreates dist\MEFinder. Restore the local test-build
        # pointer only after the public ZIP has already been staged and sealed.
        Restore-LocalDevelopmentDataMarker
    }
    catch {
        Write-Warning "Could not restore the local development data marker: $_"
    }
    finally {
        Pop-Location
    }
}

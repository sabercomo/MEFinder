@echo off
setlocal

cd /d "%~dp0"
set "PYTHON=%CD%\.venv-windows\Scripts\python.exe"
set "PYTHONPATH=%CD%\src;%PYTHONPATH%"
set "DIST=%CD%\dist\MEFinder"
set "MCP_DIST=%CD%\build\mcp-sidecar-dist"
set "MCP_WORK=%CD%\build\mcp-sidecar-work"
set "MCP_SOURCE=%MCP_DIST%\MEFinderMCP.exe"
set "LOCAL_DATA=%CD%\dist\MEFinderData"
set "DATA_ROOT_MARKER=%DIST%\data_root.txt"
if defined NO_PROXY (
  set "NO_PROXY=%NO_PROXY%,127.0.0.1,localhost"
) else (
  set "NO_PROXY=127.0.0.1,localhost"
)

if not exist "%PYTHON%" (
  echo Windows build environment is missing: %PYTHON%
  exit /b 1
)

"%PYTHON%" -c "import PyInstaller, mcp, webview; from src.me_finder import __version__; print('Building MEFinder v' + __version__)"
if errorlevel 1 exit /b 1

"%PYTHON%" -m unittest ^
  tests.test_anchor_metadata ^
  tests.test_api_fallback_recovery ^
  tests.test_mineru_config ^
  tests.test_mineru_local_settings ^
  tests.test_mineru_local_provider ^
  tests.test_local_ocr_settings ^
  tests.test_local_ocr_installer ^
  tests.test_local_ocr_provider ^
  tests.test_mineru_engine_import_bridge ^
  tests.test_parser_settings_controller ^
  tests.test_import_job_controller ^
  tests.test_import_parser_executor ^
  tests.test_import_orchestrator ^
  tests.test_vision_api ^
  tests.test_citations ^
  tests.test_cnki_citation ^
  tests.test_journal_metadata_lookup ^
  tests.test_foreign_book_lookup ^
  tests.test_crossref_lookup ^
  tests.test_book_metadata_lookup ^
  tests.test_chunked_upload ^
  tests.test_database_resilience ^
  tests.test_fts_search_scalability ^
  tests.test_large_index_resilience ^
  tests.test_pdf_import_config ^
  tests.test_import_config_concurrency ^
  tests.test_preferences_concurrency ^
  tests.test_long_filename_import ^
  tests.test_pdf_match_anchors ^
  tests.test_page_display ^
  tests.test_runtime_page_mapping ^
  tests.test_search_match_spans ^
  tests.test_search_occurrence_identity ^
  tests.test_search_service ^
  tests.test_api_request_limits ^
  tests.test_source_streaming ^
  tests.test_app_context ^
  tests.test_database_page_anchors ^
  tests.test_index_publication_guard ^
  tests.test_normalization ^
  tests.test_search_controls_and_views ^
  tests.test_structured_reader ^
  tests.test_structured_reader_frontend ^
  tests.test_structured_reader_web ^
  tests.test_batch_directory_import ^
  tests.test_calibration_library_ui ^
  tests.test_library_startup_performance ^
  tests.test_batch_document_removal ^
  tests.test_toast_presentation ^
  tests.test_directory_scan ^
  tests.test_import_queue ^
  tests.test_import_resume_mineru ^
  tests.test_import_resume_queue ^
  tests.test_import_resume_vision ^
  tests.test_import_resume_web ^
  tests.test_backup_service ^
  tests.test_backup_coordinator ^
  tests.test_backup_file_picker ^
  tests.test_document_groups ^
  tests.test_search_group_scope ^
  tests.test_data_location ^
  tests.test_desktop_shell_controller ^
  tests.test_scan_directory_picker ^
  tests.test_portable_index_rebuild ^
  tests.test_windows_desktop ^
  tests.test_update_service ^
  tests.test_windows_version_info ^
  tests.test_windows_packaging ^
  tests.test_runtime_location ^
  tests.test_literature_verification_service ^
  tests.test_mcp_v1_baseline ^
  tests.test_mcp_server ^
  tests.test_mcp_quality ^
  tests.test_mcp_documentation ^
  tests.test_mcp_packaging ^
  tests.test_mcp_concurrency ^
  tests.test_platform_open ^
  tests.test_theme_system ^
  tests.test_frontend_assets ^
  tests.test_frontend_pure_logic ^
  tests.test_desktop_portable
if errorlevel 1 exit /b 1

rem app.js 已按功能拆分到 static\js\，逐个语法检查，避免新增文件漏检。
for %%F in (src\me_finder\static\js\*.js) do (
  node --check "%%F"
  if errorlevel 1 exit /b 1
)
node --check src\me_finder\static\reader.js
if errorlevel 1 exit /b 1

"%PYTHON%" -m PyInstaller "packaging\desktop.spec" --clean --noconfirm
if errorlevel 1 exit /b 1

if exist "%MCP_DIST%" rmdir /s /q "%MCP_DIST%"
if exist "%MCP_WORK%" rmdir /s /q "%MCP_WORK%"
"%PYTHON%" -m PyInstaller "packaging\mcp_sidecar.spec" --clean --noconfirm --distpath "%MCP_DIST%" --workpath "%MCP_WORK%"
if errorlevel 1 exit /b 1
if not exist "%MCP_SOURCE%" (
  echo PyInstaller did not create MEFinderMCP.exe.
  exit /b 1
)
copy /y "%MCP_SOURCE%" "%DIST%\MEFinderMCP.exe" >nul

"%PYTHON%" -c "import os, sys; from pathlib import Path; names = {p.name for p in Path(os.environ['DIST']).glob('*.exe')}; sys.exit(0 if len(names) == 2 and 'MEFinderMCP.exe' in names else 1)"
if errorlevel 1 (
  echo Build output must contain the desktop executable and MEFinderMCP.exe.
  exit /b 1
)

if not exist "%DIST%\data" mkdir "%DIST%\data"
if not exist "%DIST%\config" mkdir "%DIST%\config"
copy /y "config\pdf_imports.empty.json" "%DIST%\config\pdf_imports.json" >nul
copy /y "config\mineru_api.local.example.json" "%DIST%\config\mineru_api.local.example.json" >nul
"%PYTHON%" -m tools.create_empty_index "%DIST%\data\index.sqlite3"
if errorlevel 1 exit /b 1
"%PYTHON%" -c "import sqlite3, sys; connection = sqlite3.connect(sys.argv[1]); table = connection.execute(\"SELECT 1 FROM sqlite_master WHERE name = 'paragraphs_fts'\").fetchone(); connection.close(); raise SystemExit(0 if table is not None else 1)" "%DIST%\data\index.sqlite3"
if errorlevel 1 (
  echo Build failed: this Python/SQLite runtime has no FTS5 trigram support.
  exit /b 1
)

if not exist "%LOCAL_DATA%" mkdir "%LOCAL_DATA%"
"%PYTHON%" -c "import os; from pathlib import Path; Path(os.environ['DATA_ROOT_MARKER']).write_text(os.environ['LOCAL_DATA'], encoding='utf-8')"
if errorlevel 1 exit /b 1
"%PYTHON%" -m tools.smoke_mcp_sidecar "%DIST%\MEFinderMCP.exe" "%DIST%"
if errorlevel 1 exit /b 1

echo Windows test build: %DIST%
echo Local data: %LOCAL_DATA%
exit /b 0

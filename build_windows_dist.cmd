@echo off
setlocal

cd /d "%~dp0"
set "PYTHON=%CD%\.venv-windows\Scripts\python.exe"
set "DIST=%CD%\dist\MEFinder"
set "LOCAL_DATA=%CD%\dist\MEFinderData"
set "DATA_ROOT_MARKER=%DIST%\data_root.txt"

if not exist "%PYTHON%" (
  echo Windows build environment is missing: %PYTHON%
  exit /b 1
)

"%PYTHON%" -c "import PyInstaller, webview; from src.me_finder import __version__; print('Building MEFinder v' + __version__)"
if errorlevel 1 exit /b 1

"%PYTHON%" -m unittest ^
  tests.test_vision_api ^
  tests.test_citations ^
  tests.test_page_display ^
  tests.test_runtime_page_mapping ^
  tests.test_search_match_spans ^
  tests.test_search_controls_and_views ^
  tests.test_structured_reader ^
  tests.test_structured_reader_frontend ^
  tests.test_structured_reader_web ^
  tests.test_batch_directory_import ^
  tests.test_calibration_library_ui ^
  tests.test_directory_scan ^
  tests.test_import_queue ^
  tests.test_import_resume_mineru ^
  tests.test_import_resume_queue ^
  tests.test_import_resume_vision ^
  tests.test_import_resume_web ^
  tests.test_backup_service ^
  tests.test_portable_index_rebuild ^
  tests.test_windows_desktop ^
  tests.test_update_service ^
  tests.test_windows_version_info ^
  tests.test_windows_packaging ^
  tests.test_platform_open ^
  tests.test_theme_system ^
  tests.test_desktop_portable
if errorlevel 1 exit /b 1

node --check src\me_finder\static\app.js
if errorlevel 1 exit /b 1
node --check src\me_finder\static\reader.js
if errorlevel 1 exit /b 1

"%PYTHON%" -m PyInstaller desktop.spec --clean --noconfirm
if errorlevel 1 exit /b 1

"%PYTHON%" -c "import os, sys; from pathlib import Path; matches = list(Path(os.environ['DIST']).glob('*.exe')); sys.exit(0 if len(matches) == 1 else 1)"
if errorlevel 1 (
  echo PyInstaller did not create the expected executable.
  exit /b 1
)

if not exist "%DIST%\data" mkdir "%DIST%\data"
if not exist "%DIST%\config" mkdir "%DIST%\config"
copy /y "config\pdf_imports.empty.json" "%DIST%\config\pdf_imports.json" >nul
copy /y "config\mineru_api.local.example.json" "%DIST%\config\mineru_api.local.example.json" >nul
"%PYTHON%" -m tools.create_empty_index "%DIST%\data\index.sqlite3"
if errorlevel 1 exit /b 1

if not exist "%LOCAL_DATA%" mkdir "%LOCAL_DATA%"
"%PYTHON%" -c "import os; from pathlib import Path; Path(os.environ['DATA_ROOT_MARKER']).write_text(os.environ['LOCAL_DATA'], encoding='utf-8')"
if errorlevel 1 exit /b 1

echo Windows test build: %DIST%
echo Local data: %LOCAL_DATA%
exit /b 0

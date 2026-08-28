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

rem 发布门禁运行整套 tests\（discover），不再手工维护模块名单。缺私有语料或
rem 可选开发依赖的用例会 skipUnless 自跳过。
"%PYTHON%" -m unittest discover -t . -s tests
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

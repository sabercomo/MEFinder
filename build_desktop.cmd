@echo off
setlocal
cd /d "%~dp0"

rem Desktop packaging script (see DESKTOP_PACKAGING_PLAN.md).
rem   build_desktop.cmd        build exe + copy index + page config
rem   build_desktop.cmd full   also copy corpus files (~400MB) for
rem                            the "open original file" button
rem NEVER copy config\mineru_api.local.json into dist: desktop credentials
rem live under %%LOCALAPPDATA%%\MEFinder and survive application upgrades.

py -3 -m PyInstaller desktop.spec --clean --noconfirm
if errorlevel 1 (
  echo Build failed.
  exit /b 1
)

set "DIST=dist\MEFinder"

if not exist "%DIST%\data" mkdir "%DIST%\data"
copy /Y "data\index.sqlite3" "%DIST%\data\index.sqlite3" >nul
if errorlevel 1 (
  echo Missing data\index.sqlite3. Run: py -3 -m src.me_finder build-index
  exit /b 1
)

if not exist "%DIST%\config" mkdir "%DIST%\config"
copy /Y "config\pdf_imports.json" "%DIST%\config\pdf_imports.json" >nul
copy /Y "config\mineru_api.local.example.json" "%DIST%\config\mineru_api.local.example.json" >nul

if /I "%~1"=="full" (
  echo Copying corpus...
  xcopy /E /I /Y /Q "corpus\raw_docx" "%DIST%\corpus\raw_docx\" >nul
  xcopy /E /I /Y /Q "corpus\raw_pdf" "%DIST%\corpus\raw_pdf\" >nul
  if exist "corpus\processed\mineru\manifests" xcopy /E /I /Y /Q "corpus\processed\mineru\manifests" "%DIST%\corpus\processed\mineru\manifests\" >nul
  if exist "corpus\processed\mineru\results" xcopy /E /I /Y /Q "corpus\processed\mineru\results" "%DIST%\corpus\processed\mineru\results\" >nul
)

echo.
echo Done. Distributable folder: %DIST%
if /I not "%~1"=="full" echo Tip: run "build_desktop.cmd full" to include corpus files.
endlocal

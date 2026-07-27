@echo off
setlocal
cd /d "%~dp0"

echo Running feature tests and rebuilding the Windows portable release...
echo.
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0build_portable_release.ps1"
if errorlevel 1 (
  echo.
  echo Build failed. Please keep this window open and share the error message.
  pause
  exit /b 1
)

echo.
echo Build completed. The ZIP and SHA256 file are in:
echo %~dp0release
pause
endlocal

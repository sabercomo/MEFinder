@echo off
setlocal
cd /d "%~dp0"

powershell.exe -NoProfile -ExecutionPolicy Bypass -Command "$root=(Resolve-Path -LiteralPath '.').Path; Get-ChildItem -LiteralPath $root -File | Unblock-File -ErrorAction Stop; $internal=Join-Path $root '_internal'; if(Test-Path -LiteralPath $internal){Get-ChildItem -LiteralPath $internal -Recurse -File | Unblock-File -ErrorAction Stop}; $exe=Get-ChildItem -LiteralPath $root -Filter '*.exe' -File | Select-Object -First 1; if(-not $exe){throw 'Application executable not found.'}; Start-Process -FilePath $exe.FullName"
if errorlevel 1 (
  echo.
  echo Failed to unblock or start MEFinder. Please re-extract the complete ZIP and try again.
  pause
  exit /b 1
)

endlocal

@echo off
chcp 65001 >nul
setlocal
cd /d "%~dp0"
rem 某些受控环境下 PSModulePath 不完整，会导致 Cert: 驱动器加载失败
set "PSModulePath=%SystemRoot%\System32\WindowsPowerShell\v1.0\Modules"
if "%~1"=="" (
  powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0sign.ps1"
) else (
  powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0sign.ps1" "%~1"
)
exit /b %errorlevel%

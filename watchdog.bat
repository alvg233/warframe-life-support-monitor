@echo off
chcp 65001 >nul
setlocal
cd /d "%~dp0"
set "PYTHONPATH=%~dp0.vendor;%PYTHONPATH%"

set "PY=%~dp0.venv\Scripts\python.exe"
if not exist "%PY%" set "PY=python.exe"

:loop
start /wait "" "%PY%" -m screen_ocr_alert
if errorlevel 1 goto restart
echo 正常退出，看门狗结束。
goto end

:restart
echo 程序异常退出，5 秒后重启...
timeout /t 5 /nobreak >nul
goto loop

:end

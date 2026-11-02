@echo off
chcp 65001 >nul
setlocal
cd /d "%~dp0"

set "ISCC=%LOCALAPPDATA%\Programs\Inno Setup 6\ISCC.exe"
if not exist "%ISCC%" set "ISCC=%ProgramFiles%\Inno Setup 6\ISCC.exe"
if not exist "%ISCC%" set "ISCC=%ProgramFiles(x86)%\Inno Setup 6\ISCC.exe"
if not exist "%ISCC%" (
  echo 找不到 Inno Setup 的 ISCC.exe。
  echo 先安装：winget install --id JRSoftware.InnoSetup
  exit /b 1
)

if not exist "dist\ScreenOcrAlert\ScreenOcrAlert.exe" (
  echo 先跑 build_exe.bat 生成程序目录。
  exit /b 1
)

"%ISCC%" "installer.iss"
if errorlevel 1 (
  echo 安装包编译失败。
  exit /b 1
)

for /f "usebackq delims=" %%f in (`powershell -NoProfile -Command "(Get-ChildItem 'dist\installer\ScreenOcrAlert-Setup-*.exe' | Sort-Object LastWriteTime -Descending | Select-Object -First 1).FullName"`) do set "SETUP=%%f"

rem 本机是在受限环境里编译的，产物会带 Low 完整性标签，双击会被系统静默拦掉
icacls "%SETUP%" /setintegritylevel M >nul 2>&1

rem 自用签名（有证书就签，没有就跳过）
call "%~dp0sign.bat" "%SETUP%"

echo.
echo 完成：%SETUP%

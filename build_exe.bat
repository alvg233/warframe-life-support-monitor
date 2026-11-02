@echo off
chcp 65001 >nul
setlocal
cd /d "%~dp0"
set "PYTHONPATH=%~dp0.vendor;%PYTHONPATH%"

echo [1/2] 安装 PyInstaller 到 .vendor ...
python -m pip install --no-input --disable-pip-version-check --target .vendor -i https://pypi.tuna.tsinghua.edu.cn/simple pyinstaller
if errorlevel 1 goto fail

echo [2/3] 打包中（目录版 onedir：不需要解压到临时目录，启动更快更稳）...
python -m PyInstaller --noconfirm --clean --windowed --name ScreenOcrAlert --icon "%~dp0dist\app.ico" --version-file "%~dp0version_info.txt" --paths .vendor --collect-all rapidocr --collect-all onnxruntime --distpath dist main.py
if errorlevel 1 goto fail

echo [3/4] 修正文件完整性级别（沙箱里编译出来是 Low，会被 SmartScreen 当可疑程序）...
icacls "%~dp0dist\ScreenOcrAlert" /setintegritylevel "(OI)(CI)M" /T /C >nul
if errorlevel 1 echo 警告：完整性级别没改成功，双击可能仍被拦。

echo [4/4] 给 exe 做本地签名...
if exist "%~dp0sign.bat" (
  call "%~dp0sign.bat"
) else (
  echo 没有 sign.bat，跳过签名。
)

echo.
echo 完成：dist\ScreenOcrAlert\ScreenOcrAlert.exe
echo 分发时整个 dist\ScreenOcrAlert 文件夹一起拷走；配置和日志就在这个文件夹里。
goto end

:fail
echo.
echo 打包失败，看上面的错误信息。

:end

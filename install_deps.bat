@echo off
chcp 65001 >nul
setlocal
cd /d "%~dp0"

echo 正在把依赖装到 .vendor（约 200MB，国内走清华镜像）...
python -m pip install --no-input --disable-pip-version-check --target .vendor -i https://pypi.tuna.tsinghua.edu.cn/simple -r requirements.txt
if errorlevel 1 goto fail

echo.
echo 完成。双击 run.bat 即可启动。
goto end

:fail
echo.
echo 安装失败：请确认已装 Python 3.10+ 且 python --version 能跑。

:end

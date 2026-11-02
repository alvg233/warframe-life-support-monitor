@echo off
chcp 65001 >nul
setlocal
cd /d "%~dp0"

echo === 推送到 GitHub ===
git remote remove github 2>nul
git remote add github git@github.com:alvg233/warframe-life-support-monitor.git
git push -u github main
if errorlevel 1 echo GitHub 推送失败：确认仓库已创建、SSH 公钥已添加。

echo.
echo === 推送到 Gitee ===
git remote remove gitee 2>nul
git remote add gitee git@gitee.com:alvg233/warframe-life-support-monitor.git
git push -u gitee main
git push gitee main:master
if errorlevel 1 (
  echo Gitee 推送失败：确认仓库已创建、SSH 公钥已添加到 Gitee。
) else (
  echo Gitee 推送完成（同时推送 main 与 master 分支）。
)

echo.
echo 完成。按任意键退出。
pause >nul

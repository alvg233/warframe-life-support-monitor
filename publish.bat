@echo off
chcp 65001 >nul
setlocal
cd /d "%~dp0"

echo === 推送到 GitHub（需已在账号 alvg233 下创建空仓库 warframe-life-support-monitor） ===
git remote remove github 2>nul
git remote add github git@github.com:alvg233/warframe-life-support-monitor.git
git push -u github main
if errorlevel 1 (
  echo.
  echo GitHub 推送失败：请确认仓库已创建，且本机 SSH 公钥已添加到 GitHub。
  goto gitee
)
echo GitHub 推送完成。

:gitee
echo.
echo === 推送到 Gitee（需先在 Gitee 创建空仓库，并添加本机 SSH 公钥） ===
git remote remove gitee 2>nul
git remote add gitee git@gitee.com:alvg233/warframe-life-support-monitor.git
git push -u gitee main
if errorlevel 1 (
  echo.
  echo Gitee 推送失败：请在 Gitee「设置 → SSH 公钥」中添加本机公钥后重试。
) else (
  echo Gitee 推送完成。
)

echo.
echo 完成。按任意键退出。
pause >nul

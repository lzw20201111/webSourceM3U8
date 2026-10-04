@echo off
rem ============================================================
rem  刷新静态播放源（必须在国内网络环境运行！）
rem
rem  原理：先校验现有 330 条地址，只重抓失效的（通常几分钟），
rem        抓完自动推送到 GitHub，中转服务随即生效。
rem
rem  ⚠️ 绝不要在 GitHub Actions 上跑抓取：
rem     海外 IP 会拿到央视等源站的【海外 CDN 线路】，国内播不了。
rem
rem  Token 放同目录的 gh_token.txt（一行，只有 PAT 本身），
rem  或用环境变量 GITHUB_TOKEN。切勿把 gh_token.txt 提交到仓库
rem  （GitHub 的 secret scanning 也会直接拒绝推送）。
rem ============================================================
chcp 65001 >nul
setlocal
cd /d "%~dp0"

set "PY=C:\Users\Administrator\.workbuddy\binaries\python\envs\default\Scripts\python.exe"
set "PW_CHANNEL=chrome"

if not defined GITHUB_TOKEN (
  if exist "%~dp0gh_token.txt" set /p GITHUB_TOKEN=<"%~dp0gh_token.txt"
)
if not defined GITHUB_TOKEN (
  echo [错误] 找不到 GitHub Token。
  echo         请把 PAT 写入 %~dp0gh_token.txt ，或先设置环境变量 GITHUB_TOKEN。
  pause
  exit /b 1
)

echo [1/2] 校验并重抓失效频道…
"%PY%" scripts\refresh_webview.py
if errorlevel 1 (
  echo 抓取失败，中止推送。
  pause
  exit /b 1
)

echo.
echo [2/2] 推送到 GitHub…
"%PY%" scripts\publish_github.py channels.json harvest_result.json webview_static.m3u
if errorlevel 1 (
  echo 推送失败，请检查 Token 权限（需要 Contents: write）。
  pause
  exit /b 1
)

echo.
echo 完成。中转地址（永久不变）：
echo   https://xinyi-relay.pages.dev/webview.m3u
pause

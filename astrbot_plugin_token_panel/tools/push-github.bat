@echo off
chcp 936 >nul
setlocal enabledelayedexpansion
set "HERE=%~dp0"
pushd "%HERE%.."

echo.
echo ==========================================================
echo   PLUTO panel - 推送到 GitHub（命令模板，不含任何账号信息）
echo ==========================================================
echo.
echo   说明：本脚本只执行 git 命令，不会登录、不会保存 token。
echo         认证走你本机已有的 git 凭证（PAT / SSH / 凭据管理器）。
echo.

where git >nul 2>nul
if errorlevel 1 (
  echo   [错误] 未安装 git。请先安装：https://git-scm.com/download/win
  popd & pause & exit /b 1
)

for /f "delims=" %%u in ('git config user.name') do set "GITNAME=%%u"
for /f "delims=" %%e in ('git config user.email') do set "GITMAIL=%%e"

echo   当前 git 身份： %GITNAME% ^< %GITMAIL% ^>
if "%GITNAME%"=="" (
  echo.
  echo   [提醒] 还没配置 git 用户名，推送前请先执行：
  echo          git config --global user.name "你的名字"
  echo          git config --global user.email "you@example.com"
  echo.
)
echo.

set "REPO="
set /p "REPO=  仓库地址 [回车默认 https://github.com/Charon/astrbot_plugin_token_panel.git] : "
if "!REPO!"=="" set "REPO=https://github.com/Charon/astrbot_plugin_token_panel.git"

set "MSG="
set /p "MSG=  提交信息 [回车默认 chore: 发布 v1.3.1] : "
if "!MSG!"=="" set "MSG=chore: 发布 v1.3.1"

echo.
echo   ----------------------------------------------
if not exist ".git" (
  echo   [1/6] git init
  git init || goto :fail
) else (
  echo   [1/6] 已存在 .git，跳过 init
)

echo   [2/6] git add .
git add . || goto :fail

echo   [3/6] git commit
git diff --cached --quiet
if errorlevel 1 (
  git commit -m "!MSG!" || goto :fail
) else (
  echo         没有新改动，跳过 commit
)

echo   [4/6] 分支切到 main
git branch -M main || goto :fail

echo   [5/6] 设置远程地址
git remote get-url origin >nul 2>nul
if errorlevel 1 (
  git remote add origin "!REPO!" || goto :fail
) else (
  git remote set-url origin "!REPO!" || goto :fail
)

echo   [6/6] git push
echo.
git push -u origin main || goto :fail

echo.
set "TAG="
set /p "TAG=  是否同时打标签 v1.3.1 并推送？(Y/N) [回车 Y] : "
if /i "!TAG!"=="" set "TAG=Y"
if /i "!TAG!"=="Y" (
  git tag v1.3.1
  git push origin v1.3.1
)

echo.
echo   [完成] 已推送到 !REPO!
echo          记得去仓库 Settings 里补 About 描述、Topics 与 Social preview。
echo.
popd
pause
exit /b 0

:fail
echo.
echo   [失败] 上面那条 git 命令出错了。
echo          常见原因：未认证 / token 无 Contents 写权限 / 仓库不存在 / 网络不通。
echo.
popd
pause
exit /b 1
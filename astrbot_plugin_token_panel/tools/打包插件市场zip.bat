@echo off
chcp 936 >nul
setlocal
set "HERE=%~dp0"
echo.
echo ==========================================================
echo   PLUTO panel - AstrBot 插件市场 zip 打包
echo ==========================================================
echo.

where powershell >nul 2>nul
if errorlevel 1 (
  echo   [错误] 找不到 powershell.exe，请在 Windows 10/11 上运行。
  pause
  exit /b 1
)

powershell -NoProfile -ExecutionPolicy Bypass -File "%HERE%pack.ps1"
if errorlevel 1 (
  echo.
  echo   [失败] 打包未通过，请把上面的报错发给作者。
  pause
  exit /b 1
)

echo   提示：zip 根目录必须是 astrbot_plugin_token_panel，可直接解压确认。
echo.
pause
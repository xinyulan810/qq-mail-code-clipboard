@echo off
setlocal
cd /d "%~dp0"
title QQ 邮箱验证码工具

rem 优先使用打包好的单文件版本
if exist "dist\QQ验证码工具.exe" (
    start "" "dist\QQ验证码工具.exe"
    exit
)

rem 开发模式：直接用 Python 运行
where python >nul 2>nul
if errorlevel 1 (
    echo [错误] 未找到 python，请先安装 Python 3.9+（推荐 3.14），或使用打包好的 exe。
    pause
    exit /b 1
)

start "" pythonw "%~dp0app.py"
exit

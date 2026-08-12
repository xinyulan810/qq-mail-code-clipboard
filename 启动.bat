@echo off
setlocal
cd /d "%~dp0"
title QQ 邮箱验证码监听工具

rem 检查 Python 是否可用
where python >nul 2>nul
if errorlevel 1 (
    echo [错误] 未找到 python，请先安装 Python 3.9+（推荐 3.14），安装时勾选 "Add python.exe to PATH"。
    pause
    exit /b 1
)

rem 检查配置文件
if not exist "config.json" (
    echo [错误] 缺少 config.json。
    echo 请先执行：copy config.example.json config.json，然后填入邮箱和 16 位授权码。
    pause
    exit /b 1
)

echo 正在启动工具……
echo 本窗口即将自动关闭，请留意右下角托盘图标和状态窗口。
where pythonw >nul 2>nul
if errorlevel 1 (
    start "" /min python "%~dp0gui.py"
) else (
    start "" pythonw "%~dp0gui.py"
)
ping -n 2 127.0.0.1 >nul
exit

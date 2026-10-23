@echo off
chcp 65001 >nul
cd /d %~dp0

if not exist ".env" (
    echo.
    echo   [首次运行] 正在创建 .env 配置文件...
    echo.
    copy /y ".env.example" ".env" >nul
    echo   请用记事本打开 .env，填入 DEEPSEEK_API_KEY，保存后重新运行本脚本。
    echo.
    pause
    exit /b 1
)

rem 依次探测可用的 Python 命令，用 if exist 判断，避免 & 链式调用导致窗口立即退出
set "PYCMD="
where python >nul 2>nul && set "PYCMD=python"
if not defined PYCMD where python3 >nul 2>nul && set "PYCMD=python3"
if not defined PYCMD where py >nul 2>nul && set "PYCMD=py"

if not defined PYCMD (
    echo.
    echo   [错误] 未找到 Python。请先安装 Python 3.8 或更高版本：
    echo   https://www.python.org/downloads/
    echo   安装时务必勾选 "Add Python to PATH"。
    echo.
    pause
    exit /b 1
)

echo   使用 Python: %PYCMD%
echo.
%PYCMD% run.py

rem Python 退出后停住窗口，便于查看报错信息
echo.
echo   [服务已停止] 按任意键关闭窗口
pause >nul

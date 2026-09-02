@echo off
chcp 65001 >nul
REM ========================================
REM LMArena Bridge 后端服务器 (改进版)
REM 关闭窗口时会自动终止Python进程
REM ========================================

echo ========================================
echo 启动 LMArena Bridge 后端服务器
echo ========================================
echo.

REM 设置窗口标题，方便识别
title LMArena Bridge Server

REM 读取端口号（与 点击启动.CMD 同口径，缺配置时回退 5102）
for /f "delims=" %%i in ('python -c "import re;c=open('config.jsonc',encoding='utf-8').read();m=re.search(r'\"server_port\"\s*:\s*(\d+)',c);print(m.group(1) if m else 5102)" 2^>nul') do set PORT=%%i
if "%PORT%"=="" set PORT=5102
title LMArena Bridge Server - Port %PORT%

REM 检查端口是否已被占用
echo 检查端口 %PORT% 是否可用...
netstat -ano | findstr :%PORT% | findstr LISTENING >nul
if not errorlevel 1 (
    echo.
    echo 警告: 端口 %PORT% 已被占用！
    echo.
    echo 可能的原因：
    echo   1. 服务器已经在运行
    echo   2. 上次启动的进程未正常关闭
    echo.
    echo 解决方法：
    echo   1. 运行 kill_server.cmd 终止旧进程
    echo   2. 或使用任务管理器手动终止
    echo.
    pause
    exit /b 1
)

echo 端口可用
echo.

REM 启动Python服务器
echo 正在启动服务器...
echo.
echo 提示：
echo   - 按 Ctrl+C 可以优雅地停止服务器
echo   - 直接关闭窗口也会终止进程
echo.
echo ========================================
echo.

REM 使用 start /wait 确保进程附加到当前窗口
REM 这样关闭窗口时进程也会被终止
python api_server_new.py

REM 如果Python退出，显示提示
echo.
echo ========================================
echo 服务器已停止
echo ========================================
pause

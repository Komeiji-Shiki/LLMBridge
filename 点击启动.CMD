@echo off
chcp 65001 >nul
echo ========================================
echo LMArena Bridge 启动器 v2.0
echo ========================================
echo.

:: 读取端口号
for /f "delims=" %%i in ('python -c "import re;c=open('config.jsonc',encoding='utf-8').read();m=re.search(r'\"server_port\"\s*:\s*(\d+)',c);print(m.group(1) if m else 5102)" 2^>nul') do set PORT=%%i
if "%PORT%"=="" set PORT=5102

echo [1/3] 清理残留进程...
echo.

echo 正在查找占用端口 %PORT% 的进程...
for /f "tokens=5" %%a in ('netstat -ano ^| findstr :%PORT% ^| findstr LISTENING') do (
    echo 发现进程 PID: %%a
    echo 正在终止进程...
    taskkill /F /PID %%a
    if errorlevel 1 (
        echo 终止失败，可能需要管理员权限
    ) else (
        echo 进程已终止
    )
)


:: 检查并安装依赖
echo [2/3] 检查依赖...
echo.

if exist requirements.txt (
    python -c "import fastapi, aiohttp, uvicorn" >nul 2>&1
    if errorlevel 1 (
        echo 正在安装依赖...
        pip install -r requirements.txt -q --disable-pip-version-check
        echo 依赖安装完成
    ) else (
        echo 核心依赖已安装
    )
) else (
    echo requirements.txt 不存在，跳过
)

echo.

:: 启动服务
echo [3/3] 启动服务...
echo.
echo ========================================
echo 端口号: %PORT%
echo 管理面板: http://127.0.0.1:%PORT%/admin
echo API地址: http://127.0.0.1:%PORT%/v1/chat/completions
echo ========================================
echo.
echo 按 Ctrl+C 可停止服务
echo.

python api_server_new.py

echo.
echo 服务已停止。
pause
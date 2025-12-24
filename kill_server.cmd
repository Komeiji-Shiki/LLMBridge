@echo off
chcp 65001 >nul
echo ========================================
echo 终止 LMArena Bridge 后端进程
echo ========================================
echo.

echo 正在查找占用端口 5102 的进程...
for /f "tokens=5" %%a in ('netstat -ano ^| findstr :5102 ^| findstr LISTENING') do (
    echo 发现进程 PID: %%a
    echo 正在终止进程...
    taskkill /F /PID %%a
    if errorlevel 1 (
        echo 终止失败，可能需要管理员权限
    ) else (
        echo 进程已终止
    )
)

echo.
echo 正在查找所有 api_server.py 进程...
for /f "tokens=2" %%a in ('tasklist ^| findstr python.exe') do (
    wmic process where "ProcessId=%%a" get CommandLine /format:list | findstr api_server.py >nul
    if not errorlevel 1 (
        echo 发现 api_server.py 进程 PID: %%a
        taskkill /F /PID %%a
        echo 已终止
    )
)

echo.
echo ========================================
echo 清理完成
echo ========================================
pause
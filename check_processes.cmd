@echo off
chcp 65001 >nul
echo ========================================
echo 检查 Python 进程
echo ========================================
echo.

echo 所有 Python 进程:
echo.
wmic process where "name='python.exe' or name='pythonw.exe'" get ProcessId,CommandLine

echo.
echo ========================================
for /f "delims=" %%i in ('python "%~dp0scripts\read_config.py" server_port') do set PORT=%%i
if not defined PORT (
    echo [错误] 无法读取端口号，请确认 Python 可用。
    pause
    exit /b 1
)
echo 端口 %PORT% 占用情况:
echo ========================================
netstat -ano | findstr :%PORT%

echo.
echo ========================================
echo 如需终止，请使用 kill_server.cmd
echo ========================================
pause
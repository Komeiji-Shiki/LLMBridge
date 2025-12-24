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
echo 端口 5102 占用情况:
echo ========================================
netstat -ano | findstr :5102

echo.
echo ========================================
echo 如需终止，请使用 kill_server.cmd
echo ========================================
pause
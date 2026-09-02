@echo off
chcp 65001 >nul
REM ========================================
REM 刷新模型配置说明
REM 服务自带配置热更新：修改 model_endpoint_map.json
REM 后约 5 秒自动重载，无需手动刷新、无需重启。
REM （旧版此脚本启动已弃用的 id_updater，已移除）
REM ========================================

echo ========================================
echo 模型配置热更新说明
echo ========================================
echo.

if not exist model_endpoint_map.json (
    echo [错误] 当前目录下找不到 model_endpoint_map.json
    echo 请确认是在项目根目录运行本脚本。
    echo.
    pause
    exit /b 1
)

echo [1/2] 模型配置文件存在，正在检查服务状态...
netstat -ano | findstr :5102 | findstr LISTENING >nul
if errorlevel 1 (
    echo 服务似乎没有在运行。
    echo 请先用 点击启动.CMD 启动服务，修改 model_endpoint_map.json
    echo 后约 5 秒会自动生效。
) else (
    echo [2/2] 服务正在运行。
    echo 修改 model_endpoint_map.json 后约 5 秒会自动重载生效，
    echo 也可在管理面板直接编辑模型，保存即生效。
)
echo.
pause

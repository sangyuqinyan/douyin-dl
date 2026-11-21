@echo off
chcp 65001 >nul
if not exist "%~dp0.venv\Scripts\python.exe" (
  echo 运行环境缺失，请重新执行安装步骤。
  pause
  exit /b 1
)
echo 正在更新组件（playwright / httpx / f2），请稍候...
"%~dp0.venv\Scripts\python.exe" -m pip install -U playwright httpx f2 --index-url https://pypi.org/simple
echo.
echo 完成。
pause

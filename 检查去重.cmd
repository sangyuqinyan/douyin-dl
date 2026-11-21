@echo off
chcp 65001 >nul
if not exist "%~dp0.venv\Scripts\python.exe" (
  echo 运行环境缺失，请重新执行安装步骤。
  pause
  exit /b 1
)
"%~dp0.venv\Scripts\python.exe" "%~dp0douyin_batch.py" --out "%~dp0Download" --verify
echo.
pause

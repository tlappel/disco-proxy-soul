@echo off
cd /d "%~dp0"
if not exist .venv\Scripts\python.exe (
  echo Creating a local Python environment. First run can take a minute...
  py -m venv .venv
  if errorlevel 1 python -m venv .venv
)
.\.venv\Scripts\python.exe -m pip install -r requirements.txt --quiet
.\.venv\Scripts\python.exe -m disco_proxy_soul
echo.
pause

@echo off
echo Tax Radar MINIMAL 1.2.1
echo Tax Radar FINAL 1.1
cd /d %~dp0
where python >nul 2>nul
if errorlevel 1 (
  echo Python 3 not found. Install Python from python.org.
  pause
  exit /b 1
)
if not exist .venv python -m venv .venv
call .venv\Scripts\activate
python -m pip install -q --upgrade pip
python -m pip install -q -r requirements.txt
python run_local.py

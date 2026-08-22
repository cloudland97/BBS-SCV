@echo off
chcp 65001 > nul
cd /d "%~dp0"

echo Killing existing telegram bridge...
:: Matches telegram_bridge.py alone. The bbs-scv clause only held when the script
:: was launched by absolute path; an instance started as `python telegram_bridge.py`
:: from this folder survived, and two pollers on one token means Telegram rejects
:: getUpdates with 409 Conflict until one of them dies.
powershell -NoProfile -Command "Get-CimInstance Win32_Process | Where-Object { $_.Name -eq 'python.exe' -and $_.CommandLine -like '*telegram_bridge.py*' } | ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }"
:: Start-Sleep, not timeout: timeout.exe aborts immediately when stdin is not
:: a console, which is how the hidden launcher runs this script.
powershell -NoProfile -Command "Start-Sleep -Seconds 2"

set PYTHON=%~dp0.venv\Scripts\python.exe
if not exist "%PYTHON%" set PYTHON=python

echo Checking dependencies...
"%PYTHON%" -c "import telegram, aiohttp, telethon" > nul 2>&1
if errorlevel 1 (
    echo Installing missing packages...
    "%PYTHON%" -m pip install -r "%~dp0requirements.txt"
    if errorlevel 1 (
        echo [ERROR] pip install failed. Check Python installation.
        pause
        exit /b 1
    )
)

echo Starting telegram bridge...
"%PYTHON%" "%~dp0telegram_bridge.py"
pause

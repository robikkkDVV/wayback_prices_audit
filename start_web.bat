@echo off
cd /d "%~dp0"

echo ============================================================
echo   Wayback Audit
echo ============================================================
echo.

py --version >nul 2>&1
if errorlevel 1 (
    echo [ERROR] Python launcher "py" not found.
    pause
    exit /b 1
)

if not exist ".venv\Scripts\python.exe" (
    echo [1/3] Creating .venv ...
    py -m venv .venv
    if errorlevel 1 (
        echo [ERROR] Cannot create .venv
        pause
        exit /b 1
    )
    echo       OK
) else (
    echo [1/3] .venv exists
)

if not exist ".venv\.requirements_ok" (
    echo [2/3] Installing requirements.txt ...
    ".venv\Scripts\python.exe" -m pip install --upgrade pip
    ".venv\Scripts\python.exe" -m pip install -r requirements.txt
    if errorlevel 1 (
        echo [ERROR] pip install failed
        pause
        exit /b 1
    )
    echo ok> ".venv\.requirements_ok"
    echo       OK
) else (
    echo [2/3] Requirements already installed
)

echo [3/3] Stopping old Flask on port 5000 ...
for /f "tokens=5" %%p in ('netstat -ano ^| findstr ":5000" ^| findstr "LISTENING"') do (
    taskkill /F /PID %%p >nul 2>&1
)

timeout /t 1 /nobreak >nul

echo.
echo ============================================================
echo   Flask: http://127.0.0.1:5000
echo ============================================================
echo.

start "" /B ".venv\Scripts\python.exe" -m wayback_audit.web.app

timeout /t 4 /nobreak >nul

start "" "http://127.0.0.1:5000"

echo.
echo Do not close this window while Flask is running.
pause
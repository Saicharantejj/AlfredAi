@echo off
echo =====================================
echo    Booting Project Alfred (Windows)
echo =====================================

:: Activate virtual environment if it exists
if exist "venv\Scripts\activate.bat" (
    call venv\Scripts\activate.bat
)

echo Starting FastAPI Server on port 8000...
start "Alfred Server" /B uvicorn main:app --host 0.0.0.0 --port 8000

:: Pause to let server start
timeout /t 3 /nobreak >nul

where ngrok >nul 2>nul
if %errorlevel%==0 (
    echo Starting Ngrok tunnel...
    start "Ngrok Tunnel" /B ngrok http 8000 >nul

    timeout /t 3 /nobreak >nul
    echo.
    echo =====================================
    echo Alfred is LIVE remotely at your Ngrok URL!
    echo Check the Ngrok Web Interface at: http://localhost:4040
    echo =====================================
) else (
    echo Ngrok not found. Running local only ^(http://localhost:8000^).
    echo To access remotely, download ngrok and add it to your PATH.
)

pause

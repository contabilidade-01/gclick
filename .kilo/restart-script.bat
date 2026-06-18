@echo off
echo Restarting GCLICK server...

:: Kill any process using port 8080
for /f "tokens=5" %%a in ('netstat -ano ^| findstr :8080 ^| findstr LISTENING') do (
    echo Killing process %%a on port 8080...
    taskkill /F /PID %%a >nul 2>&1
)

:: Also try to kill uvicorn processes
taskkill /F /IM python.exe /FI "WINDOWTITLE eq *uvicorn*" >nul 2>&1

timeout /t 2 /nobreak >nul

cd /d "%~dp0.."
echo Starting fresh server on http://localhost:8080
uvicorn app.main:app --reload --host 0.0.0.0 --port 8080

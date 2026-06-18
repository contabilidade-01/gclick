@echo off
echo Starting GCLICK development server...
cd /d "%~dp0.."
uvicorn app.main:app --reload --host 0.0.0.0 --port 8080

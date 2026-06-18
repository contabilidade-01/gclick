# Run script for GCLICK - starts the dev server
# This script is used by Agent Manager to run the development server

$ErrorActionPreference = "Continue"

$ScriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$ProjectRoot = Split-Path -Parent $ScriptDir

Write-Host "Starting GCLICK development server..." -ForegroundColor Cyan

# Change to project directory
Set-Location $ProjectRoot

# Activate virtual environment if exists
$VenvPath = Join-Path $ProjectRoot "venv"
if (Test-Path $VenvPath) {
    $VenvActivate = Join-Path $VenvPath "Scripts\Activate.ps1"
    if (Test-Path $VenvActivate) {
        Write-Host "Activating virtual environment..." -ForegroundColor Yellow
        & $VenvActivate
    }
}

# Start the server
$Port = "8080"
Write-Host "Starting uvicorn on http://localhost:$Port" -ForegroundColor Green

uvicorn app.main:app --reload --host 0.0.0.0 --port $Port
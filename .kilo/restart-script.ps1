# Restart script for GCLICK - stops existing server and starts a new one
# This script stops any existing uvicorn processes and starts fresh

$ProjectRoot = "C:\Users\Jeandson\OneDrive\01_Jean\00_Claude\00_PROJETOS\GCLICK"
$Port = "8080"

Write-Host "Restarting GCLICK server..." -ForegroundColor Cyan

# Stop any existing server on port 8000
$Process = Get-Process -Name "python" -ErrorAction SilentlyContinue | Where-Object {
    $_.CommandLine -like "*uvicorn*" -or $_.CommandLine -like "*app.main*"
}

if ($Process) {
    Write-Host "Stopping existing server processes..." -ForegroundColor Yellow
    Stop-Process -Id $Process.Id -Force -ErrorAction SilentlyContinue
    Start-Sleep -Seconds 2
}

# Kill any process using port 8000
$Connection = Get-NetTCPConnection -LocalPort $Port -ErrorAction SilentlyContinue
if ($Connection) {
    Write-Host "Killing process on port $Port..." -ForegroundColor Yellow
    Stop-Process -Id $Connection.OwningProcess -Force -ErrorAction SilentlyContinue
    Start-Sleep -Seconds 1
}

Write-Host "Starting fresh server on http://localhost:$Port" -ForegroundColor Green

# Start new server
Set-Location $ProjectRoot
uvicorn app.main:app --reload --host 0.0.0.0 --port $Port
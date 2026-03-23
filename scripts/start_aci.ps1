# Start OpenClaw ACI Framework (all services)
# Uses fast native detection tiers (CursorProbe, OCR, Contour, VLM).
#
# Parameters:
#   -DesktopOnly    Skip web bridge (no Chrome window). Use for desktop-only tasks.
#   -Headed         Start web bridge in headed (visible) mode. Default: headless.
param(
    [switch]$DesktopOnly,
    [switch]$Headed
)

$ErrorActionPreference = "Stop"
$ScriptDir = Split-Path $MyInvocation.MyCommand.Path
$BaseDir = Split-Path $ScriptDir -Parent

# Force UTF-8 for all child processes to avoid GBK encoding crashes.
$env:PYTHONIOENCODING = "utf-8"

# --- Paths ---
$PidFile = "$BaseDir\.openclaw_daemon.pid"

# Locate Python: prefer venv, then system python.
$VenvPython = "$BaseDir\.venv\Scripts\python.exe"
if (Test-Path $VenvPython) {
    $Python = $VenvPython
    Write-Host "Using venv Python: $VenvPython" -ForegroundColor DarkGray
} else {
    $Python = "python"
    Write-Host "[INFO] No .venv found — using system Python. Consider: python -m venv .venv" -ForegroundColor DarkYellow
}

# --- Helper: check if a TCP port is already in use ---
function Test-PortInUse {
    param([int]$Port)
    $conn = Get-NetTCPConnection -LocalPort $Port -State Listen -ErrorAction SilentlyContinue
    if ($conn) {
        $pid = $conn.OwningProcess | Select-Object -First 1
        Write-Host "  [WARN] Port $Port already in use by PID $pid" -ForegroundColor Yellow
        return $true
    }
    return $false
}

# --- 1. OpenClaw Daemon (port 11434) ---
if (Test-PortInUse -Port 11434) {
    Write-Host "  Skipping daemon launch - port 11434 already occupied." -ForegroundColor DarkYellow
    # Verify existing daemon is healthy
    try {
        $resp = Invoke-RestMethod -Uri "http://127.0.0.1:11434/health" -TimeoutSec 3 -ErrorAction SilentlyContinue
        if ($resp.status -eq "ok") {
            Write-Host "  Existing daemon is healthy." -ForegroundColor Green
        }
    } catch {
        Write-Host "  [WARN] Port 11434 occupied but daemon not responding - consider running stop_aci.ps1 first." -ForegroundColor Yellow
    }
} else {
    Write-Host "Starting OpenClaw Daemon on port 11434..." -ForegroundColor Cyan
    Start-Process -FilePath $Python `
        -ArgumentList "-m core.server" `
        -WorkingDirectory $BaseDir -WindowStyle Minimized

    # Wait for daemon to be ready before starting bridges.
    Write-Host "  Waiting for daemon..." -ForegroundColor DarkCyan
    for ($i = 0; $i -lt 15; $i++) {
        Start-Sleep -Seconds 2
        try {
            $resp = Invoke-RestMethod -Uri "http://127.0.0.1:11434/health" -TimeoutSec 3 -ErrorAction SilentlyContinue
            if ($resp.status -eq "ok") {
                Write-Host "  Daemon is ready." -ForegroundColor Green
                break
            }
        } catch {}
    }
}

# --- 2. Web Bridge Worker (lazy browser — no Chrome opens until first web command) ---
if (-not $DesktopOnly) {
    $WebArgs = "-m bridges.web_bridge.worker"
    if ($Headed) {
        $WebArgs += " --headed"
        Write-Host "Starting Web Bridge Worker (headed)..." -ForegroundColor Cyan
    } else {
        Write-Host "Starting Web Bridge Worker (headless, lazy)..." -ForegroundColor Cyan
    }
    Start-Process -FilePath $Python `
        -ArgumentList $WebArgs `
        -WorkingDirectory $BaseDir -WindowStyle Minimized
} else {
    Write-Host "  Skipping Web Bridge (--DesktopOnly mode)." -ForegroundColor DarkYellow
}

# --- 3. Desktop Bridge Worker ---
# Uses native detection tiers (cursor probe, OCR, contour, VLM).
Write-Host "Starting Desktop Bridge Worker..." -ForegroundColor Cyan
Start-Process -FilePath $Python `
    -ArgumentList "-m bridges.desktop_bridge.worker" `
    -WorkingDirectory $BaseDir -WindowStyle Minimized

Start-Sleep -Seconds 2

# --- Status ---
Write-Host ""
Write-Host "OpenClaw ACI cluster is running!" -ForegroundColor Green
Write-Host ""
Write-Host "Services:" -ForegroundColor Cyan
Write-Host "  Daemon:         http://127.0.0.1:11434/health"
Write-Host "  Web Bridge:     connected via WebSocket"
Write-Host "  Desktop Bridge: connected via WebSocket (native detection tiers)"
Write-Host ""
Write-Host "Detection tiers: UIA -> CursorProbe -> FastOCR -> Contour -> VLM (last resort)" -ForegroundColor DarkCyan
Write-Host ""
Write-Host "To stop all: .\scripts\stop_aci.ps1" -ForegroundColor DarkGray

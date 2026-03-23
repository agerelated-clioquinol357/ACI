# Stop OpenClaw ACI Framework (all services)
$BaseDir = Split-Path (Split-Path $MyInvocation.MyCommand.Path) -Parent
$PidFile = "$BaseDir\.openclaw_daemon.pid"

Write-Host "Stopping all OpenClaw ACI processes..." -ForegroundColor Yellow

# 1. WMI-based kill (primary method — matches by command line).
$wmiQuery = "CommandLine LIKE '%core.server%' OR CommandLine LIKE '%bridges.web_bridge.worker%' OR CommandLine LIKE '%bridges.desktop_bridge.worker%'"
Get-WmiObject Win32_Process -Filter $wmiQuery | ForEach-Object {
    Write-Host "  Killing PID $($_.ProcessId) - $($_.Name) (WMI match)" -ForegroundColor DarkGray
    Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue
}

# 2. Port-based kill as fallback (catches processes missed by WMI query).
foreach ($port in @(11434)) {
    $conns = Get-NetTCPConnection -LocalPort $port -State Listen -ErrorAction SilentlyContinue
    foreach ($c in $conns) {
        $pid = $c.OwningProcess
        if ($pid -and $pid -ne 0) {
            $proc = Get-Process -Id $pid -ErrorAction SilentlyContinue
            if ($proc) {
                Write-Host "  Killing PID $pid ($($proc.ProcessName)) on port $port (port fallback)" -ForegroundColor DarkGray
                Stop-Process -Id $pid -Force -ErrorAction SilentlyContinue
            }
        }
    }
}

# 3. Clean up PID file.
if (Test-Path $PidFile) {
    Remove-Item $PidFile -Force -ErrorAction SilentlyContinue
    Write-Host "  Removed PID file." -ForegroundColor DarkGray
}

Write-Host "All ACI processes stopped." -ForegroundColor Green

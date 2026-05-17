# FEF Terminal launcher.
#
# Launches:
#   1. Python sidecar (FastAPI + signal engine) wrapped in a watchdog loop
#   2. Tauri app — release build by default (instant), or dev mode with -Dev

param(
    [ValidateSet("mock", "live")]
    [string]$QuantDataMode = "mock",

    [ValidateSet("mock", "live")]
    [string]$IbkrMode = "mock",

    [int]$LoopIntervalSec = 120,

    [switch]$Dev,

    # Replay a synthetic full trading day through every regime state
    # (OPEN_DRIVE -> TREND -> ABSORPTION -> PIN -> WALL_TEST -> HIGH_VOL ->
    #  CHOP -> POWER_HOUR). Auto-sets fast 3s cadence so the day replays
    # in ~10 minutes.
    [switch]$ScriptedDay
)

$ErrorActionPreference = "Continue"
$repo = Split-Path -Parent $PSScriptRoot

# Scripted-day mode forces mock providers + fast cadence so we can see
# every regime phase fire in ~10 minutes instead of 5.5hr.
if ($ScriptedDay) {
    $QuantDataMode = "mock"
    $IbkrMode = "mock"
    if ($LoopIntervalSec -eq 120) { $LoopIntervalSec = 3 }
    Write-Host "[SCRIPTED DAY MODE] mocks forced, 3s cadence" -ForegroundColor Magenta
}

Write-Host "FEF Terminal launcher" -ForegroundColor Cyan
Write-Host ("  Repo:          " + $repo)
Write-Host ("  QuantData:     " + $QuantDataMode)
Write-Host ("  IBKR:          " + $IbkrMode)
Write-Host ("  FEF interval:  " + $LoopIntervalSec + "s")
Write-Host ("  Scripted day:  " + $ScriptedDay)
Write-Host ""

# Sanity checks
$venvPython = Join-Path $repo "sidecar\.venv\Scripts\python.exe"
if (-not (Test-Path $venvPython)) {
    Write-Error "Python venv not found at $venvPython"
    Start-Sleep -Seconds 5
    exit 1
}

# Warn if live IBKR but TWS not running
if ($IbkrMode -eq "live") {
    $tws = Get-Process -Name "tws", "ibgateway" -ErrorAction SilentlyContinue
    if (-not $tws) {
        Write-Warning "TWS / IB Gateway not running. Continue anyway? (Ctrl+C to abort)"
        Start-Sleep -Seconds 3
    }
}

# Build sidecar watchdog script as a single-line block.
# Using a script-block string avoids here-string quoting issues in PS 5.1.
$scriptedDayEnv = if ($ScriptedDay) { "`$env:FEF_MOCK_SCRIPTED_DAY='true'; " } else { "" }
$watchdogScript = "Set-Location '" + (Join-Path $repo "sidecar") + "'; " + `
    "& .\.venv\Scripts\Activate.ps1; " + `
    "`$env:FEF_PROVIDER_QUANTDATA='" + $QuantDataMode + "'; " + `
    "`$env:FEF_PROVIDER_IBKR='" + $IbkrMode + "'; " + `
    "`$env:FEF_LOOP_INTERVAL_SEC='" + $LoopIntervalSec + "'; " + `
    $scriptedDayEnv + `
    "while (`$true) { " + `
        "Write-Host '[watchdog] Starting fef-sidecar...' -ForegroundColor Yellow; " + `
        "fef-sidecar; " + `
        "Write-Host '[watchdog] Sidecar exited. Restarting in 3s...' -ForegroundColor Red; " + `
        "Start-Sleep -Seconds 3 " + `
    "}"

Write-Host "[FEF] Starting Python sidecar (with watchdog)..." -ForegroundColor Cyan
Start-Process "powershell.exe" -ArgumentList "-NoExit", "-NoProfile", "-Command", $watchdogScript -WindowStyle Normal

# Poll /healthz for up to 30s
Write-Host "[FEF] Waiting for sidecar /healthz..." -ForegroundColor Cyan
$ready = $false
for ($i = 0; $i -lt 30; $i++) {
    Start-Sleep -Seconds 1
    try {
        $r = Invoke-WebRequest -Uri "http://127.0.0.1:8765/healthz" -TimeoutSec 1 -UseBasicParsing -ErrorAction Stop
        if ($r.StatusCode -eq 200) { $ready = $true; break }
    } catch {}
}
if (-not $ready) {
    Write-Warning "Sidecar didn't respond within 30s."
} else {
    Write-Host "[OK] Sidecar ready" -ForegroundColor Green
}

# Launch Tauri — release exe by default, dev mode if -Dev passed
$releaseExe = "D:\fef-terminal-builds\target\release\fef-terminal.exe"
if ($Dev -or -not (Test-Path $releaseExe)) {
    if (-not (Test-Path $releaseExe)) {
        Write-Warning ("Release exe not found at " + $releaseExe + " - using dev mode")
    }
    Write-Host "[FEF] Launching Tauri DEV mode..." -ForegroundColor Cyan
    $cargoBin = Join-Path $env:USERPROFILE ".cargo\bin"
    $tauriCmd = "Set-Location '" + $repo + "'; `$env:Path = '" + $cargoBin + ";' + `$env:Path; npm run tauri dev"
    Start-Process "powershell.exe" -ArgumentList "-NoExit", "-NoProfile", "-Command", $tauriCmd -WindowStyle Normal
} else {
    Write-Host "[FEF] Launching Tauri release..." -ForegroundColor Cyan
    Start-Process $releaseExe
}

Write-Host ""
Write-Host "[FEF] All processes started." -ForegroundColor Green
Start-Sleep -Seconds 2

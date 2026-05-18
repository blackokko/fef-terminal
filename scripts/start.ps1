# FEF Terminal launcher.
#
# Launches:
#   1. Python sidecar (FastAPI + signal engine) wrapped in a watchdog loop
#   2. Tauri app — release build by default (instant), or dev mode with -Dev

param(
    # Defaults to LIVE for both — if you want mock, pass -QuantDataMode mock
    # and -IbkrMode mock (or use scripts\FEF-Mock.bat).
    [ValidateSet("mock", "live")]
    [string]$QuantDataMode = "live",

    [ValidateSet("mock", "live")]
    [string]$IbkrMode = "live",

    [int]$LoopIntervalSec = 120,

    [switch]$Dev,

    [switch]$ScriptedDay,

    # Which day archetype to replay (only used with -ScriptedDay)
    [ValidateSet("5_15_REPLAY", "STEADY_BULL", "STEADY_BEAR", "REVERSAL",
                 "PURE_CHOP", "FED_DAY", "GAP_AND_FADE")]
    [string]$Archetype = "5_15_REPLAY",

    # Mock seed (only used with -ScriptedDay). 0 = stochastic (time-based seed)
    [int]$MockSeed = 42,

    # Enable FEF_LIVE_TRADING — required for /live/place_bracket and
    # auto-trade to actually submit orders to IBKR (paper or live account
    # depending on which TWS port is listening). Without this switch,
    # those calls all run in DRY_RUN mode (write records, no submission).
    [switch]$LiveTrading,

    # Force a specific IBKR port. If set, skips auto-detection. Use this
    # via the explicit FEF-Live-*.bat / FEF-Paper-*.bat launchers so you
    # always know which account you're connected to.
    #   7496 = LIVE account
    #   7497 = PAPER account
    [int]$IbkrPort = 0
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

# Kill any existing sidecar / fef-terminal so we don't accidentally connect
# to a stale mock instance from a previous session.
Write-Host "[FEF] Stopping any existing FEF processes..." -ForegroundColor DarkGray
Stop-Process -Name fef-terminal -Force -ErrorAction SilentlyContinue
$pid_holding = (Get-NetTCPConnection -LocalPort 8765 -State Listen -ErrorAction SilentlyContinue | Select-Object -First 1).OwningProcess
if ($pid_holding) { Stop-Process -Id $pid_holding -Force -ErrorAction SilentlyContinue }
$me = $PID
Get-Process powershell, pwsh -ErrorAction SilentlyContinue | Where-Object {
    $_.Id -ne $me -and (Get-WmiObject Win32_Process -Filter "ProcessId=$($_.Id)" -ErrorAction SilentlyContinue).CommandLine -match 'fef-sidecar|fef_sidecar'
} | ForEach-Object { Stop-Process -Id $_.Id -Force -ErrorAction SilentlyContinue }
Start-Sleep -Seconds 2

# Resolve IBKR port. Three paths:
#   (a) -IbkrPort explicitly set → force that port (fail if not listening)
#   (b) -IbkrMode live + no port → auto-detect (legacy, less safe)
#   (c) -IbkrMode mock → port unused
$ibkrPort = "7497"
if ($IbkrMode -eq "live") {
    $tws = Get-Process -Name "tws", "ibgateway" -ErrorAction SilentlyContinue
    if (-not $tws) {
        Write-Warning "TWS / IB Gateway not running. Continue anyway? (Ctrl+C to abort)"
        Start-Sleep -Seconds 3
    }
    if ($IbkrPort -gt 0) {
        # Explicit port — verify it's listening and refuse if not (safer)
        $portReachable = Test-NetConnection -ComputerName 127.0.0.1 -Port $IbkrPort -InformationLevel Quiet -WarningAction SilentlyContinue
        if (-not $portReachable) {
            $acctLabel = if ($IbkrPort -eq 7496) { "LIVE" } elseif ($IbkrPort -eq 7497) { "PAPER" } else { "custom" }
            Write-Error "Forced IBKR port $IbkrPort ($acctLabel account) is not listening. Log into the right TWS account first, then re-run."
            Start-Sleep -Seconds 5
            exit 1
        }
        $ibkrPort = "$IbkrPort"
        $label = if ($IbkrPort -eq 7496) { "LIVE account" } elseif ($IbkrPort -eq 7497) { "PAPER account" } else { "custom port" }
        Write-Host "  IBKR port:     $IbkrPort ($label - forced)" -ForegroundColor Yellow
    } else {
        # Auto-detect path — convenience but doesn't tell you which account
        $live = Test-NetConnection -ComputerName 127.0.0.1 -Port 7496 -InformationLevel Quiet -WarningAction SilentlyContinue
        $paper = Test-NetConnection -ComputerName 127.0.0.1 -Port 7497 -InformationLevel Quiet -WarningAction SilentlyContinue
        if ($live) {
            $ibkrPort = "7496"
            Write-Host "  IBKR port:     7496 (live account auto-detected)" -ForegroundColor Yellow
        } elseif ($paper) {
            $ibkrPort = "7497"
            Write-Host "  IBKR port:     7497 (paper account auto-detected)" -ForegroundColor Yellow
        } else {
            Write-Warning "Neither 7496 (live) nor 7497 (paper) is listening. Defaulting to 7497."
        }
    }
}

# Build sidecar watchdog script as a single-line block.
# Using a script-block string avoids here-string quoting issues in PS 5.1.
$scriptedDayEnv = ""
if ($ScriptedDay) {
    $scriptedDayEnv = "`$env:FEF_MOCK_SCRIPTED_DAY='true'; " +
                      "`$env:FEF_MOCK_ARCHETYPE='" + $Archetype + "'; " +
                      "`$env:FEF_MOCK_SEED='" + $MockSeed + "'; "
    Write-Host ("  Archetype:     " + $Archetype) -ForegroundColor Magenta
    Write-Host ("  Mock seed:     " + $MockSeed + $(if ($MockSeed -eq 0) { " (stochastic)" } else { " (deterministic)" })) -ForegroundColor Magenta
}
$liveTradingEnv = if ($LiveTrading) {
    Write-Host "  Live trading:  ENABLED (orders WILL submit to IBKR on port $ibkrPort)" -ForegroundColor Red
    "`$env:FEF_LIVE_TRADING='true'; "
} else { "" }

$watchdogScript = "Set-Location '" + (Join-Path $repo "sidecar") + "'; " + `
    "& .\.venv\Scripts\Activate.ps1; " + `
    "`$env:FEF_PROVIDER_QUANTDATA='" + $QuantDataMode + "'; " + `
    "`$env:FEF_PROVIDER_IBKR='" + $IbkrMode + "'; " + `
    "`$env:FEF_IBKR_PORT='" + $ibkrPort + "'; " + `
    "`$env:FEF_LOOP_INTERVAL_SEC='" + $LoopIntervalSec + "'; " + `
    $liveTradingEnv + `
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

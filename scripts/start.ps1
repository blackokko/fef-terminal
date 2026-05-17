#requires -Version 5.1
<#
.SYNOPSIS
    FEF Terminal daily launcher (PowerShell version).

.DESCRIPTION
    More sophisticated than start.bat:
    - Checks for TWS/IB Gateway and warns if not running (when using live IBKR)
    - Waits for sidecar /healthz before launching the frontend
    - Surfaces config (provider modes, ports) before starting

.PARAMETER QuantDataMode
    "mock" (default) or "live"

.PARAMETER IbkrMode
    "mock" (default) or "live"

.PARAMETER LoopIntervalSec
    FEF loop tick interval in seconds. Default 120.

.EXAMPLE
    .\start.ps1
    Launch with both providers in mock mode (offline dev).

.EXAMPLE
    .\start.ps1 -QuantDataMode live -IbkrMode live -LoopIntervalSec 60
    Launch with both providers live and faster 1-min FEF cadence.
#>

param(
    [ValidateSet("mock", "live")]
    [string]$QuantDataMode = "mock",

    [ValidateSet("mock", "live")]
    [string]$IbkrMode = "mock",

    [int]$LoopIntervalSec = 120,

    # Default to the release build for fast startup. Pass -Dev to use
    # `npm run tauri dev` instead (HMR + dev tools, slower).
    [switch]$Dev
)

$ErrorActionPreference = "Stop"
$repo = Split-Path -Parent $PSScriptRoot

Write-Host "FEF Terminal launcher" -ForegroundColor Cyan
Write-Host "  Repo:          $repo"
Write-Host "  QuantData:     $QuantDataMode"
Write-Host "  IBKR:          $IbkrMode"
Write-Host "  FEF interval:  ${LoopIntervalSec}s"
Write-Host ""

# Sanity checks
$venvPython = Join-Path $repo "sidecar\.venv\Scripts\python.exe"
if (-not (Test-Path $venvPython)) {
    Write-Error "Python venv not found at $venvPython. Run setup first (see README)."
    exit 1
}

$nodeModules = Join-Path $repo "node_modules"
if (-not (Test-Path $nodeModules)) {
    Write-Error "node_modules not found. Run: npm install"
    exit 1
}

# Warn if live IBKR but TWS/Gateway not running
if ($IbkrMode -eq "live") {
    $tws = Get-Process -Name "tws", "ibgateway" -ErrorAction SilentlyContinue
    if (-not $tws) {
        Write-Warning "TWS / IB Gateway is not running. Start it before connecting in live mode."
        Write-Warning "Press Enter to continue anyway, or Ctrl+C to abort."
        Read-Host | Out-Null
    } else {
        Write-Host "[OK] TWS/Gateway process detected: $($tws.Name -join ', ')" -ForegroundColor Green
    }
}

# Spawn sidecar
$env:FEF_PROVIDER_QUANTDATA = $QuantDataMode
$env:FEF_PROVIDER_IBKR = $IbkrMode
$env:FEF_LOOP_INTERVAL_SEC = $LoopIntervalSec.ToString()

# Use pwsh (PS7+) if available, otherwise fall back to Windows PowerShell 5.1.
$psExe = if (Get-Command pwsh -ErrorAction SilentlyContinue) { "pwsh" } else { "powershell" }
Write-Host "[FEF] Using shell: $psExe" -ForegroundColor DarkGray

# Add Rust cargo bin to PATH so `npm run tauri` finds cargo even in fresh shells
$cargoBin = Join-Path $env:USERPROFILE ".cargo\bin"

# IMPROVEMENTS #O: spawn sidecar in a watchdog loop — auto-respawn after 3s if it crashes
Write-Host "[FEF] Starting Python sidecar (with watchdog)..." -ForegroundColor Cyan
$sidecarLoop = @"
`$env:FEF_PROVIDER_QUANTDATA='$QuantDataMode'
`$env:FEF_PROVIDER_IBKR='$IbkrMode'
`$env:FEF_LOOP_INTERVAL_SEC='$LoopIntervalSec'
Set-Location '$repo\sidecar'
& .\.venv\Scripts\Activate.ps1
while (`$true) {
  Write-Host "[watchdog] Starting fef-sidecar at `$(Get-Date -Format 'HH:mm:ss')..." -ForegroundColor Yellow
  fef-sidecar
  Write-Host "[watchdog] Sidecar exited (code `$LASTEXITCODE). Restarting in 3s..." -ForegroundColor Red
  Start-Sleep -Seconds 3
}
"@
Start-Process $psExe -ArgumentList "-NoExit", "-Command", $sidecarLoop -WindowStyle Normal

# Wait for /healthz
Write-Host "[FEF] Waiting for sidecar /healthz..." -ForegroundColor Cyan
$ready = $false
for ($i = 0; $i -lt 30; $i++) {
    Start-Sleep -Seconds 1
    try {
        $r = Invoke-WebRequest -Uri "http://127.0.0.1:8765/healthz" -TimeoutSec 1 -UseBasicParsing
        if ($r.StatusCode -eq 200) {
            $ready = $true
            break
        }
    } catch {
        # Not ready yet
    }
}
if (-not $ready) {
    Write-Warning "Sidecar didn't respond on /healthz within 30s. Check its console for errors."
} else {
    Write-Host "[OK] Sidecar ready" -ForegroundColor Green
}

# Spawn Tauri app — release exe by default (fast), dev mode if -Dev passed
$releaseExe = "D:\fef-terminal-builds\target\release\fef-terminal.exe"
if ($Dev -or -not (Test-Path $releaseExe)) {
    if (-not (Test-Path $releaseExe)) {
        Write-Warning "Release exe not found at $releaseExe — falling back to dev mode."
        Write-Warning "Run 'npm run tauri build' from $repo to produce the release build."
    }
    Write-Host "[FEF] Launching Tauri DEV..." -ForegroundColor Cyan
    $tauriCmd = "Set-Location '$repo'; `$env:Path = '$cargoBin;' + `$env:Path; npm run tauri dev"
    Start-Process $psExe -ArgumentList "-NoExit", "-Command", $tauriCmd -WindowStyle Normal
} else {
    Write-Host "[FEF] Launching Tauri release: $releaseExe" -ForegroundColor Cyan
    Start-Process $releaseExe
}

Write-Host ""
Write-Host "[FEF] All processes started. Close each window to stop." -ForegroundColor Green

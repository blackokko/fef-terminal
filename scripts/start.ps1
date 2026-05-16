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

    [int]$LoopIntervalSec = 120
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

Write-Host "[FEF] Starting Python sidecar..." -ForegroundColor Cyan
$sidecarCmd = "cd '$repo\sidecar'; .venv\Scripts\activate; `$env:FEF_PROVIDER_QUANTDATA='$QuantDataMode'; `$env:FEF_PROVIDER_IBKR='$IbkrMode'; `$env:FEF_LOOP_INTERVAL_SEC='$LoopIntervalSec'; fef-sidecar"
Start-Process pwsh -ArgumentList "-NoExit", "-Command", $sidecarCmd -WindowStyle Normal

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

# Spawn Tauri dev
Write-Host "[FEF] Launching Tauri app..." -ForegroundColor Cyan
$tauriCmd = "cd '$repo'; npm run tauri dev"
Start-Process pwsh -ArgumentList "-NoExit", "-Command", $tauriCmd -WindowStyle Normal

Write-Host ""
Write-Host "[FEF] All processes started. Close each window to stop." -ForegroundColor Green

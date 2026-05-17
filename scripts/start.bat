@echo off
REM FEF Terminal — daily launcher (simple batch version).
REM
REM Spawns:
REM   1. Python sidecar (FastAPI + signal engine) at http://127.0.0.1:8765
REM   2. Tauri dev app (Vite + Rust shell)
REM
REM Does NOT auto-start TWS / IB Gateway — start that yourself first if you
REM want live IBKR data. The sidecar defaults to mock providers (set
REM FEF_PROVIDER_QUANTDATA=live and FEF_PROVIDER_IBKR=live to switch).

setlocal
set REPO=%~dp0..

REM Sanity check: venv + node_modules must exist
if not exist "%REPO%\sidecar\.venv\Scripts\python.exe" (
  echo [ERROR] Python venv not found. Run from sidecar\:
  echo   py -3.12 -m venv .venv ^&^& .venv\Scripts\activate ^&^& pip install -e .[dev]
  exit /b 1
)
if not exist "%REPO%\node_modules" (
  echo [ERROR] node_modules not found. Run: npm install
  exit /b 1
)

echo [FEF] Starting Python sidecar...
start "fef-sidecar" /D "%REPO%\sidecar" cmd /k ".venv\Scripts\activate && fef-sidecar"

REM Give sidecar a moment to bind 127.0.0.1:8765
timeout /t 3 /nobreak >nul

REM Prefer the release exe if it exists; fall back to dev mode otherwise.
set RELEASE_EXE=D:\fef-terminal-builds\target\release\fef-terminal.exe
if exist "%RELEASE_EXE%" (
  echo [FEF] Launching Tauri release: %RELEASE_EXE%
  start "" "%RELEASE_EXE%"
) else (
  echo [FEF] Release exe not found, launching dev mode (slower startup)...
  start "fef-terminal" /D "%REPO%" cmd /k "npm run tauri dev"
)

echo [FEF] Both processes started. Close each console window to stop.
endlocal

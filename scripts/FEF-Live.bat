@echo off
REM FEF Terminal — LIVE launcher. Double-click to start.
REM
REM This is the one you should be running daily. It:
REM   1. Kills any existing sidecar + fef-terminal exe
REM   2. Verifies TWS / IB Gateway is reachable
REM   3. Launches the sidecar in LIVE mode (both IBKR + QuantData live)
REM   4. Launches the Tauri release exe
REM
REM Make a Desktop shortcut to this file and you're set.

REM Forces port 7496 (LIVE account). DRY_RUN orders only (no live trading
REM env). For real order submission use FEF-Live-AutoTrade.bat instead.
powershell.exe -ExecutionPolicy Bypass -NoProfile -File "%~dp0start.ps1" -IbkrMode live -QuantDataMode live -IbkrPort 7496

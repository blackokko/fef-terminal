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

powershell.exe -ExecutionPolicy Bypass -NoProfile -File "%~dp0start.ps1" -IbkrMode live -QuantDataMode live

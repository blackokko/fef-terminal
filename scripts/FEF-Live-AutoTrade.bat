@echo off
REM FEF Terminal — LIVE ACCOUNT + REAL ORDER SUBMISSION.
REM ⚠ Forces port 7496. Real money. Use this only after validating on paper.

set FEF_LOOP_MARKET_HOURS_ONLY=true
powershell.exe -ExecutionPolicy Bypass -NoProfile -File "%~dp0start.ps1" -IbkrMode live -QuantDataMode live -LiveTrading -IbkrPort 7496

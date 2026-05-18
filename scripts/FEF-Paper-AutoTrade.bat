@echo off
REM FEF Terminal — PAPER account + LIVE TRADING enabled.
REM Forces port 7497. place_bracket() will submit real orders to your
REM PAPER account (no real money). Auto-trade toggle in the UI still
REM needs to be flipped on per session.

set FEF_LOOP_MARKET_HOURS_ONLY=true
powershell.exe -ExecutionPolicy Bypass -NoProfile -File "%~dp0start.ps1" -IbkrMode live -QuantDataMode live -LiveTrading -IbkrPort 7497

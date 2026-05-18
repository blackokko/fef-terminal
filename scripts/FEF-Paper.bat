@echo off
REM FEF Terminal — PAPER account, NO trading (data only).
REM Forces port 7497. Refuses to launch if paper TWS isn't running.

powershell.exe -ExecutionPolicy Bypass -NoProfile -File "%~dp0start.ps1" -IbkrMode live -QuantDataMode live -IbkrPort 7497

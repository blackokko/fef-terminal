@echo off
REM FEF Terminal — MOCK launcher. For UI testing without TWS / live data.

powershell.exe -ExecutionPolicy Bypass -NoProfile -File "%~dp0start.ps1" -IbkrMode mock -QuantDataMode mock

@echo off
REM FEF Terminal — PAPER account + LIVE TRADING enabled.
REM Forces port 7497. place_bracket() will submit real orders to your
REM PAPER account (no real money). Auto-trade toggle in the UI still
REM needs to be flipped on per session.
REM
REM -CreditSpreads (added 2026-08-17) arms FEF_ALLOW_CREDIT_SPREADS, the
REM opt-in for the uncovered short leg of a credit spread. Without it the
REM 0DTE credit-spread auto-trader runs every gate and is then refused at
REM the broker call, which is what happened on 2026-08-12.
REM
REM The credit-spread module now has API routes (GET/POST /spread_auto_trade/*)
REM and a UI toggle (SpreadAutoTradePanel), default OFF. -CreditSpreads is the
REM CAPABILITY (env var); the panel is the INTENT. Both must be on before a
REM spread transmits. The 0DTE directional bot was retired 2026-08-25 when
REM QuantData was cancelled; -QuantDataMode is inert (nothing in the sidecar
REM reads it any more) and stays only so start.ps1's parameter list matches.

set FEF_LOOP_MARKET_HOURS_ONLY=true
powershell.exe -ExecutionPolicy Bypass -NoProfile -File "%~dp0start.ps1" -IbkrMode live -QuantDataMode live -LiveTrading -CreditSpreads -IbkrPort 7497

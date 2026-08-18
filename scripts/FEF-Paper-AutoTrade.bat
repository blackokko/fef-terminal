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
REM ⚠ ASYMMETRY, DELIBERATE, AND TEMPORARY. The 0DTE directional bot still
REM requires a per-session UI toggle (see above). The credit-spread module
REM has NO API route and NO UI toggle, so once this launcher arms it, it
REM will place a spread on the next qualifying iteration with no further
REM confirmation. Remove -CreditSpreads to go back to gates-run-nothing-sent.
REM Building the UI toggle removes this asymmetry; until then, this flag IS
REM the toggle.

set FEF_LOOP_MARKET_HOURS_ONLY=true
powershell.exe -ExecutionPolicy Bypass -NoProfile -File "%~dp0start.ps1" -IbkrMode live -QuantDataMode live -LiveTrading -CreditSpreads -IbkrPort 7497

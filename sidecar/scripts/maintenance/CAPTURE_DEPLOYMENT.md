# Continuous Microstructure Capture — Deployment Note

Runs `capture_microstructure_standalone.py` every market session so the
`microstructure_sample` / `hft_sample` tables fill in **continuously**, decoupled
from the trading engine (which only sampled while you were actively trading — the
cause of the month-long June 2026 gap).

## What it does / why it's safe next to the live sidecar
- Connects to the **same TWS/Gateway** but on an **isolated `clientId` (151)**. The
  live sidecar uses `clientId 1` (+`101` for history). TWS supports many API
  clients at once; client 151 cannot see or touch client 1's orders/subscriptions.
  The daemon **refuses to start** if `--client-id` is 1 or 101.
- The IBKR socket is opened with **`readonly=True`** → TWS rejects any order
  placement/modify/cancel from this client at the protocol level. The daemon has
  **no order code path** at all — it only calls market-data subscription APIs.
- Writes go to the same `signal_log.db`, which is in **WAL mode with a 30s
  busy_timeout** (see `db/engine.py`). The sidecar already runs several concurrent
  writers; one more 1 Hz writer is safe and non-blocking. It only runs
  `init_db()` (idempotent) if the sample tables are missing — normal runs take
  **zero schema locks**.
- Captures **ES** (full order-book microstructure — OFI/microprice/stuffing/L2
  depth/realized-vol) and **SPX** (index realized-vol only; the cash index has no
  book or tape, so book/stuffing columns are written with honest `empty` badges
  and no `microstructure_sample` row). Each row is tagged by its **real** symbol.

## Prerequisites
1. **TWS or IB Gateway running and logged in** during 09:30–16:00 ET, with the API
   enabled (Global Config → API → Settings → Enable ActiveX and Socket Clients,
   Trusted IP 127.0.0.1, port 7497 for paper). **IB Gateway is recommended** for
   an always-on capture host — it's lighter than TWS and supports auto-restart.
   Capture only works while this is up.
2. The project venv (`.venv`) with `fef-sidecar` installed (already the case).

## Manual run
```powershell
# Full session — waits for 09:30 ET open if launched early, auto-exits ~16:05 ET
C:\Users\diant\projects\fef-terminal\sidecar\.venv\Scripts\python.exe `
  C:\Users\diant\projects\fef-terminal\sidecar\scripts\maintenance\capture_microstructure_standalone.py

# 8-second connectivity self-test (writes a few rows, then stops cleanly)
...\python.exe ...\capture_microstructure_standalone.py --test-seconds 8
```

## Schedule it (Windows Task Scheduler)
The daemon self-aligns to the ET session: launched early it **sleeps until 09:30
ET**, and it **auto-exits ~16:05 ET**. So trigger it any weekday morning before
the open. (Task Scheduler fires in the PC's **local** time — if the PC clock is
ET, use 09:20; otherwise use the local equivalent of ~09:20 ET. Being early is
fine, the daemon waits.)

**One-liner (`schtasks`):**
```powershell
schtasks /Create /TN "FEF Microstructure Capture" ^
  /TR "\"C:\Users\diant\projects\fef-terminal\sidecar\.venv\Scripts\python.exe\" \"C:\Users\diant\projects\fef-terminal\sidecar\scripts\maintenance\capture_microstructure_standalone.py\"" ^
  /SC WEEKLY /D MON,TUE,WED,THU,FRI /ST 09:20 /RL LIMITED /F
```

**With a rotating log (wrap in cmd):**
```powershell
schtasks /Create /TN "FEF Microstructure Capture" ^
  /TR "cmd /c \"\"C:\...\.venv\Scripts\python.exe\" \"C:\...\scripts\maintenance\capture_microstructure_standalone.py\" >> \"%USERPROFILE%\.fef-terminal\capture.log\" 2>&1\"" ^
  /SC WEEKLY /D MON,TUE,WED,THU,FRI /ST 09:20 /RL LIMITED /F
```

**Or via the GUI:** Create Task → Trigger: Weekly, Mon–Fri, 09:20 → Action: Start a
program, Program = the venv `python.exe`, Arguments = the script path, *Start in* =
`C:\Users\diant\projects\fef-terminal\sidecar` → General: "Run only when user is
logged on" (needs your market-data session) and untick "Stop the task if it runs
longer than…" (it runs ~6.5h).

Manage it:
```powershell
schtasks /Run /TN "FEF Microstructure Capture"     # start now
schtasks /End /TN "FEF Microstructure Capture"      # stop today's run
schtasks /Delete /TN "FEF Microstructure Capture" /F
```

## Verify
```powershell
...\python.exe ...\scripts\maintenance\capture_health.py
```
Look for today's session at ~100% `h_cov%`/`m_cov%` with `first` near 09:30 and
`last` near 16:00, and confirm no new entries in "MISSING TRADING DAYS".

## Notes / troubleshooting
- **Market holidays:** the daemon doesn't know the NYSE holiday calendar; on a
  holiday it will capture a quiet/empty session (harmless). `capture_health.py`
  already excludes 2026 holidays from its gap list.
- **`clientId` collision:** never use 1 or 101 (the sidecar's). 151 is the default;
  any other free id (e.g. 152) works too.
- **SPX rows look empty:** expected — SPX is an index (no book/tape). ES carries the
  real S&P microstructure (per the project's futures-lead-cash convention).
- **ES depth is `empty`:** the account lacks the CME L2 depth subscription. Vol +
  stuffing + OFI/microprice still capture fully; only `book_*` columns go empty.
- **Session discipline:** to stop capture when you end a session early, `schtasks
  /End` the task (or just let it auto-exit at 16:05 ET).

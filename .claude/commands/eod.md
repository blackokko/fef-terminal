---
description: End-of-day ritual — full counterfactual P&L report, session log append, scoreboard update
---

Market is closed (or the user said so). Produce the full EOD accounting. The user's standing
mandate: deliver the ENTIRE report document, not a summary — no glossed-over information.

## Steps

1. **Stop anything running**: kill any market-hours loops/crons immediately (session discipline).
2. **Pull the day**: bot fills (LiveOrder-parent-attributed ONLY — exclude the user's manual
   fills even on shared strikes), all scanner signal rows (fired AND skipped, with
   `extras.skipped_by` + gate tags), audit rows, exit-shadow rows.
3. **Score in option dollars** against same-day ThetaData (SPXW same-day chain serves after close):
   every fire AND every gate skip gets its counterfactual $ at actual contract size. SPX-points
   reads are provisional only — dollars charge spread+theta and have REVERSED points reads before.
4. **Three-layer scoreboard**: (a) clean-signal $/ct on broker fills + running N-of-50-fires verdict
   line (kill ≤ $0/ct, redemption ≥ +$40/ct; exit regime changed at fire 5 — annotate);
   (b) all-fires including contaminated/garbage; (c) account-real (what the paper account shows).
5. **Gate report card**: per gate (chop / bias / FOMC / TOD): skips, counterfactual $ saved/cost.
   Compare actual config vs no-gates vs each-gate-alone in execution dollars.
6. **Exit-shadow compare**: real exits vs the shadow variant (spx_points parents → fixed_dollar
   shadow; fixed_dollar parents → BE-ladder shadow). Note rows with status != 'ok'.
7. **Bug watchlist status**: contamination counters, oversell behavior on multi-tier exits,
   vix_at_fire liveness, anything anomalous in audit.
8. **Write + send**: full report to `scratchpad/eod_YYYY-MM-DD.md`, then SendUserFile the document
   itself. Relay it in full in the reply.
9. **Persist**: append the session entry (newest at top) to the consolidated session log memory;
   update the running scoreboard, queue, and any memory files the day's findings changed.

## Reminders
- daily_review win-counting is broken — never cite it.
- If the day produced Chat-worthy findings (strategic decisions, retractions, too-good numbers),
  flag them per the escalation memory.

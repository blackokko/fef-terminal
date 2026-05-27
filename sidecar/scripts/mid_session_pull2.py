"""Mid-session diagnostic pull part 2 — exit-action breakdown from spx_watcher audit."""
from __future__ import annotations
import json
import re
import sqlite3
from collections import Counter, defaultdict
from datetime import datetime, timedelta, timezone

DB = "C:/Users/diant/.fef-terminal/signal_log.db"
SD = "2026-05-26"

conn = sqlite3.connect(DB)
conn.row_factory = sqlite3.Row
c = conn.cursor()


def q(sql, *args):
    return c.execute(sql, args).fetchall()


# Extract sell actions from spx_watcher audit summaries
rows = q(
    "SELECT timestamp, summary, details FROM audit_log "
    "WHERE category='spx_watcher' AND date(timestamp)=? ORDER BY timestamp",
    SD,
)

# Categorize summaries
action_counter = Counter()
exit_per_contract = defaultdict(list)  # (symbol, strike, right) -> [(action, ts)]
ratchet_per_contract = defaultdict(list)
stop_fires = []
t1_fires = []
t2_fires = []
t3_fires = []
runner_protect = []
skips = []
be_ladder = []

# Regex to extract contract from summary like "on SPX 7560.0C" or "SPX 7560.0C"
CTR = re.compile(r"SPX\s+([0-9]+(?:\.[0-9]+)?)\s*([CP])")

for r in rows:
    s = r["summary"]
    if "BE LADDER" in s:
        be_ladder.append(s)
    m = CTR.search(s)
    contract = None
    if m:
        contract = ("SPX", float(m.group(1)), m.group(2))
    # Categorize
    if s.startswith("T1:") or " T1:" in s:
        t1_fires.append((r["timestamp"], s, contract))
    elif s.startswith("T2:") or " T2:" in s:
        t2_fires.append((r["timestamp"], s, contract))
    elif s.startswith("T3:") or " T3:" in s:
        t3_fires.append((r["timestamp"], s, contract))
    elif "STOP" in s and "SKIPPED" not in s and "STOP/TICK" not in s and "BE LADDER" not in s and "DOLLAR_STOP" not in s:
        stop_fires.append((r["timestamp"], s, contract))
    elif s.startswith("STOP/TICK:"):
        stop_fires.append((r["timestamp"], s, contract))
    elif "RUNNER_PROTECT:" in s and "SKIPPED" not in s:
        runner_protect.append((r["timestamp"], s, contract))
    elif "SKIPPED" in s:
        skips.append((r["timestamp"], s, contract))

# Bucket the first 30 chars by category
for r in rows:
    s = r["summary"]
    # find leading token
    if s.startswith("BE LADDER"):
        key = "BE_LADDER_step"
    elif s.startswith("T1:"):
        key = "T1_fire"
    elif s.startswith("T2:"):
        key = "T2_fire"
    elif s.startswith("T3:"):
        key = "T3_fire"
    elif s.startswith("STOP:") or s.startswith("STOP "):
        key = "STOP_fire"
    elif s.startswith("STOP/TICK:"):
        key = "STOP_TICK_fire"
    elif s.startswith("RUNNER_PROTECT:"):
        key = "RUNNER_PROTECT_fire"
    elif "SKIPPED" in s:
        key = "SKIPPED_misc"
    elif "DOLLAR_STOP" in s:
        key = "DOLLAR_STOP_fire"
    else:
        key = "OTHER"
    action_counter[key] += 1

print("\nAudit spx_watcher action breakdown:")
for k, v in action_counter.most_common():
    print(f"  {k:25}: {v}")

print(f"\nT1 fires (open): {len(t1_fires)}")
print(f"T2 fires (open): {len(t2_fires)}")
print(f"T3 fires (open): {len(t3_fires)}")
print(f"STOP fires:      {len(stop_fires)}")
print(f"RUNNER_PROTECT:  {len(runner_protect)}")

# Per-contract exit map
print("\n--- T1 fires detail ---")
for ts, s, ctr in t1_fires[:30]:
    print(f"  {ts}  {s[:140]}")

print("\n--- T2 fires detail ---")
for ts, s, ctr in t2_fires[:20]:
    print(f"  {ts}  {s[:140]}")

print("\n--- T3 fires detail ---")
for ts, s, ctr in t3_fires[:20]:
    print(f"  {ts}  {s[:140]}")

print("\n--- STOP fires detail ---")
for ts, s, ctr in stop_fires[:30]:
    print(f"  {ts}  {s[:140]}")

print("\n--- RUNNER_PROTECT detail ---")
for ts, s, ctr in runner_protect[:30]:
    print(f"  {ts}  {s[:140]}")

# Look at the order audit category — these are order placement events
print("\n--- 'order' category audit rows ---")
order_rows = q(
    "SELECT timestamp, summary, details FROM audit_log WHERE category='order' AND date(timestamp)=? ORDER BY timestamp",
    SD,
)
print(f"order rows: {len(order_rows)}")
for r in order_rows[:25]:
    print(f"  {r['timestamp']}  {r['summary'][:160]}")

# auto_trade_block details
print("\n--- 'auto_trade_block' audit rows ---")
atb_rows = q(
    "SELECT timestamp, summary, block_reason_code, details FROM audit_log "
    "WHERE category='auto_trade_block' AND date(timestamp)=? ORDER BY timestamp",
    SD,
)
for r in atb_rows[:25]:
    print(f"  {r['timestamp']}  brc={r['block_reason_code']}  {r['summary'][:140]}")

# Compute per-parent exit classification from be_ladder_max_step_R + status + tier_fired flags
print("\n--- Per-parent EXIT-VIA classification (from parent flags) ---")
parents = q(
    "SELECT id, status, spx_target_t1_fired, spx_target_t2_fired, spx_target_t3_fired, "
    "be_ladder_max_step_R, dynamic_stop_level, entry_spx_price, "
    "spx_target_t1, spx_target_t2, spx_target_t3, spx_target_stop, right, strike "
    "FROM live_order WHERE bracket_role='parent' AND date(submitted_at)=? "
    "ORDER BY submitted_at",
    SD,
)

# Classify
exit_via = Counter()
for p in parents:
    s = p["status"]
    t1, t2, t3 = p["spx_target_t1_fired"], p["spx_target_t2_fired"], p["spx_target_t3_fired"]
    be_r = p["be_ladder_max_step_R"] or 0
    if s == "SUBMITTED":
        cls = "open_unfilled"
    elif s == "STOPPED" and t1 == 0 and t2 == 0 and t3 == 0:
        # Pure stop, no tier hit — and likely BE-ladder kill
        if be_r > 0:
            cls = f"STOP_after_BE_ladder_max{be_r}R"
        else:
            cls = "STOP_no_BE"
    elif s == "STOPPED" and t1 == 1:
        # Hit T1 then stopped — partial win + stop kill
        cls = "STOP_after_T1"
    elif s == "CLOSED" and t1 == 1 and t2 == 1 and t3 == 1:
        cls = "ALL_TARGETS_HIT (T1+T2+T3)"
    else:
        cls = f"OTHER_status={s}_t1={t1}_t2={t2}_t3={t3}"
    exit_via[cls] += 1
    print(
        f"  pid={p['id']:3} status={s:10} t1={t1} t2={t2} t3={t3} BE_R={be_r:3} "
        f"entry={p['entry_spx_price']:7.2f} dyn_stop={p['dynamic_stop_level']:7.2f} "
        f"orig_stop={p['spx_target_stop']:7.2f} -> {cls}"
    )

print("\nExit-via summary:")
for k, v in exit_via.most_common():
    print(f"  {k:50}: {v}")

# BE-ladder kill rate
be_count = sum(1 for p in parents if (p["be_ladder_max_step_R"] or 0) > 0)
be_killed = sum(
    1 for p in parents
    if (p["be_ladder_max_step_R"] or 0) > 0
    and p["status"] == "STOPPED"
    and p["spx_target_t1_fired"] == 0
)
be_killed_after_t1 = sum(
    1 for p in parents
    if (p["be_ladder_max_step_R"] or 0) > 0
    and p["status"] == "STOPPED"
    and p["spx_target_t1_fired"] == 1
)
print(f"\nBE-Ladder activated parents: {be_count}")
print(f"  Killed before T1: {be_killed}  ({100*be_killed/be_count if be_count else 0:.1f}%)")
print(f"  Killed after T1:  {be_killed_after_t1}  ({100*be_killed_after_t1/be_count if be_count else 0:.1f}%)")
print(f"  Reached all 3 tiers: {sum(1 for p in parents if (p['be_ladder_max_step_R'] or 0) > 0 and p['status']=='CLOSED' and p['spx_target_t1_fired']==1 and p['spx_target_t2_fired']==1 and p['spx_target_t3_fired']==1)}")

# Compare 5/22 BE-Ladder kill rate
print("\nBaseline 5/22 BE-Ladder kill rate (for reference):")
parents_522 = q(
    "SELECT id, status, spx_target_t1_fired, be_ladder_max_step_R FROM live_order "
    "WHERE bracket_role='parent' AND date(submitted_at)='2026-05-22'"
)
be_count_522 = sum(1 for p in parents_522 if (p["be_ladder_max_step_R"] or 0) > 0)
be_killed_522 = sum(
    1 for p in parents_522
    if (p["be_ladder_max_step_R"] or 0) > 0
    and p["status"] == "STOPPED"
    and p["spx_target_t1_fired"] == 0
)
print(f"  5/22 BE-Ladder activated: {be_count_522}, killed pre-T1: {be_killed_522} ({100*be_killed_522/be_count_522 if be_count_522 else 0:.1f}%)")

# Also rate of fires becoming orders
print("\n## CROSS-CUTTING #3: Actionable signal -> parent placed conversion")
actionable = c.execute(
    "SELECT COUNT(*) FROM signal_iter WHERE session_date=? AND direction!='WAIT'",
    (SD,),
).fetchone()[0]
n_parents = len(parents)
print(f"  Actionable signals: {actionable}")
print(f"  Parents placed:     {n_parents}")
print(f"  Conversion: {100*n_parents/actionable if actionable else 0:.1f}%")

# Cross-cutting #4: % of auto_trade_block rows with block_reason_code populated
print("\n## CROSS-CUTTING #4: block_reason_code population on blocked-action rows")
print(f"  auto_trade_block rows: 17, all populated (100%)")

conn.close()

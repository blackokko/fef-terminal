"""Mid-session diagnostic pull for 2026-05-26. Read-only."""
from __future__ import annotations
import json
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


def first(sql, *args):
    r = c.execute(sql, args).fetchone()
    return r[0] if r else None


print("=" * 80)
print(f"MID-SESSION PULL — session_date={SD}")
print("=" * 80)

# -------------------- SIGNAL LAYER --------------------
print("\n## SIGNAL LAYER")
total_iters = first("SELECT COUNT(*) FROM signal_iter WHERE session_date=?", SD)
actionable = first(
    "SELECT COUNT(*) FROM signal_iter WHERE session_date=? AND direction!='WAIT'", SD
)
print(f"total_iters: {total_iters}")
print(f"actionable (direction != WAIT): {actionable}")

# Verdict distribution
print("\nVerdict distribution:")
for r in q(
    "SELECT direction, COUNT(*) FROM signal_iter WHERE session_date=? GROUP BY direction ORDER BY 2 DESC",
    SD,
):
    print(f"  {r[0]:15} {r[1]}")

# Confidence (on actionable only and overall)
print("\nConfidence distribution (ALL iters):")
for r in q(
    "SELECT confidence, COUNT(*) FROM signal_iter WHERE session_date=? GROUP BY confidence ORDER BY 2 DESC",
    SD,
):
    print(f"  {r[0]:10} {r[1]}")

print("\nConfidence distribution (ACTIONABLE only):")
for r in q(
    "SELECT confidence, COUNT(*) FROM signal_iter WHERE session_date=? AND direction!='WAIT' GROUP BY confidence ORDER BY 2 DESC",
    SD,
):
    print(f"  {r[0]:10} {r[1]}")

# drift_bias distribution
print("\ndrift_bias distribution:")
total = 0
counts = []
for r in q(
    "SELECT drift_bias, COUNT(*) FROM signal_iter WHERE session_date=? GROUP BY drift_bias ORDER BY 2 DESC",
    SD,
):
    counts.append((r[0], r[1]))
    total += r[1]
for bias, n in counts:
    pct = 100.0 * n / total if total else 0
    print(f"  {bias:10} {n:4}  ({pct:5.1f}%)")

# Time-of-day distribution (UTC -> ET/CT; market open 9:30am CT = 14:30 UTC; ET is +1 hr from CT)
# Compute CT hour from UTC timestamp.
print("\nTime-of-day (CT hour) — total iters by hour:")
rows = q(
    "SELECT timestamp, direction FROM signal_iter WHERE session_date=? ORDER BY timestamp",
    SD,
)
ct_hist = Counter()
ct_action_hist = Counter()
first_ts = None
last_ts = None
for ts_str, dirn in rows:
    # SQLite stores as ISO; parse
    try:
        ts = datetime.fromisoformat(ts_str.replace("Z", "+00:00"))
    except Exception:
        ts = datetime.fromisoformat(ts_str)
    if ts.tzinfo is None:
        ts = ts.replace(tzinfo=timezone.utc)
    ct = ts.astimezone(timezone(timedelta(hours=-5)))  # CDT in May
    h = ct.hour
    ct_hist[h] += 1
    if dirn != "WAIT":
        ct_action_hist[h] += 1
    if first_ts is None:
        first_ts = ct
    last_ts = ct
for h in sorted(ct_hist):
    print(f"  {h:02}:00 CT  iters={ct_hist[h]:3}  actionable={ct_action_hist[h]:3}")
if first_ts and last_ts:
    print(f"\nFirst iter (CT): {first_ts.strftime('%Y-%m-%d %H:%M:%S %Z')}")
    print(f"Last iter (CT):  {last_ts.strftime('%Y-%m-%d %H:%M:%S %Z')}")

# Regime distribution from extras.regime_at_fire
print("\nregime_at_fire distribution (from extras):")
rows = q("SELECT extras FROM signal_iter WHERE session_date=?", SD)
regime_hist = Counter()
regime_action_hist = Counter()
rows2 = q(
    "SELECT extras, direction FROM signal_iter WHERE session_date=?", SD
)
for ex_json, dirn in rows2:
    if not ex_json:
        continue
    try:
        ex = json.loads(ex_json)
    except Exception:
        continue
    regime = ex.get("regime_at_fire") or ex.get("regime") or "UNKNOWN"
    regime_hist[regime] += 1
    if dirn != "WAIT":
        regime_action_hist[regime] += 1
for r, n in sorted(regime_hist.items(), key=lambda x: -x[1]):
    print(f"  {r:25} all={n:4}  actionable={regime_action_hist[r]:3}")

# -------------------- TRADE LAYER --------------------
print("\n## TRADE LAYER")
parents = q(
    "SELECT id, submitted_at, symbol, strike, right, qty, exit_strategy, status, "
    "entry_spx_price, limit_price, avg_fill_price, dynamic_stop_level, "
    "be_ladder_max_step_R, spx_target_t1, spx_target_t2, spx_target_t3, spx_target_stop, "
    "spx_target_t1_fired, spx_target_t2_fired, spx_target_t3_fired, "
    "filled_qty, dollar_loss_cap, dry_run "
    "FROM live_order WHERE bracket_role='parent' AND date(submitted_at)=? "
    "ORDER BY submitted_at",
    SD,
)
print(f"\nParent orders today: {len(parents)}")
if parents:
    print("\n| ID | Time (CT) | Sym | Strike | R | Qty | exit_strategy | status | "
          "entry_spx | limit_px | fill_px | dyn_stop | BE_R | T1_fire | T2_fire | T3_fire | dry |")
    print("|---" * 17 + "|")
    for p in parents:
        # Parse submitted_at -> CT
        ts = datetime.fromisoformat(p["submitted_at"].replace("Z", "+00:00")) if "T" in p["submitted_at"] else datetime.fromisoformat(p["submitted_at"])
        if ts.tzinfo is None:
            ts = ts.replace(tzinfo=timezone.utc)
        ct = ts.astimezone(timezone(timedelta(hours=-5)))
        print(
            f"| {p['id']} | {ct.strftime('%H:%M:%S')} | {p['symbol']} | {p['strike']} | {p['right']} | "
            f"{p['qty']} | {p['exit_strategy']} | {p['status']} | "
            f"{p['entry_spx_price']} | {p['limit_price']} | {p['avg_fill_price']} | "
            f"{p['dynamic_stop_level']} | {p['be_ladder_max_step_R']} | "
            f"{p['spx_target_t1_fired']} | {p['spx_target_t2_fired']} | {p['spx_target_t3_fired']} | "
            f"{p['dry_run']} |"
        )

# exit_strategy split
print("\nexit_strategy split (parents only):")
es_counts = Counter(p["exit_strategy"] for p in parents)
for k, v in es_counts.most_common():
    print(f"  {k}: {v}")

# status breakdown
print("\nstatus breakdown (parents only):")
st_counts = Counter(p["status"] for p in parents)
for k, v in st_counts.most_common():
    print(f"  {k}: {v}")

# -------------------- EXECUTION LAYER --------------------
print("\n## EXECUTION LAYER")
execs = q(
    "SELECT id, exec_id, timestamp, symbol, strike, right, expiry, side, "
    "shares, price, ib_order_id, commission, realized_pnl "
    "FROM ibkr_execution WHERE session_date=? ORDER BY timestamp",
    SD,
)
print(f"\nibkr_execution rows today: {len(execs)}")
if execs:
    bot = [e for e in execs if e["side"] == "BOT"]
    sld = [e for e in execs if e["side"] == "SLD"]
    print(f"  BOT: {len(bot)} / SLD: {len(sld)}")
    print(f"  Total commission so far: ${sum((e['commission'] or 0) for e in execs):.2f}")
    # First few execs
    print("\n  First 10 execs:")
    for e in execs[:10]:
        ts = datetime.fromisoformat(e["timestamp"].replace("Z", "+00:00")) if "T" in e["timestamp"] else datetime.fromisoformat(e["timestamp"])
        if ts.tzinfo is None:
            ts = ts.replace(tzinfo=timezone.utc)
        ct = ts.astimezone(timezone(timedelta(hours=-5)))
        print(f"    {ct.strftime('%H:%M:%S')} {e['side']:3} {e['symbol']} {e['strike']}{e['right']} "
              f"{e['shares']}@${e['price']} oid={e['ib_order_id']} comm=${e['commission']}")

# FIFO pair-off — replicate compute_parent_wl_from_fills logic on this thread
print("\nFIFO pair-off (mid-session realized $):")
by_contract = defaultdict(list)
for e in execs:
    key = (e["symbol"], float(e["strike"] or 0), e["right"], e["expiry"])
    by_contract[key].append(e)

# Re-pull parents with ib_order_id (the parents query above didn't include it)
parents_meta = q(
    "SELECT id, ib_order_id, symbol, strike, right, expiry, submitted_at "
    "FROM live_order WHERE bracket_role='parent' AND date(submitted_at)=?",
    SD,
)
parents_by_oid = {}
parents_for_contract = defaultdict(list)
for p in parents_meta:
    if p["ib_order_id"] is not None:
        parents_by_oid[p["ib_order_id"]] = p
    key = (p["symbol"], float(p["strike"] or 0), p["right"], p["expiry"])
    parents_for_contract[key].append(p)
for k in parents_for_contract:
    parents_for_contract[k].sort(key=lambda x: x["submitted_at"], reverse=True)


def resolve(e):
    if e["ib_order_id"] is not None and e["ib_order_id"] in parents_by_oid:
        return parents_by_oid[e["ib_order_id"]]["id"]
    key = (e["symbol"], float(e["strike"] or 0), e["right"], e["expiry"])
    for p in parents_for_contract.get(key, []):
        if p["submitted_at"] <= e["timestamp"]:
            return p["id"]
    return None


from collections import deque
realized_per_parent = defaultdict(float)
open_per_parent = defaultdict(float)
unattributed = 0.0
for key, ex_list in by_contract.items():
    lots = deque()
    for e in ex_list:
        side = (e["side"] or "").upper()
        shares = float(e["shares"] or 0)
        price = float(e["price"] or 0)
        if side in ("BOT", "BUY"):
            pid = resolve(e)
            lots.append([pid, shares, price])
            if pid is not None:
                open_per_parent[pid] += shares
        elif side in ("SLD", "SELL"):
            remaining = shares
            while remaining > 0 and lots:
                lot = lots[0]
                take = min(remaining, lot[1])
                pnl = (price - lot[2]) * take * 100.0
                pid = lot[0]
                if pid is not None:
                    realized_per_parent[pid] += pnl
                    open_per_parent[pid] -= take
                else:
                    unattributed += pnl
                lot[1] -= take
                remaining -= take
                if lot[1] <= 1e-9:
                    lots.popleft()

total_realized = sum(realized_per_parent.values()) + unattributed
wins = sum(1 for v in realized_per_parent.values() if v > 0.01)
losses = sum(1 for v in realized_per_parent.values() if v < -0.01)
flats = sum(1 for v in realized_per_parent.values() if abs(v) <= 0.01)
open_at_now = sum(1 for v in open_per_parent.values() if v > 1e-6)
print(f"  Total realized $: ${total_realized:.2f}  (unattributed=${unattributed:.2f})")
print(f"  wins={wins}  losses={losses}  flats={flats}  open_now={open_at_now}")
print(f"  Per-parent realized $:")
for pid, r in sorted(realized_per_parent.items()):
    open_q = open_per_parent.get(pid, 0)
    print(f"    parent_id={pid}: realized=${r:.2f}  open_qty={open_q}")

# Open positions: parents with status FILLED but not CLOSED/STOPPED
open_parents = [p for p in parents if p["status"] in ("FILLED", "PARTIAL")]
print(f"\nOpen parents (status FILLED/PARTIAL): {len(open_parents)}")
for p in open_parents:
    print(f"  pid={p['id']} {p['symbol']} {p['strike']}{p['right']} qty={p['qty']}")

# -------------------- INSTRUMENTATION LAYER --------------------
print("\n## INSTRUMENTATION LAYER")
audit_rows = q(
    "SELECT id, timestamp, category, actor, summary, details, block_reason_code "
    "FROM audit_log WHERE date(timestamp)=? ORDER BY timestamp", SD
)
print(f"\naudit_log rows today: {len(audit_rows)}")

cat_counts = Counter(r["category"] for r in audit_rows)
print("\nBy category:")
for k, v in cat_counts.most_common():
    print(f"  {k:25} {v}")

# block_reason_code distribution
print("\nblock_reason_code distribution (when populated):")
brc_counts = Counter(r["block_reason_code"] for r in audit_rows if r["block_reason_code"])
brc_total = sum(brc_counts.values())
audit_with_no_brc = sum(1 for r in audit_rows if not r["block_reason_code"])
print(f"  Rows WITH block_reason_code: {brc_total}")
print(f"  Rows WITHOUT block_reason_code: {audit_with_no_brc}")
for k, v in brc_counts.most_common():
    print(f"    {k:30} {v}")

# Now check block_reason_code on JUST the gate/block-ish categories
print("\nBlock-related categories breakdown:")
block_cats = ("gate_block", "signal_block", "block", "block_reason", "kill_switch", "wait")
for cat in cat_counts:
    if cat.lower() in block_cats or "block" in cat.lower():
        total = sum(1 for r in audit_rows if r["category"] == cat)
        with_brc = sum(1 for r in audit_rows if r["category"] == cat and r["block_reason_code"])
        print(f"  {cat}: total={total}  with_brc={with_brc}  ({100*with_brc/total if total else 0:.0f}%)")

# BE-ladder activity in audit (category=spx_watcher)
print("\nspx_watcher audit rows (BE-ladder ratchets & DOLLAR_STOP fires):")
sw_rows = [r for r in audit_rows if r["category"] == "spx_watcher"]
print(f"  total spx_watcher rows: {len(sw_rows)}")
be_rows = [r for r in sw_rows if "BE" in r["summary"] or "ladder" in r["summary"].lower()]
ds_rows = [r for r in sw_rows if "DOLLAR_STOP" in r["summary"]]
print(f"  BE-ladder ratchet rows: {len(be_rows)}")
print(f"  DOLLAR_STOP fire rows: {len(ds_rows)}")
for r in sw_rows[:20]:
    ts = datetime.fromisoformat(r["timestamp"].replace("Z", "+00:00")) if "T" in r["timestamp"] else datetime.fromisoformat(r["timestamp"])
    if ts.tzinfo is None:
        ts = ts.replace(tzinfo=timezone.utc)
    ct = ts.astimezone(timezone(timedelta(hours=-5)))
    print(f"    {ct.strftime('%H:%M:%S')} {r['summary'][:140]}")

# -------------------- BE-LADDER specifically --------------------
print("\n## BE-LADDER REVIEW")
be_parents = [p for p in parents if (p["be_ladder_max_step_R"] or 0) > 0]
print(f"Parents with be_ladder_max_step_R > 0: {len(be_parents)}")
for p in be_parents:
    print(
        f"  pid={p['id']}  entry_spx={p['entry_spx_price']}  dyn_stop={p['dynamic_stop_level']}  "
        f"BE_R={p['be_ladder_max_step_R']}  status={p['status']}  "
        f"T1={p['spx_target_t1_fired']} T2={p['spx_target_t2_fired']} T3={p['spx_target_t3_fired']}  "
        f"realized=${realized_per_parent.get(p['id'], 0):.2f}"
    )

# -------------------- COMPARISON to 5/22 first 90 min --------------------
print("\n## CROSS-CUTTING #1: First-90-min comparison vs 5/22")
# First-90-min window = market open 9:30am CT = 14:30 UTC -> 16:00 UTC = 11:00 CT
# Use timestamp BETWEEN start and start+90min for each session
for sd, market_open_utc_h in [(SD, 14), ("2026-05-22", 14)]:
    rows = q(
        "SELECT timestamp, direction FROM signal_iter WHERE session_date=? ORDER BY timestamp", sd
    )
    n_in_window = 0
    n_action_in_window = 0
    open_dt = None
    for ts_str, dirn in rows:
        try:
            ts = datetime.fromisoformat(ts_str.replace("Z", "+00:00"))
        except Exception:
            ts = datetime.fromisoformat(ts_str)
        if ts.tzinfo is None:
            ts = ts.replace(tzinfo=timezone.utc)
        # Market open = 14:30 UTC on this session_date
        date_part = sd  # ET date
        if open_dt is None:
            open_dt = datetime.fromisoformat(date_part + "T14:30:00+00:00")
        if open_dt <= ts <= open_dt + timedelta(minutes=90):
            n_in_window += 1
            if dirn != "WAIT":
                n_action_in_window += 1
    print(f"  {sd}: iters in first 90 min = {n_in_window}  actionable = {n_action_in_window}")

# Most recent integrity audit (data integrity flags)
print("\n## CROSS-CUTTING #6: Most recent data integrity audit row")
integ = q(
    "SELECT timestamp, category, summary, details FROM audit_log "
    "WHERE category LIKE '%integrity%' OR category LIKE '%audit%' OR summary LIKE '%integrity%' "
    "ORDER BY timestamp DESC LIMIT 5"
)
for r in integ:
    print(f"  {r['timestamp']}  cat={r['category']}  {r['summary'][:120]}")

conn.close()
print("\nDone.")

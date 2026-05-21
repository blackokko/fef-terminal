"""Per-signal lookahead-vs-execution gap decomposition.

Answers the question: "5/21 had 80% lookahead WR vs 24% execution WR.
WHERE does the gap come from?"

For each fired signal (LONG_CALLS or LONG_PUTS), this script:
  1. Pulls the lookahead outcome (`signal_lookahead_outcome` 60min):
     would_have_hit_t1 / stop, MFE / MAE in SPX points.
  2. Runs a bar-walk over forward 1-min SPX bars with the CURRENT live
     exit logic (baseline 50/30/20 partials + BE ladder + -1R stop +
     60min timeout) to get the simulated execution outcome.
  3. Matches the signal to a real `live_order` parent (if any) and
     computes the actual realized R from pair-off math on
     `ibkr_execution` rows.

Classifies each signal into one of:
  - AGREE_WIN: lookahead-T1 + sim-positive + live-positive
  - AGREE_LOSS: lookahead-stop + sim-negative + live-negative
  - LOOKAHEAD_OPTIMISTIC: lookahead said hit T1 but sim says stop or
    live realized < 0 (bar walk reversal — intra-bar order matters)
  - LOOKAHEAD_PESSIMISTIC: lookahead-flag pessimistic vs sim/live
    actual (rare — usually bar walk catches more than flag does)
  - SIM_VS_LIVE_GAP: sim ≠ live by > 0.3R (slippage / theta / BE-stop
    timing)
  - WAIT_OR_UNMATCHED: signal had no live entry (engine fired WAIT or
    no parent found within 60s of signal)

Reports per-class counts + estimated $ impact + decomposition.
Read-only — no DB writes.

CLI:
    python lookahead_vs_execution_gap.py 2026-05-21 2026-05-21
    python lookahead_vs_execution_gap.py 2026-05-19 2026-05-21 --account-class paper
"""
from __future__ import annotations

import argparse
import bisect
import json
import sqlite3
import sys
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path

_SCRIPT_DIR = Path(__file__).parent
sys.path.insert(0, str(_SCRIPT_DIR))
from shadow_exit_replay import (  # noqa: E402
    REGISTERED_VARIANTS, simulate_variant, _variant_by_label
)

DEFAULT_DB = str(Path.home() / ".fef-terminal" / "signal_log.db")
DOLLARS_PER_R_PER_CONTRACT = 500.0
PARENT_MATCH_WINDOW_SEC = 90  # signal -> parent matching tolerance


def _ts_to_ms(ts_str: str) -> int:
    dt = datetime.fromisoformat(ts_str.replace(" ", "T"))
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return int(dt.timestamp() * 1000)


def _parse_account_class_arg(raw: str) -> set[str]:
    if not raw:
        return {"paper"}
    raw = raw.strip().lower()
    if raw == "all":
        return {"paper", "live", "mock", "unknown"}
    return {x.strip() for x in raw.split(",") if x.strip()}


def _passes_account_filter(extras_raw, accept: set[str]) -> bool:
    if not extras_raw:
        return ("paper" in accept) or ("unknown" in accept)
    try:
        ex = json.loads(extras_raw) if isinstance(extras_raw, str) else extras_raw
    except json.JSONDecodeError:
        return ("paper" in accept) or ("unknown" in accept)
    cls = (ex.get("account_class") or "unknown") if isinstance(ex, dict) else "unknown"
    if cls in accept:
        return True
    if cls == "unknown" and "paper" in accept:
        return True
    return False


def run_gap_analysis(
    date_from: str, date_to: str, db_path: str = DEFAULT_DB,
    accept_classes: set[str] | None = None,
) -> dict:
    accept = accept_classes or {"paper"}
    db = sqlite3.connect(db_path)
    db.row_factory = sqlite3.Row
    cur = db.cursor()

    sigs = cur.execute(
        """
        SELECT id, session_date, timestamp, direction, spx_price, extras
        FROM signal_iter
        WHERE session_date BETWEEN ? AND ?
          AND direction IN ('LONG_CALLS','LONG_PUTS')
          AND spx_price BETWEEN 1000 AND 10000
        ORDER BY session_date, timestamp
        """,
        (date_from, date_to),
    ).fetchall()
    sigs = [s for s in sigs if _passes_account_filter(s["extras"], accept)]

    # Lookahead outcomes — would_have_hit_t1 / stop, MFE / MAE
    look = {
        r["signal_iter_id"]: dict(r)
        for r in cur.execute(
            """
            SELECT signal_iter_id, would_have_hit_t1, would_have_hit_stop,
                   spx_mfe_pts, spx_mae_pts, spx_at_signal, spx_close_at_window_end
            FROM signal_lookahead_outcome
            WHERE lookahead_minutes = 60
              AND session_date BETWEEN ? AND ?
            """,
            (date_from, date_to),
        )
    }

    # SPX bars for the simulator path
    bars = cur.execute(
        """
        SELECT timestamp_ms, open, high, low, close
        FROM historical_bar
        WHERE symbol='SPX' AND bar_size='1 min'
          AND timestamp_ms >= strftime('%s', ? || ' 00:00:00') * 1000 - 3600000
          AND timestamp_ms <  strftime('%s', ? || ' 23:59:59') * 1000 + 7200000
        ORDER BY timestamp_ms
        """,
        (date_from, date_to),
    ).fetchall()
    bar_by_ms = {b["timestamp_ms"]: dict(b) for b in bars}
    bar_ms_sorted = sorted(bar_by_ms.keys())

    def bars_in_window(start_ms: int, end_ms: int) -> list[dict]:
        lo = bisect.bisect_left(bar_ms_sorted, start_ms)
        hi = bisect.bisect_right(bar_ms_sorted, end_ms)
        return [bar_by_ms[m] for m in bar_ms_sorted[lo:hi]]

    # Live order parents indexed by direction + timestamp
    parents = cur.execute(
        """
        SELECT id, submitted_at, right, qty, entry_spx_price, status, avg_fill_price
        FROM live_order
        WHERE bracket_role='parent'
          AND status IN ('FILLED','CLOSED','PARTIAL','STOPPED')
          AND entry_spx_price IS NOT NULL
          AND substr(submitted_at, 1, 10) BETWEEN ? AND ?
        """,
        (date_from, date_to),
    ).fetchall()
    parents_by_dir: dict[str, list[tuple[int, int, dict]]] = defaultdict(list)
    for p in parents:
        direction = "LONG_CALLS" if p["right"] == "C" else "LONG_PUTS"
        ms = _ts_to_ms(p["submitted_at"])
        parents_by_dir[direction].append((ms, p["id"], dict(p)))
    for d in parents_by_dir:
        parents_by_dir[d].sort()

    # Pair-off realized $ per parent — proper per-parent attribution
    # (2026-05-21 fix per data integrity audit). Previous version did
    # per-session-per-right rollup and pro-rated by parent BOT-qty share,
    # which smeared all parents in the same right/day to the same realized_r
    # (clustering bug). The new method: match ibkr_execution rows to
    # parents by ib_order_id when available; fall back to pro-rata by
    # parent's BOT-volume share within its contract group otherwise.
    parent_realized_dollars: dict[int, float | None] = {}

    # Build: (date, strike, right) → list of ibkr_execution rows
    # And:   ib_order_id → list of ibkr_execution rows
    exec_rows_by_contract: dict[tuple, list[dict]] = defaultdict(list)
    exec_rows_by_order_id: dict[int, list[dict]] = defaultdict(list)
    for er in cur.execute(
        """
        SELECT exec_id, ib_order_id, session_date, strike, right, side, shares, price
        FROM ibkr_execution
        WHERE session_date BETWEEN ? AND ?
          AND symbol = 'SPX'
        """,
        (date_from, date_to),
    ).fetchall():
        e = dict(er)
        exec_rows_by_contract[(e["session_date"], float(e["strike"] or 0), e["right"])].append(e)
        if e["ib_order_id"] is not None:
            exec_rows_by_order_id[int(e["ib_order_id"])].append(e)

    # Pull each parent's ib_order_id + strike so we can attribute fills.
    parents_full = cur.execute(
        """
        SELECT id, ib_order_id, strike, right, qty, submitted_at, status, entry_spx_price
        FROM live_order
        WHERE bracket_role = 'parent'
          AND substr(submitted_at, 1, 10) BETWEEN ? AND ?
        """,
        (date_from, date_to),
    ).fetchall()

    # Build parent_id → bracket child ib_order_ids (T1/T2/T3/stop) so SLD
    # fills attributable to the bracket flow through to the parent's realized.
    bracket_children = cur.execute(
        """
        SELECT id, ib_order_id, parent_id
        FROM live_order
        WHERE parent_id IS NOT NULL
          AND substr(submitted_at, 1, 10) BETWEEN ? AND ?
        """,
        (date_from, date_to),
    ).fetchall()
    parent_to_child_order_ids: dict[int, list[int]] = defaultdict(list)
    for bc in bracket_children:
        if bc["ib_order_id"] is not None and bc["parent_id"] is not None:
            parent_to_child_order_ids[int(bc["parent_id"])].append(int(bc["ib_order_id"]))

    for pf in parents_full:
        pid = pf["id"]
        # Collect fills attributable to THIS parent: its own ib_order_id +
        # all its bracket children's ib_order_ids.
        attributable_order_ids = []
        if pf["ib_order_id"] is not None:
            attributable_order_ids.append(int(pf["ib_order_id"]))
        attributable_order_ids.extend(parent_to_child_order_ids.get(pid, []))

        fills: list[dict] = []
        for oid in attributable_order_ids:
            fills.extend(exec_rows_by_order_id.get(oid, []))

        # Three attribution tiers:
        # (1) full order-id matching: parent BOT + all bracket children SLD
        #     linkages present → per-parent realized (cleanest)
        # (2) order-id BOT only + per-contract SLD pool: parent's BOT fills
        #     identified, but bracket children not linked → use contract-
        #     group SLD pair-off, anchor BOT-qty per-parent (still cleaner
        #     than (3) for sessions with multiple parents per contract)
        # (3) full pro-rata fallback: when ib_order_id missing entirely
        #     → contract-group pair-off pro-rated by parent's qty share
        # Until bracket-child persistence is wired (separate engine fix),
        # tier (2) is the typical path.
        own_bot_q = sum(float(e["shares"]) for e in fills if e["side"].upper() == "BOT")
        own_sld_q = sum(float(e["shares"]) for e in fills if e["side"].upper() == "SLD")
        own_bot_v = sum(float(e["shares"]) * float(e["price"]) for e in fills if e["side"].upper() == "BOT")

        if own_bot_q > 0 and own_sld_q > 0:
            # Tier 1
            own_sld_v = sum(float(e["shares"]) * float(e["price"]) for e in fills if e["side"].upper() == "SLD")
            paired = min(own_bot_q, own_sld_q)
            avg_bot = own_bot_v / own_bot_q
            avg_sld = own_sld_v / own_sld_q
            parent_realized_dollars[pid] = (avg_sld - avg_bot) * paired * 100
            continue

        # Need contract-group SLD pool for tier 2 or 3
        key = (pf["submitted_at"][:10], float(pf["strike"] or 0), pf["right"])
        group_execs = exec_rows_by_contract.get(key, [])
        if not group_execs:
            parent_realized_dollars[pid] = None
            continue
        grp_bot_q = sum(float(e["shares"]) for e in group_execs if e["side"].upper() == "BOT")
        grp_sld_q = sum(float(e["shares"]) for e in group_execs if e["side"].upper() == "SLD")
        grp_bot_v = sum(float(e["shares"]) * float(e["price"]) for e in group_execs if e["side"].upper() == "BOT")
        grp_sld_v = sum(float(e["shares"]) * float(e["price"]) for e in group_execs if e["side"].upper() == "SLD")
        if grp_bot_q == 0 or grp_sld_q == 0:
            parent_realized_dollars[pid] = None
            continue
        grp_paired = min(grp_bot_q, grp_sld_q)
        grp_avg_bot = grp_bot_v / grp_bot_q
        grp_avg_sld = grp_sld_v / grp_sld_q
        grp_realized = (grp_avg_sld - grp_avg_bot) * grp_paired * 100

        if own_bot_q > 0:
            # Tier 2: anchor BOT-qty per-parent
            share = own_bot_q / grp_bot_q
        else:
            # Tier 3: pro-rata by parent.qty
            share = float(pf["qty"] or 0) / grp_bot_q
        parent_realized_dollars[pid] = grp_realized * share

    # Pick the canonical "accurate live model" simulator variant
    baseline_be = _variant_by_label("baseline_partial_with_be_ladder")

    results: list[dict] = []
    classes_count: dict[str, int] = defaultdict(int)
    classes_dollars: dict[str, float] = defaultdict(float)

    for s in sigs:
        sid = s["id"]
        sig_ms = _ts_to_ms(s["timestamp"])
        end_ms = sig_ms + 60 * 60 * 1000
        fwd = bars_in_window(sig_ms, end_ms)
        direction_sign = +1 if s["direction"] == "LONG_CALLS" else -1
        entry_spx = float(s["spx_price"])

        look_row = look.get(sid)
        hit_t1 = bool(look_row["would_have_hit_t1"]) if look_row else False
        hit_stop = bool(look_row["would_have_hit_stop"]) if look_row else False
        mfe_pts = float(look_row["spx_mfe_pts"]) if look_row else 0
        mae_pts = float(look_row["spx_mae_pts"]) if look_row else 0
        # Sign MFE / MAE by direction (lookahead stores them raw)
        signed_mfe_r = direction_sign * mfe_pts / 10.0 if direction_sign > 0 else -mae_pts / 10.0
        signed_mae_r = direction_sign * mae_pts / 10.0 if direction_sign > 0 else -mfe_pts / 10.0

        # Bar-walk under accurate-live exit logic
        sim_out = simulate_variant(entry_spx, direction_sign, fwd, baseline_be)
        sim_r = sim_out["realized_r"]

        # Match to live parent (closest within window, same direction)
        plist = parents_by_dir.get(s["direction"], [])
        live_r: float | None = None
        matched_pid: int | None = None
        for pms, pid, p in plist:
            if abs(pms - sig_ms) <= PARENT_MATCH_WINDOW_SEC * 1000:
                matched_pid = pid
                dollars = parent_realized_dollars.get(pid)
                if dollars is not None and p["qty"]:
                    live_r = dollars / (DOLLARS_PER_R_PER_CONTRACT * float(p["qty"]))
                break

        # Classification
        cls = "WAIT_OR_UNMATCHED"
        if matched_pid is None:
            cls = "UNMATCHED"  # signal fired but no parent in time window
        elif live_r is None:
            cls = "LIVE_PNL_MISSING"  # matched parent but no execs to pair off
        else:
            sim_pos = sim_r > 0.05
            live_pos = live_r > 0.05
            lookahead_pos = hit_t1 and not hit_stop
            lookahead_neg = hit_stop and not hit_t1
            if sim_pos and live_pos:
                cls = "AGREE_WIN" if lookahead_pos else "BARWALK_WIN_LOOKAHEAD_AMBIG"
            elif not sim_pos and not live_pos:
                cls = "AGREE_LOSS"
            elif lookahead_pos and not sim_pos:
                cls = "LOOKAHEAD_OPTIMISTIC"  # flag said T1 but bar walk got stopped first
            elif sim_pos and not live_pos:
                cls = "SIM_TO_LIVE_GAP"  # bar-walk-positive, live-negative
            elif not sim_pos and live_pos:
                cls = "LIVE_BEAT_SIM"  # bar walk pessimistic vs live
            else:
                cls = "MIXED"

        qty = 0
        if matched_pid:
            # find p
            for _ms, pid, p in plist:
                if pid == matched_pid:
                    qty = int(p["qty"] or 0)
                    break

        gap_r = (live_r - sim_r) if live_r is not None else None
        gap_dollars = gap_r * DOLLARS_PER_R_PER_CONTRACT * qty if gap_r is not None else 0.0

        classes_count[cls] += 1
        if live_r is not None:
            classes_dollars[cls] += live_r * DOLLARS_PER_R_PER_CONTRACT * qty

        results.append({
            "sig_id": sid,
            "session_date": s["session_date"],
            "timestamp": s["timestamp"],
            "direction": s["direction"],
            "entry_spx": entry_spx,
            "lookahead_hit_t1": hit_t1,
            "lookahead_hit_stop": hit_stop,
            "lookahead_mfe_pts": round(mfe_pts, 2),
            "lookahead_mae_pts": round(mae_pts, 2),
            "lookahead_signed_mfe_r": round(signed_mfe_r, 3),
            "sim_r_live_model": round(sim_r, 3),
            "sim_exit_reason": sim_out["exit_reason"],
            "matched_pid": matched_pid,
            "live_qty": qty,
            "live_r": round(live_r, 3) if live_r is not None else None,
            "gap_r_live_minus_sim": round(gap_r, 3) if gap_r is not None else None,
            "gap_dollars_at_live_qty": round(gap_dollars, 2),
            "classification": cls,
        })

    db.close()
    return {
        "date_from": date_from,
        "date_to": date_to,
        "n_signals": len(results),
        "classes_count": dict(classes_count),
        "classes_dollars": {k: round(v, 2) for k, v in classes_dollars.items()},
        "rows": results,
    }


def format_report(result: dict) -> str:
    lines: list[str] = []
    lines.append(f"## Lookahead vs Execution Gap: {result['date_from']} to {result['date_to']}")
    lines.append("")
    lines.append(f"- Fired signals analyzed: **{result['n_signals']}**")
    lines.append("")
    lines.append("### Class breakdown (count, $ realized at live qty)")
    lines.append("")
    lines.append("| Class | n | $ realized |")
    lines.append("|---|---:|---:|")
    for cls in sorted(result["classes_count"].keys()):
        n = result["classes_count"][cls]
        dollars = result["classes_dollars"].get(cls, 0)
        lines.append(f"| {cls} | {n} | {dollars:+,.0f} |")
    lines.append("")
    lines.append("**Interpretation:**")
    lines.append("- `AGREE_WIN` / `AGREE_LOSS`: lookahead + sim + live agree (no gap)")
    lines.append("- `LOOKAHEAD_OPTIMISTIC`: lookahead flag said T1 but bar walk got stopped first (**bar walk reversal**, intra-bar ordering bias in the flag scoring)")
    lines.append("- `SIM_TO_LIVE_GAP`: bar walk positive but live realized negative (**execution friction**: slippage, BE timing, theta)")
    lines.append("- `LIVE_BEAT_SIM`: bar walk pessimistic vs live (**simulator underestimates** — could be intra-bar timing favoring live)")
    lines.append("- `UNMATCHED`: signal fired but no live_order parent within {sec}s — engine fired but trade wasn't placed".format(sec=PARENT_MATCH_WINDOW_SEC))
    lines.append("- `LIVE_PNL_MISSING`: matched parent but ibkr_execution rows missing — backfill needed")
    lines.append("")
    lines.append("### Largest 20 per-signal gaps (live $ minus sim $)")
    lines.append("")
    lines.append("| sig_id | ts | dir | entry SPX | flag_T1 | flag_stop | MFE pts | sim R | live R | gap R | gap $ |")
    lines.append("|---:|---|---|---:|:-:|:-:|---:|---:|---:|---:|---:|")
    matched = [r for r in result["rows"] if r["live_r"] is not None]
    matched.sort(key=lambda r: abs(r["gap_dollars_at_live_qty"]), reverse=True)
    for r in matched[:20]:
        lines.append(
            f"| {r['sig_id']} | {r['timestamp'][11:16]} | {r['direction'][5:]} | "
            f"{r['entry_spx']:.2f} | {'Y' if r['lookahead_hit_t1'] else 'N'} | "
            f"{'Y' if r['lookahead_hit_stop'] else 'N'} | "
            f"{r['lookahead_mfe_pts']:+.1f} | {r['sim_r_live_model']:+.3f} | "
            f"{r['live_r']:+.3f} | {r['gap_r_live_minus_sim']:+.3f} | "
            f"{r['gap_dollars_at_live_qty']:+,.0f} |"
        )
    return "\n".join(lines)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("date_from")
    ap.add_argument("date_to")
    ap.add_argument("--db", default=DEFAULT_DB)
    ap.add_argument("--account-class", default="paper")
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args()
    accept = _parse_account_class_arg(args.account_class)
    result = run_gap_analysis(args.date_from, args.date_to, args.db, accept)
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except (AttributeError, OSError):
        pass
    if args.json:
        print(json.dumps({k: v for k, v in result.items() if k != "rows"},
                         indent=2, default=str))
    else:
        print(format_report(result))
    return 0


if __name__ == "__main__":
    sys.exit(main())

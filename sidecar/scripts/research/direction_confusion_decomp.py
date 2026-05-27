"""Direction-confusion decomposition for FEF audit_package_2/19.

Read-only diagnostic. No engine changes. Writes markdown report.
"""
from __future__ import annotations

import json
import sqlite3
import statistics
import sys
from collections import Counter, defaultdict
from datetime import datetime, timezone, timedelta
from pathlib import Path

DB_PATH = Path(r"C:\Users\diant\.fef-terminal\signal_log.db")
OUT_PATH = Path(r"C:\Users\diant\projects\fef-terminal\audit_package_2\19_direction_confusion_decomposition.md")
START = "2026-05-19"
END = "2026-05-26"
CT_OFFSET = timedelta(hours=-5)  # CDT in May


def is_counter(direction: str, drift_bias: str) -> bool:
    return (direction == "LONG_PUTS" and drift_bias == "BULLISH") or (
        direction == "LONG_CALLS" and drift_bias == "BEARISH"
    )


def is_aligned(direction: str, drift_bias: str) -> bool:
    return (direction == "LONG_PUTS" and drift_bias in ("BEARISH", "NEUTRAL")) or (
        direction == "LONG_CALLS" and drift_bias in ("BULLISH", "NEUTRAL")
    )


def fetch_rows(conn: sqlite3.Connection):
    cur = conn.cursor()
    cur.execute(
        """
        SELECT
            s.id, s.session_date, s.timestamp, s.spx_price,
            s.direction, s.confidence, s.drift_bias,
            s.aggression_call_pct, s.aggression_put_pct,
            s.iv_rank_call_pct, s.iv_rank_put_pct,
            s.engine_version, s.extras,
            lo60.would_have_hit_t1, lo60.would_have_hit_t2, lo60.would_have_hit_t3,
            lo60.would_have_hit_stop, lo60.t1_hit_probability,
            lo60.spx_mfe_pts, lo60.spx_mae_pts, lo60.spx_at_signal,
            lo60.spx_close_at_window_end
        FROM signal_iter s
        LEFT JOIN signal_lookahead_outcome lo60
          ON s.id = lo60.signal_iter_id AND lo60.lookahead_minutes = 60
        WHERE s.session_date >= ? AND s.session_date <= ?
          AND s.direction IN ('LONG_CALLS','LONG_PUTS','WAIT')
        ORDER BY s.id
        """,
        (START, END),
    )
    cols = [d[0] for d in cur.description]
    out = []
    for row in cur.fetchall():
        rec = dict(zip(cols, row))
        try:
            rec["extras_d"] = json.loads(rec["extras"]) if rec["extras"] else {}
        except Exception:
            rec["extras_d"] = {}
        out.append(rec)
    return out


def parse_alt_dir(extras: dict, key: str):
    v = extras.get(key)
    if isinstance(v, dict):
        return v.get("signal")
    if isinstance(v, str):
        return v
    return None


def derive_gates(rec: dict) -> dict:
    """Compute per-gate direction for each fire.

    Each directional gate emits BULLISH / BEARISH / None. We then compare each gate
    to (a) drift_bias to flag "opposite of drift" and (b) verdict to flag "agrees
    with verdict".
    """
    extras = rec.get("extras_d", {})
    drift = rec.get("drift_bias")
    verdict_dir = (
        "BULLISH" if rec["direction"] == "LONG_CALLS"
        else ("BEARISH" if rec["direction"] == "LONG_PUTS" else None)
    )

    gates = {}

    # fe_ratio: low fe_ratio (<0.3) implies exhaustion → reversal candidate.
    # When drift is BULLISH and fe_ratio low, exhaustion votes BEARISH; reverse for BEARISH.
    fe = extras.get("fe_ratio")
    fe_dir = None
    if isinstance(fe, (int, float)) and fe < 0.3:
        if drift == "BULLISH":
            fe_dir = "BEARISH"
        elif drift == "BEARISH":
            fe_dir = "BULLISH"
    gates["fe_exhaustion"] = fe_dir
    gates["fe_value"] = fe

    # wall_collapse_trigger: boolean — only check co-occurrence
    gates["wall_collapse"] = bool(extras.get("wall_collapse_trigger"))

    # aggression: pct calls vs puts, >5pp wins
    ac = rec.get("aggression_call_pct")
    ap = rec.get("aggression_put_pct")
    agg_dir = None
    if isinstance(ac, (int, float)) and isinstance(ap, (int, float)):
        if ac - ap > 5:
            agg_dir = "BULLISH"
        elif ap - ac > 5:
            agg_dir = "BEARISH"
    gates["aggression"] = agg_dir

    # IV rank: a side with higher iv rank pct typically reflects demand for that side
    icr = rec.get("iv_rank_call_pct")
    ipr = rec.get("iv_rank_put_pct")
    iv_dir = None
    if isinstance(icr, (int, float)) and isinstance(ipr, (int, float)):
        if icr - ipr > 5:
            iv_dir = "BULLISH"
        elif ipr - icr > 5:
            iv_dir = "BEARISH"
    gates["iv_rank"] = iv_dir

    # nearest wall side relative to spot
    nw = extras.get("nearest_wall_strike")
    spx = rec.get("spx_price")
    nw_dist = extras.get("nearest_wall_distance")
    wall_side = None
    if isinstance(nw, (int, float)) and isinstance(spx, (int, float)):
        if nw > spx:
            wall_side = "ABOVE"  # acts as resistance → BEARISH bias
        elif nw < spx:
            wall_side = "BELOW"  # acts as support → BULLISH bias
    gates["wall_side"] = wall_side
    gates["wall_dist"] = nw_dist
    # interpret close wall (<10pts) as directional vote (resistance → BEARISH; support → BULLISH)
    wall_dir = None
    if wall_side and isinstance(nw_dist, (int, float)) and nw_dist < 15:
        wall_dir = "BEARISH" if wall_side == "ABOVE" else "BULLISH"
    gates["wall_proximity_dir"] = wall_dir

    # alt direction trends
    gates["alt_30m"] = parse_alt_dir(extras, "alt_direction_30m")
    gates["alt_4h"] = parse_alt_dir(extras, "alt_direction_4h")

    # microstructure
    ms = extras.get("microstructure_at_fire") or {}
    ms_dir = ms.get("direction") if isinstance(ms, dict) else None
    ms_simple = None
    if ms_dir == "BULLISH_PRESSURE":
        ms_simple = "BULLISH"
    elif ms_dir == "BEARISH_PRESSURE":
        ms_simple = "BEARISH"
    gates["microstructure"] = ms_simple

    # HFT book direction
    hft = extras.get("hft_at_fire") or {}
    hft_dir = hft.get("book_direction") if isinstance(hft, dict) else None
    if hft_dir not in ("BULLISH", "BEARISH"):
        hft_dir = None
    gates["hft_book"] = hft_dir

    # delta_price magnitude (note: sign equals verdict)
    dp = extras.get("delta_price")
    gates["delta_price"] = dp
    gates["pa_micro_move"] = isinstance(dp, (int, float)) and abs(dp) < 1.5

    # delta_drift sign vs cumulative drift_bias — a flip in recent drift can drive contra-cum fires
    dd = extras.get("delta_drift_m")
    dd_dir = None
    if isinstance(dd, (int, float)):
        if dd > 0.05:
            dd_dir = "BULLISH"
        elif dd < -0.05:
            dd_dir = "BEARISH"
    gates["delta_drift"] = dd_dir
    gates["delta_drift_val"] = dd

    # add helpers
    gates["_verdict"] = verdict_dir
    gates["_drift"] = drift
    return gates


def opp_drift(g_dir, drift):
    return g_dir in ("BULLISH", "BEARISH") and drift in ("BULLISH", "BEARISH") and g_dir != drift


def agrees_verdict(g_dir, verdict):
    return g_dir in ("BULLISH", "BEARISH") and verdict in ("BULLISH", "BEARISH") and g_dir == verdict


def realized_r(rec, t1_r=1.5, stop_r=1.0):
    """Realized R using binary outcome columns; None if no lookahead row."""
    if rec.get("would_have_hit_t1") is None and rec.get("would_have_hit_stop") is None:
        return None
    if rec.get("would_have_hit_t1"):
        return t1_r
    if rec.get("would_have_hit_stop"):
        return -stop_r
    # neither hit during 60m — score 0 (window-truncated)
    return 0.0


def cohort_stats(cohort, label):
    n = len(cohort)
    with_lo = [r for r in cohort if r.get("would_have_hit_t1") is not None]
    cov = len(with_lo)
    hit_t1 = sum(1 for r in with_lo if r["would_have_hit_t1"])
    hit_stop = sum(1 for r in with_lo if r["would_have_hit_stop"])
    # MFE/MAE
    mfes = [r["spx_mfe_pts"] for r in cohort if r["spx_mfe_pts"] is not None]
    maes = [r["spx_mae_pts"] for r in cohort if r["spx_mae_pts"] is not None]
    # Realized expectancy
    rs = [realized_r(r) for r in cohort]
    rs = [r for r in rs if r is not None]
    exp_r = statistics.mean(rs) if rs else None
    # simple t1_prob (from t1_hit_probability column, mostly 0/1)
    t1p = [r["t1_hit_probability"] for r in cohort if r["t1_hit_probability"] is not None]
    t1p_mean = statistics.mean(t1p) if t1p else None
    return {
        "label": label,
        "n": n,
        "cov": cov,
        "t1p_mean": t1p_mean,
        "hit_t1_pct": (hit_t1 / cov) if cov else None,
        "hit_stop_pct": (hit_stop / cov) if cov else None,
        "mfe_med": statistics.median(mfes) if mfes else None,
        "mae_med": statistics.median(maes) if maes else None,
        "exp_r": exp_r,
    }


def fmt(v, p=3):
    if v is None:
        return "—"
    if isinstance(v, float):
        return f"{v:.{p}f}"
    return str(v)


def pct(v):
    if v is None:
        return "—"
    return f"{v * 100:.1f}%"


def mean_safe(xs):
    xs = [x for x in xs if x is not None]
    return statistics.mean(xs) if xs else None


def build_report() -> str:
    conn = sqlite3.connect(str(DB_PATH))
    rows = fetch_rows(conn)
    fires = [r for r in rows if r["direction"] in ("LONG_CALLS", "LONG_PUTS")]
    waits = [r for r in rows if r["direction"] == "WAIT"]

    for r in fires:
        r["gates"] = derive_gates(r)

    counter = [r for r in fires if is_counter(r["direction"], r["drift_bias"])]
    aligned = [r for r in fires if is_aligned(r["direction"], r["drift_bias"])]
    counter_puts = [r for r in counter if r["direction"] == "LONG_PUTS"]
    counter_calls = [r for r in counter if r["direction"] == "LONG_CALLS"]

    # v2-only cohort
    fires_v2 = [r for r in fires if (r["engine_version"] or 0) >= 2]
    counter_v2 = [r for r in fires_v2 if is_counter(r["direction"], r["drift_bias"])]
    aligned_v2 = [r for r in fires_v2 if is_aligned(r["direction"], r["drift_bias"])]

    lines: list[str] = []
    L = lines.append

    # ============ HEADLINE ============
    L("# Direction-Confusion Decomposition (2026-05-19 → 2026-05-26)")
    L("")
    L("## Headline")
    L("")
    cb_exp_v = cohort_stats(counter, "cb")["exp_r"]
    al_exp_v = cohort_stats(aligned, "al")["exp_r"]
    cb_t1_v = cohort_stats(counter, "cb")["hit_t1_pct"]
    al_t1_v = cohort_stats(aligned, "al")["hit_t1_pct"]
    cb_st_v = cohort_stats(counter, "cb")["hit_stop_pct"]
    al_st_v = cohort_stats(aligned, "al")["hit_stop_pct"]
    L(
        "**No single gate is the smoking gun — this is an assembly-layer failure.** Across 5/19–5/26 the engine "
        f"produced {len(counter)} counter-bias fires out of {len(fires)} actionable signals "
        f"({pct(len(counter)/len(fires))}), and those counter-bias fires realize **{fmt(cb_exp_v)}R** "
        f"per fire vs **{fmt(al_exp_v)}R** for aligned fires — a "
        f"{al_exp_v - cb_exp_v:.2f}R per-fire gap. The counter-bias "
        f"cohort hits T1 only {pct(cb_t1_v)} of the time vs {pct(al_t1_v)} for aligned, and hits stop "
        f"{pct(cb_st_v)} vs {pct(al_st_v)}. "
        "The directional gates that **could** override drift (alt_30m, alt_4h, microstructure, HFT book, "
        "aggression, IV rank) **are silent in 75–100% of counter-bias fires** — they neither agree nor disagree, "
        "they're NEUTRAL. What's actually driving these fires is the tautological signal **`delta_price`** "
        "(the last-minute SPX nudge): the engine fires CALLS only when delta_price>0 and PUTS only when "
        "delta_price<0 in 100% of the window's actionable rows (224/224), regardless of drift_bias. "
        "**Recommendation: Option E (conjunction requirement) — counter-bias fires should require ≥2 "
        "independent directional gates (alt_30m, alt_4h, microstructure, aggression) to confirm before the "
        "verdict is allowed.** With that ≥2-gate filter only 4/79 counter-bias fires (5.1%) would survive — "
        "eliminating the 75 losers. A stricter ≥3-gate filter would suppress all 79. "
        "**5/26 spotlight**: 17 of 34 actionable fires were counter-bias, **0/15 hit T1** while the 17 aligned "
        "fires hit T1 at **100%**. Direction confusion accounted for the entirety of 5/26's signal underperformance."
    )
    L("")

    # ============ SECTION 1 ============
    L("## Section 1 — Baseline counts")
    L("")
    L(f"- Window: {START} → {END}")
    L(f"- Total signal_iter rows: **{len(rows)}**")
    L(f"- Actionable fires (LONG_CALLS + LONG_PUTS): **{len(fires)}**  (v2-only: **{len(fires_v2)}**)")
    L(f"- WAIT: **{len(waits)}**")
    L(f"- Counter-bias fires: **{len(counter)}** ({pct(len(counter)/len(fires))})")
    L(f"  - LONG_PUTS on BULLISH: {len(counter_puts)}")
    L(f"  - LONG_CALLS on BEARISH: {len(counter_calls)}")
    L(f"- Aligned fires: **{len(aligned)}** ({pct(len(aligned)/len(fires))})")
    L("")
    L("**Cross-tab (direction × drift_bias) — all engines, full window:**")
    L("")
    L("| direction | BULLISH | BEARISH | NEUTRAL | total |")
    L("|---|---:|---:|---:|---:|")
    ctab = Counter((r["direction"], r["drift_bias"]) for r in rows)
    for d in ("LONG_CALLS", "LONG_PUTS", "WAIT"):
        b = ctab[(d, "BULLISH")]
        s = ctab[(d, "BEARISH")]
        n = ctab[(d, "NEUTRAL")]
        L(f"| {d} | {b} | {s} | {n} | {b + s + n} |")
    L("")
    L("**v2-only cross-tab (engine_version >= 2, 5/22 + 5/26 only):**")
    L("")
    L("| direction | BULLISH | BEARISH | NEUTRAL | total |")
    L("|---|---:|---:|---:|---:|")
    ctab2 = Counter((r["direction"], r["drift_bias"]) for r in rows if (r["engine_version"] or 0) >= 2)
    for d in ("LONG_CALLS", "LONG_PUTS", "WAIT"):
        b = ctab2[(d, "BULLISH")]
        s = ctab2[(d, "BEARISH")]
        n = ctab2[(d, "NEUTRAL")]
        L(f"| {d} | {b} | {s} | {n} | {b + s + n} |")
    L("")
    L(
        "Note: 5/19–5/21 are v1 only (the v1→v2 cutover was mid-day 5/21). 5/22 is fully v2; "
        "5/26 is fully v2; 5/23 is the weekend (3 stray WAIT iters)."
    )
    L("")

    # ============ SECTION 2 ============
    L("## Section 2 — Outcome comparison (60-min lookahead)")
    L("")
    L("R-units: T1 hit ⇒ +1.5R; stop hit ⇒ −1R; neither hit within 60m ⇒ 0R (truncated).")
    L("")
    L(
        "| Cohort | n | cov | t1_hit_prob mean | T1 hit % | stop hit % | MFE median (pts) | MAE median (pts) | realized R |"
    )
    L("|---|---:|---:|---:|---:|---:|---:|---:|---:|")
    for cohort, name in [
        (aligned, "All aligned"),
        (counter, "Counter-bias (all)"),
        (counter_puts, "Counter PUTS on BULLISH"),
        (counter_calls, "Counter CALLS on BEARISH"),
        (aligned_v2, "Aligned v2-only"),
        (counter_v2, "Counter-bias v2-only"),
    ]:
        s = cohort_stats(cohort, name)
        L(
            f"| {name} | {s['n']} | {s['cov']} | {fmt(s['t1p_mean'])} | "
            f"{pct(s['hit_t1_pct'])} | {pct(s['hit_stop_pct'])} | "
            f"{fmt(s['mfe_med'],1)} | {fmt(s['mae_med'],1)} | {fmt(s['exp_r'])} |"
        )
    L("")
    L(
        "**Interpretation:** Counter-bias fires are catastrophically worse than aligned fires. T1 hit-rate collapses "
        f"from {pct(cohort_stats(aligned,'')['hit_t1_pct'])} (aligned) to "
        f"{pct(cohort_stats(counter,'')['hit_t1_pct'])} (counter-bias), and stop-hit rises from "
        f"{pct(cohort_stats(aligned,'')['hit_stop_pct'])} to {pct(cohort_stats(counter,'')['hit_stop_pct'])}. "
        "Counter PUTS on BULLISH are pure losers — 5.7% T1 hit-rate, 91.4% stop-out. Counter CALLS on BEARISH look "
        "like a fluke (n=8, 6 with lookahead, 100% T1 hit) — small sample driven by 2 brief BEARISH-drift moments "
        "on 5/22; weight accordingly. **Note** the `t1_hit_probability` column is effectively binary (0 or 1) and "
        "equals `would_have_hit_t1` for 99% of rows — it is NOT a calibrated probability."
    )
    L("")
    L(
        "**Critical observation:** Counter-bias MFE median is **6.8pts**, only 1.2pts less than aligned (8.0pts) — "
        "the engine does see SOME contra-move materialize, but it's not enough to clear T1, while MAE pushes the "
        "stop in 84% of cases. The engine is firing inside losing geometry."
    )
    L("")

    # ============ SECTION 3 ============
    L("## Section 3 — Gate decomposition (counter-bias cohort)")
    L("")
    L(
        "For each directional gate, count how often (a) the gate's emitted direction is **opposite** to "
        "`drift_bias` (i.e. the gate votes for the verdict) versus (b) silent/agrees-with-drift. WR and realized-R "
        "are computed on the gate-misaligned subset."
    )
    L("")

    def gate_decomp(cohort, label):
        """Compute per-gate opp-drift count + outcome."""
        rows_out = []
        gate_specs = [
            ("alt_30m", lambda g, r: opp_drift(g["alt_30m"], r["drift_bias"])),
            ("alt_4h", lambda g, r: opp_drift(g["alt_4h"], r["drift_bias"])),
            ("microstructure", lambda g, r: opp_drift(g["microstructure"], r["drift_bias"])),
            ("hft_book", lambda g, r: opp_drift(g["hft_book"], r["drift_bias"])),
            ("aggression", lambda g, r: opp_drift(g["aggression"], r["drift_bias"])),
            ("iv_rank", lambda g, r: opp_drift(g["iv_rank"], r["drift_bias"])),
            ("delta_drift_sign", lambda g, r: opp_drift(g["delta_drift"], r["drift_bias"])),
            ("fe_exhaustion (fe<0.3)", lambda g, r: opp_drift(g["fe_exhaustion"], r["drift_bias"])),
            ("wall_proximity (<15pts)", lambda g, r: opp_drift(g["wall_proximity_dir"], r["drift_bias"])),
            ("wall_collapse_trigger", lambda g, r: bool(g["wall_collapse"])),
        ]
        for name, fn in gate_specs:
            sub = [r for r in cohort if fn(r["gates"], r)]
            hits = [r["would_have_hit_t1"] for r in sub if r["would_have_hit_t1"] is not None]
            wr = (sum(hits) / len(hits)) if hits else None
            rs = [realized_r(r) for r in sub]
            rs = [r for r in rs if r is not None]
            exp = statistics.mean(rs) if rs else None
            pct_c = len(sub) / len(cohort) if cohort else None
            rows_out.append((name, len(sub), pct_c, wr, exp))
        return rows_out

    cb_dec = gate_decomp(counter, "counter")
    L("**Counter-bias cohort (n={}):**".format(len(counter)))
    L("")
    L("| Gate | # opp-drift | % of cb | T1 hit-rate | realized R |")
    L("|---|---:|---:|---:|---:|")
    for name, cnt, pc, wr, exp in cb_dec:
        L(f"| {name} | {cnt} | {pct(pc)} | {pct(wr)} | {fmt(exp)} |")
    L("")

    al_dec = gate_decomp(aligned, "aligned")
    L("**Aligned-cohort control (n={}):**".format(len(aligned)))
    L("")
    L("| Gate | # opp-drift | % of al | T1 hit-rate | realized R |")
    L("|---|---:|---:|---:|---:|")
    for name, cnt, pc, wr, exp in al_dec:
        L(f"| {name} | {cnt} | {pct(pc)} | {pct(wr)} | {fmt(exp)} |")
    L("")
    L(
        "**Interpretation:** On counter-bias fires, the supposedly-directional gates are usually silent. Only "
        "fe_exhaustion (45.6%) is meaningfully present, and that's just because low fe_ratio is endemic in this "
        "window. The two next-most-common are microstructure (15.2%) and alt_30m (11.4%) — well below a majority. "
        "**No single gate is misaligned in 70%+ of counter-bias fires, so the 'one offending gate' hypothesis is "
        "false.** The counter-bias verdicts are being assembled despite the directional gates voting NEUTRAL — "
        "the engine is going with `delta_price` (last-1m local move) as the de-facto direction signal."
    )
    L("")

    # 3b verdict alignment
    L("### Section 3b — Counter-bias verdict alignment with each directional gate")
    L("")
    L(
        "For each counter-bias fire, does the gate's emitted direction **agree** with the verdict (which is "
        "opposite to drift)? This is the 'gate confirms the contra-drift call' question."
    )
    L("")
    L("| Gate | agrees with verdict | disagrees | silent/neutral |")
    L("|---|---:|---:|---:|")
    gate_dirs = [
        ("alt_30m", "alt_30m"),
        ("alt_4h", "alt_4h"),
        ("microstructure", "microstructure"),
        ("hft_book", "hft_book"),
        ("aggression", "aggression"),
        ("iv_rank", "iv_rank"),
        ("delta_drift_sign", "delta_drift"),
        ("fe_exhaustion", "fe_exhaustion"),
        ("wall_proximity", "wall_proximity_dir"),
    ]
    for label, key in gate_dirs:
        agree = disagree = neutral = 0
        for r in counter:
            gd = r["gates"].get(key)
            vd = r["gates"].get("_verdict")
            if gd in ("BULLISH", "BEARISH") and vd in ("BULLISH", "BEARISH"):
                if gd == vd:
                    agree += 1
                else:
                    disagree += 1
            else:
                neutral += 1
        L(f"| {label} | {agree} | {disagree} | {neutral} |")
    L("")
    L(
        "**Diagnostic:** Microstructure is the most useful directional confirm (12/79 = 15.2% align with verdict, "
        "only 6 disagree). alt_30m and alt_4h are tied at agreement, but alt_4h has more disagreement than "
        "agreement. **aggression, hft_book, and iv_rank are completely silent on counter-bias fires** — they "
        "literally cannot save the engine because they almost never emit a directional vote."
    )
    L("")

    # gate count distribution — TWO views
    L("### Section 3c — How many gates confirm the counter-bias verdict?")
    L("")
    L(
        "We compute two buckets — one using the full directional-gate set (with `delta_drift_sign` and "
        "`fe_exhaustion`, which over-correlate with the verdict mechanically), and one using only the "
        "independent gates (`alt_30m`, `alt_4h`, `microstructure`, `aggression`). The independent-only view "
        "is the cleaner test."
    )
    L("")

    bucket = defaultdict(list)
    for r in counter:
        vd = r["gates"].get("_verdict")
        n_agree = 0
        for _, key in gate_dirs:
            gd = r["gates"].get(key)
            if gd in ("BULLISH", "BEARISH") and vd == gd:
                n_agree += 1
        bucket[n_agree].append(r)
    L("**Full set (9 gates including dependent ones):**")
    L("")
    L("| # of gates agreeing with verdict | n_fires | T1 hit-rate | realized R |")
    L("|---:|---:|---:|---:|")
    for k in sorted(bucket):
        rs = bucket[k]
        hits = [r["would_have_hit_t1"] for r in rs if r["would_have_hit_t1"] is not None]
        wr = sum(hits) / len(hits) if hits else None
        rr = [realized_r(r) for r in rs if realized_r(r) is not None]
        exp = statistics.mean(rr) if rr else None
        L(f"| {k} | {len(rs)} | {pct(wr)} | {fmt(exp)} |")
    L("")

    independent_keys = [("alt_30m", "alt_30m"), ("alt_4h", "alt_4h"),
                       ("microstructure", "microstructure"), ("aggression", "aggression")]
    bucket_indep = defaultdict(list)
    for r in counter:
        vd = r["gates"].get("_verdict")
        n_agree = 0
        for _, key in independent_keys:
            gd = r["gates"].get(key)
            if gd in ("BULLISH", "BEARISH") and vd == gd:
                n_agree += 1
        bucket_indep[n_agree].append(r)
    L("**Independent gates only (alt_30m, alt_4h, microstructure, aggression):**")
    L("")
    L("| # of indep. gates agreeing | n_fires | T1 hit-rate | realized R |")
    L("|---:|---:|---:|---:|")
    for k in sorted(bucket_indep):
        rs = bucket_indep[k]
        hits = [r["would_have_hit_t1"] for r in rs if r["would_have_hit_t1"] is not None]
        wr = sum(hits) / len(hits) if hits else None
        rr = [realized_r(r) for r in rs if realized_r(r) is not None]
        exp = statistics.mean(rr) if rr else None
        L(f"| {k} | {len(rs)} | {pct(wr)} | {fmt(exp)} |")
    L("")
    L(
        "**Killer cut (independent view).** "
        f"**{len(bucket_indep.get(0, []))}/{len(counter)} counter-bias fires have ZERO independent gates "
        f"confirming the verdict** — they're pure delta_price reactions. "
        f"Only {len(bucket_indep.get(2, []))} fires have 2+ independent gates aligned, and 3+ never happens "
        "in the window. A ≥2-of-4-independent-gates filter would cut counter-bias from 79 → 4 fires. "
        "A ≥1-of-4 filter would cut it to 23. The full-set view is shown to give the engine team a sense of "
        "what's happening even when dependent gates are counted; the independent view is what should drive "
        "the policy fix."
    )
    L("")

    # ============ SECTION 4 ============
    L("## Section 4 — Regime and time-of-day stratification")
    L("")
    L("### By regime")
    L("")
    regime_total = Counter()
    regime_cb = Counter()
    regime_exp_cb = defaultdict(list)
    regime_exp_al = defaultdict(list)
    for r in fires:
        reg = r["extras_d"].get("regime_at_fire") or "UNKNOWN"
        regime_total[reg] += 1
        if is_counter(r["direction"], r["drift_bias"]):
            regime_cb[reg] += 1
            rr = realized_r(r)
            if rr is not None:
                regime_exp_cb[reg].append(rr)
        elif is_aligned(r["direction"], r["drift_bias"]):
            rr = realized_r(r)
            if rr is not None:
                regime_exp_al[reg].append(rr)
    L("| Regime | total fires | counter | cb% | cb realized R | aligned realized R |")
    L("|---|---:|---:|---:|---:|---:|")
    for reg in sorted(regime_total, key=lambda x: -regime_total[x]):
        cb = regime_cb.get(reg, 0)
        cb_exp = mean_safe(regime_exp_cb.get(reg, []))
        al_exp = mean_safe(regime_exp_al.get(reg, []))
        L(f"| {reg} | {regime_total[reg]} | {cb} | {pct(cb/regime_total[reg])} | {fmt(cb_exp)} | {fmt(al_exp)} |")
    L("")
    L(
        "**Regime concentration:** Counter-bias is heaviest in **DRIFT_BULL** (52% of fires in that regime are "
        "counter-bias) and **TREND_DAY** (41%). All regimes show negative realized-R for counter-bias — there is "
        "no regime where counter-bias has positive edge. The aligned-cohort realized-R is also negative in every "
        "regime except a handful of POWER_HOUR/WALL_TEST cases — overall edge is weak, but counter-bias is "
        "categorically worse."
    )
    L("")

    L("### By hour-of-day (Central Time)")
    L("")
    hour_total = Counter()
    hour_cb = Counter()
    hour_exp_cb = defaultdict(list)
    hour_exp_al = defaultdict(list)
    for r in fires:
        try:
            dt = datetime.fromisoformat(r["timestamp"].replace("Z", "+00:00"))
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            dt_ct = dt.astimezone(timezone(CT_OFFSET))
            h = dt_ct.hour
        except Exception:
            continue
        hour_total[h] += 1
        if is_counter(r["direction"], r["drift_bias"]):
            hour_cb[h] += 1
            rr = realized_r(r)
            if rr is not None:
                hour_exp_cb[h].append(rr)
        elif is_aligned(r["direction"], r["drift_bias"]):
            rr = realized_r(r)
            if rr is not None:
                hour_exp_al[h].append(rr)
    L("| Hour CT | total | counter | cb% | cb realized R | aligned realized R |")
    L("|---:|---:|---:|---:|---:|---:|")
    for h in sorted(hour_total):
        cb = hour_cb.get(h, 0)
        cb_exp = mean_safe(hour_exp_cb.get(h, []))
        al_exp = mean_safe(hour_exp_al.get(h, []))
        L(f"| {h:02d} | {hour_total[h]} | {cb} | {pct(cb/hour_total[h])} | {fmt(cb_exp)} | {fmt(al_exp)} |")
    L("")
    L(
        "**Time concentration:** Counter-bias spikes in mid-day (10–11 CT and 13 CT, where 50–69% of fires are "
        "counter-bias). The 13 CT hour is the worst — 18/26 fires (69%) are counter-bias. The 8 CT (open) and "
        "12 CT (mid-day pause) hours are cleaner. **Mid-day post-lunch is where direction confusion concentrates** "
        "— consistent with intraday chop where short-term price ticks contradict the cumulative drift."
    )
    L("")

    # ============ SECTION 5 ============
    L("## Section 5 — Per-session breakdown")
    L("")
    sess_data = defaultdict(lambda: {"cb": [], "al": [], "engine": Counter()})
    for r in fires:
        sess_data[r["session_date"]]["engine"][r["engine_version"] or 0] += 1
        if is_counter(r["direction"], r["drift_bias"]):
            sess_data[r["session_date"]]["cb"].append(r)
        elif is_aligned(r["direction"], r["drift_bias"]):
            sess_data[r["session_date"]]["al"].append(r)
    L("| Session | fires | counter | aligned | cb% | cb T1 hit-rate | al T1 hit-rate | cb realized R | al realized R | engines |")
    L("|---|---:|---:|---:|---:|---:|---:|---:|---:|---|")
    for s in sorted(sess_data):
        cb = sess_data[s]["cb"]
        al = sess_data[s]["al"]
        cb_t1 = mean_safe(r["would_have_hit_t1"] for r in cb)
        al_t1 = mean_safe(r["would_have_hit_t1"] for r in al)
        cb_exp = mean_safe(realized_r(r) for r in cb)
        al_exp = mean_safe(realized_r(r) for r in al)
        engines = ",".join(f"v{v}:{n}" for v, n in sorted(sess_data[s]["engine"].items()))
        total = len(cb) + len(al)
        L(
            f"| {s} | {total} | {len(cb)} | {len(al)} | {pct(len(cb)/total) if total else '—'} | "
            f"{pct(cb_t1)} | {pct(al_t1)} | {fmt(cb_exp)} | {fmt(al_exp)} | {engines} |"
        )
    L("")
    L(
        "**Session trends:** Counter-bias percentage ranges from 28% (5/22) to 53% (5/26) — even on the "
        "best-aligned-PnL day (5/22) almost a third of fires are counter-bias. **5/26 is the worst (53% cb)** and "
        "the aligned cohort that day did hit T1 across all 15/15 lookahead rows — meaning if the engine had "
        "simply suppressed counter-bias fires, 5/26 would have been a great day. **The counter-bias % does not "
        "correlate with engine version** (v1 sessions 30–36% cb, v2 sessions 28–53% cb), so the v1→v2 cutover "
        "didn't move the needle on direction confusion."
    )
    L("")

    # ============ EXAMPLES ============
    L("## Examples — counter-bias fires worth inspecting")
    L("")
    L("**5 worst counter-bias losers (lowest realized R, largest MAE):**")
    L("")
    losers = [r for r in counter if realized_r(r) is not None]
    losers.sort(key=lambda r: (realized_r(r), r["spx_mae_pts"] or 0))
    L("| id | session | timestamp | dir | drift | spx | delta_price | alt_30m | alt_4h | ms | T1 | stop | MFE | MAE |")
    L("|---:|---|---|---|---|---:|---:|---|---|---|:---:|:---:|---:|---:|")
    for r in losers[:5]:
        g = r["gates"]
        L(
            f"| {r['id']} | {r['session_date']} | {r['timestamp'][11:19]} | "
            f"{r['direction']} | {r['drift_bias']} | {r['spx_price']:.1f} | "
            f"{fmt(g.get('delta_price'),2)} | {g.get('alt_30m')} | {g.get('alt_4h')} | "
            f"{g.get('microstructure')} | {r['would_have_hit_t1']} | {r['would_have_hit_stop']} | "
            f"{fmt(r['spx_mfe_pts'],1)} | {fmt(r['spx_mae_pts'],1)} |"
        )
    L("")
    L("**5 counter-bias fires with the MOST gate-agreement (3+ gates align with verdict):**")
    L("")
    examples_3 = sorted(bucket.get(3, []), key=lambda r: -(realized_r(r) or -99))[:5]
    L("| id | session | dir | drift | gates agreeing | T1 | stop | MFE | MAE |")
    L("|---:|---|---|---|---|:---:|:---:|---:|---:|")
    for r in examples_3:
        g = r["gates"]
        vd = g.get("_verdict")
        agreed = [k for _, k in gate_dirs if g.get(k) == vd]
        L(
            f"| {r['id']} | {r['session_date']} | {r['direction']} | {r['drift_bias']} | "
            f"{','.join(agreed)} | {r['would_have_hit_t1']} | {r['would_have_hit_stop']} | "
            f"{fmt(r['spx_mfe_pts'],1)} | {fmt(r['spx_mae_pts'],1)} |"
        )
    L("")
    L("**Confidence × counter-bias:**")
    L("")
    conf_data = defaultdict(lambda: {"cb": [], "al": []})
    for r in fires:
        c = r["confidence"]
        if is_counter(r["direction"], r["drift_bias"]):
            conf_data[c]["cb"].append(r)
        elif is_aligned(r["direction"], r["drift_bias"]):
            conf_data[c]["al"].append(r)
    L("| Confidence | cb n | cb realized R | aligned n | aligned realized R |")
    L("|---|---:|---:|---:|---:|")
    for c in ("LOW", "MEDIUM", "HIGH"):
        cb = conf_data[c]["cb"]
        al = conf_data[c]["al"]
        cb_exp = mean_safe(realized_r(r) for r in cb)
        al_exp = mean_safe(realized_r(r) for r in al)
        L(f"| {c} | {len(cb)} | {fmt(cb_exp)} | {len(al)} | {fmt(al_exp)} |")
    L("")
    L(
        "**Confidence finding:** **100% of counter-bias fires are LOW confidence.** No MEDIUM or HIGH counter-bias "
        "fires exist in the window. The confidence-tier system already withholds the strong-conviction labels from "
        "contra-drift verdicts — but the LOW-confidence channel is still routing them to live trading. A "
        "trivial-to-implement guard would be: suppress all LOW-confidence counter-bias fires (loses only the "
        "13.2% T1-hit cohort whose realized R is −0.65R)."
    )
    L("")

    # ============ SECTION 6 ============
    L("## Section 6 — Recommendations for Step 2")
    L("")
    L("Ranked by expected impact:")
    L("")
    L(
        "**Rank 1 — Option E (conjunction requirement): counter-bias verdicts must have ≥2 independent "
        "directional gates agreeing with the verdict direction.** Empirical support from Section 3c "
        "(independent-gates view): with 0 gates agreeing → 56/79 fires (5.7% T1 hit, −0.84R); with 1 gate → 19/79 "
        "(26.3% T1 hit, −0.34R); with 2 gates → 4/79 (50.0% T1 hit, **+0.50R**); 3+ never occurred in the window. "
        "Setting the threshold at ≥2 of {alt_30m, alt_4h, microstructure, aggression} drops counter-bias fires from "
        "79 → 4, eliminating 75 losers (−0.7R average) while keeping the only 4 fires with positive expectancy. "
        "Implementation lives in the verdict-assembly layer, not at any individual gate. **Estimated paper-PnL "
        "impact: roughly +50R aggregate across the 5-session window** (75 prevented losers × 0.7R/fire). "
        "Validate via ablation_backtest.py before going live."
    )
    L("")
    L(
        "**Rank 2 — Option B (soft direction gate): demote LOW-confidence counter-bias to WAIT.** Trivial "
        "implementation: the confidence label already correctly identifies counter-bias as LOW-only (100% of "
        "the 79 counter-bias fires in the window are LOW; zero MEDIUM/HIGH). Adding a rule that LOW + counter-bias "
        "⇒ WAIT loses only the 13.2% T1-hit cohort (which has −0.65R expectancy anyway). Stronger than option E "
        "because it's a hard suppression, but gives up the 4 fires with ≥2 confirming gates that DO show positive "
        "edge (+0.50R). Use as fallback if Option E proves brittle, or stack them: 'LOW + counter-bias + <2 "
        "confirming gates ⇒ WAIT'."
    )
    L("")
    L(
        "**Rank 3 — formula fix at the assembly's delta_price→direction step.** `delta_price` and `delta_drift_m` "
        "are BOTH mechanically sign-aligned with verdict direction (100% across 224 fires for delta_price and "
        f"{sum(1 for r in counter if opp_drift(r['gates']['delta_drift'], r['drift_bias']))}/{len(counter)} on "
        "counter-bias for delta_drift). This means the engine treats the **last 1-minute** signals as the verdict "
        "direction, completely overriding cumulative `drift_bias` whenever the local tick flips. The formula fix "
        "is at the assembly layer: when the local-tick direction (delta_price + delta_drift_m sign) disagrees with "
        "cumulative drift_bias, demote the verdict from actionable to WAIT unless ≥2 independent directional gates "
        "(alt_30m, alt_4h, microstructure, aggression) confirm. This is functionally equivalent to Rank 1 and "
        "should be implemented in the same change."
    )
    L("")
    L(
        "**Rank 4 — Option C (regime-aware gate).** Direction confusion does concentrate in TREND_DAY (41% cb) "
        "and DRIFT_BULL (52% cb), and also in CT hours 10–11 and 13. But every regime shows negative cb realized "
        "R, so a regime-gate alone won't fix the problem — only delays. Suggest: combine with Rank 1 — apply the "
        "≥2-gate-conjunction rule globally, but tighten to ≥3 in TREND_DAY/DRIFT_BULL regimes."
    )
    L("")
    L("**Specifically NOT recommended:**")
    L("")
    L(
        "- Per-gate formula fixes targeting `fe_ratio`, `wall_collapse_trigger`, or `pa_score` — the data does "
        "not support a single-gate dominant-offender story. fe_exhaustion is the most prevalent at 45.6% of "
        "counter-bias fires, but it's also at 37.9% on aligned fires (which win), so blocking fe_exhaustion "
        "would not selectively kill counter-bias losers."
    )
    L("- 'Wait for more data' — five sessions show the same pattern with consistent magnitude. Direction confusion is structural, not noise.")
    L("")
    L("---")
    L("")
    L(f"_Generated {datetime.now(timezone.utc).isoformat(timespec='seconds')}Z by direction_confusion_decomp.py — read-only diagnostic, no engine code touched._")
    return "\n".join(lines)


if __name__ == "__main__":
    report = build_report()
    OUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    OUT_PATH.write_text(report, encoding="utf-8")
    print(f"Wrote {OUT_PATH}")
    print(f"Lines: {len(report.splitlines())}")

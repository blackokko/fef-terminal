"""Placebo event-study: is `vwap_loss_short` CAUSALLY about VWAP, or is VWAP just
a costume over generic intraday momentum / time-of-day?

The real signal (engine.signals.vwap.detect, kind='vwap_loss_short') fires when
price held ABOVE the session anchored-VWAP for >=15 min and then a 5-min bar
closes BELOW it -> short (PUTS). If that VWAP level is doing real work, the real
signal should BEAT a battery of placebo "anchors" that share its cadence, its
exit, and (where possible) its time-of-day / volatility profile but replace the
VWAP level with something with no special standing (no-hold version, a rolling
SMA, a randomly time-shifted VWAP, the *prior* day's VWAP, or a pure
time/ATR-matched random short). If a placebo matches or beats the real signal,
the VWAP level is decorative and we're really trading momentum/time-of-day.

EVERYTHING is scored with the SAME conservative exit as the frozen shadow
contract (shadow_vwap_loss_short): entry = OPEN of the first ES 1-min bar at/after
the 5-min bar's close (ts = trigger_bar.ts_ms + 5*60_000); risk = max(1*ATR, 4.0)
with ATR = 14-period ATR on the 5-min series at the trigger bar; short stop =
entry+risk, target = entry-2*ATR; forward-walk on 1-min bars ADVERSE-FIRST (a bar
that touches both stop and target counts as a STOP); friction 1.5pt; R in the
trade's own risk units; ONE-TRADE-AT-A-TIME (a signal whose entry lands while a
prior trade is still open is skipped). This is identical to score_session(), so
the comparison is apples-to-apples — the ONLY thing that varies across A-E is
which bars get flagged as "anchor lost from above".

Second analysis: microstructure event-study around each REAL break — does
sell-side order flow (negative OFI, positive sell imbalance, worsening
microprice) actually intensify AFTER the VWAP loss vs the 30 min before? That is
the "distribution continuation" mechanism the signal claims to exploit.

Pure Python (the sidecar venv has no pandas/numpy). No live code touched; reuses
the frozen scorer's _atr/_walk_short/_et_date so the placebos can't accidentally
get a more generous exit than the real signal.

Run:  .venv/Scripts/python.exe -m scripts.research.vwap_placebo_eventstudy
"""

from __future__ import annotations

import bisect
import math
import os
import random
import sqlite3
import statistics as stats
from collections import defaultdict

from fef_sidecar.engine.signals import dale, vwap
from fef_sidecar.engine.signals.dale import Bar
from fef_sidecar.engine.signals.vwap import anchored_vwap, session_key
# Reuse the FROZEN scorer pieces so every signal set gets the identical exit.
from fef_sidecar.engine.signals.shadow_vwap_loss_short import (
    _atr, _et_date, _walk_short, score_session,
    TF, ATR_STOP_MULT, ATR_TARGET_MULT, MIN_STOP_PTS, FRICTION_PTS,
)

DB = os.path.expanduser("~/.fef-terminal/signal_log.db")
ES_BAR_SIZE = "1 min RTH"
RECLAIM_MIN = 15                                  # the "held above for >=15min" window
RECLAIM_BARS = max(1, math.ceil(RECLAIM_MIN / TF))  # in 5-min bars => 3


# ---------------------------------------------------------------------------
# Data loading
# ---------------------------------------------------------------------------


def load_es_rth(c: sqlite3.Connection) -> list[Bar]:
    rows = c.execute(
        "SELECT timestamp_ms,open,high,low,close,volume FROM historical_bar "
        "WHERE symbol='ES' AND bar_size=? ORDER BY timestamp_ms", (ES_BAR_SIZE,),
    ).fetchall()
    return [Bar(ts_ms=r[0], open=r[1], high=r[2], low=r[3], close=r[4],
                volume=r[5] or 0.0) for r in rows]


# ---------------------------------------------------------------------------
# Generic conservative scorer (IDENTICAL exit to score_session, but driven by an
# arbitrary list of "trigger" 5-min bars instead of vwap.detect()).
#
# `trigger_ts` is the ts_ms (start) of the 5-min bar whose CLOSE flags the short.
# This mirrors Signal.ts_ms in the real path, so reusing the frozen entry/exit
# logic is exact. Returns one dict per TAKEN trade with r_outcome/session_date/win.
# ---------------------------------------------------------------------------


def score_triggers(trigger_ts: list[int], es5: list[Bar], atr: list[float | None],
                   imap: dict[int, int], es1: list[Bar], ts1: list[int]) -> list[dict]:
    out: list[dict] = []
    busy_until = 0  # one-trade-at-a-time gate (ms), same semantics as score_session
    for tts in sorted(set(trigger_ts)):
        i = imap.get(tts)
        a = atr[i] if i is not None else None
        if a is None or a <= 0:
            continue
        # realistic entry: open of the first 1-min bar at/after the 5-min close
        ei = bisect.bisect_left(ts1, tts + TF * 60_000)
        if ei >= len(es1):
            continue
        entry_ts = ts1[ei]
        if entry_ts < busy_until:                 # a prior trade is still open -> skip
            continue
        entry = es1[ei].open
        risk = max(ATR_STOP_MULT * a, MIN_STOP_PTS)
        stop = entry + risk                        # short
        target = entry - ATR_TARGET_MULT * a
        sd = _et_date(tts)
        r, exit_ms = _walk_short(entry, target, stop, es1, ts1, entry_ts, sd)
        if r is None:
            continue
        busy_until = exit_ms or busy_until
        r -= FRICTION_PTS / risk
        out.append({"session_date": sd, "timestamp_ms": tts,
                    "r_outcome": r, "win": 1.0 if r > 0 else 0.0})
    return out


# ---------------------------------------------------------------------------
# Placebo anchor builders. Each returns the list of trigger 5-min bar ts_ms
# (the bar whose close < anchor, with the analogous "held above first" setup).
# The setup logic mirrors vwap.detect's reclaim/loss block: prior RECLAIM_BARS
# bars all strictly ABOVE the anchor in the same session, then this bar closes
# strictly BELOW it. hold_bars=1 in the real signal => fire on the loss bar.
# ---------------------------------------------------------------------------


def _session_keys(es5: list[Bar]) -> list[str]:
    return [session_key(b.ts_ms) for b in es5]


def _held_above_then_lost(es5: list[Bar], sk: list[str], anchor: list[float | None],
                          *, require_hold: bool) -> list[int]:
    """Generic 'lost an anchor from above' detector at the real signal's cadence.

    require_hold=True  -> reproduce the real setup: prior RECLAIM_BARS bars all
                          strictly above `anchor` (same session) AND this bar closes
                          below it (placebo B, and the SMA-with-hold case).
    require_hold=False -> just the FIRST bar in each session that closes below the
                          anchor while the IMMEDIATELY prior bar was above it (the
                          'no 15-min hold' placebo A) — still a genuine cross from
                          above, just no dwell requirement.
    """
    n = len(es5)
    triggers: list[int] = []
    first_done: set[str] = set()
    for i in range(n):
        av = anchor[i]
        if av is None:
            continue
        if es5[i].close >= av:                     # must close BELOW the anchor
            continue
        if require_hold:
            start = i - RECLAIM_BARS
            if start < 0:
                continue
            ok = True
            for k in range(start, i):
                ak = anchor[k]
                if ak is None or sk[k] != sk[i] or es5[k].close <= ak:
                    ok = False
                    break
            if ok:
                triggers.append(es5[i].ts_ms)
        else:
            # no-hold: prior bar above, same session, and first such cross of the day
            s = sk[i]
            if s in first_done:
                continue
            if i - 1 < 0 or sk[i - 1] != s:
                continue
            ap = anchor[i - 1]
            if ap is None or es5[i - 1].close <= ap:
                continue
            triggers.append(es5[i].ts_ms)
            first_done.add(s)
    return triggers


def anchor_session_vwap(es5: list[Bar]) -> list[float | None]:
    sv = anchored_vwap(es5, session_key)
    return [s.vwap for s in sv]


def anchor_sma(es5: list[Bar], period: int = 20) -> list[float | None]:
    """20-period rolling SMA of close — a 'generic mean breakdown' anchor (B)."""
    closes = [b.close for b in es5]
    out: list[float | None] = [None] * len(es5)
    for i in range(period - 1, len(es5)):
        out[i] = sum(closes[i - period + 1:i + 1]) / period
    return out


def anchor_shifted_vwap(es5: list[Bar], seed_idx: int) -> list[float | None]:
    """Session VWAP read at a RANDOM integer-minute offset (placebo anchor C).

    The anchor used at bar i is the session VWAP value sampled `off` minutes away
    (rounded to the 5-min grid, clamped into the same session). A genuine VWAP
    number, but deliberately mis-timed so it carries no information about *this*
    bar's relationship to the live VWAP. Seed varies per call for reproducibility;
    we never use time/Math.random.
    """
    rng = random.Random(20260530 + seed_idx)
    off_min = rng.randint(-30, 30)
    while off_min == 0:                            # a zero offset is just the real VWAP
        off_min = rng.randint(-30, 30)
    off_bars = int(round(off_min / TF))
    sv = anchored_vwap(es5, session_key)
    sk = _session_keys(es5)
    out: list[float | None] = [None] * len(es5)
    for i in range(len(es5)):
        j = i + off_bars
        if j < 0 or j >= len(es5) or sk[j] != sk[i]:
            out[i] = None                          # offset would cross a session boundary
        else:
            out[i] = sv[j].vwap
    return out


def anchor_prior_day_vwap(es5: list[Bar]) -> list[float | None]:
    """Prior session's FINAL anchored-VWAP carried as a flat level (placebo D)."""
    sv = anchored_vwap(es5, session_key)
    sk = _session_keys(es5)
    final_by_session: dict[str, float] = {}
    for s, st in zip(sk, sv):
        final_by_session[s] = st.vwap            # last write per session = its close VWAP
    sessions_in_order: list[str] = []
    seen: set[str] = set()
    for s in sk:
        if s not in seen:
            seen.add(s)
            sessions_in_order.append(s)
    prior_level: dict[str, float | None] = {}
    for idx, s in enumerate(sessions_in_order):
        prior_level[s] = None if idx == 0 else final_by_session[sessions_in_order[idx - 1]]
    return [prior_level[s] for s in sk]


def placebo_E_random_short(real_trades: list[dict], es5: list[Bar],
                           atr: list[float | None], imap: dict[int, int],
                           seed: int = 20260530) -> list[int]:
    """Time-of-day + ATR-bucket matched random short (control E).

    For each REAL trade we draw ONE random 5-min bar that (a) falls in the same
    ET hour and (b) shares the real bar's ATR tercile, across ALL sessions. Fires
    a short on no real level — only matched on *when* and *how volatile*. If this
    control matches the real signal, 'short at these times/vols' is the whole edge.
    """
    rng = random.Random(seed)
    import datetime as dt

    def et_hour(ts):
        return dt.datetime.fromtimestamp(ts / 1000, dt.timezone(dt.timedelta(hours=-4))).hour

    # ATR tercile thresholds over defined ATR values
    defined = sorted(a for a in atr if a is not None and a > 0)
    if not defined:
        return []
    q1 = defined[len(defined) // 3]
    q2 = defined[2 * len(defined) // 3]

    def atr_bucket(a):
        if a is None:
            return -1
        return 0 if a <= q1 else (1 if a <= q2 else 2)

    # index 5-min bars by (et_hour, atr_bucket)
    pool: dict[tuple[int, int], list[int]] = defaultdict(list)
    for i, b in enumerate(es5):
        a = atr[i]
        if a is None or a <= 0:
            continue
        pool[(et_hour(b.ts_ms), atr_bucket(a))].append(b.ts_ms)

    triggers: list[int] = []
    for t in real_trades:
        i = imap.get(t["timestamp_ms"])
        a = atr[i] if i is not None else None
        key = (et_hour(t["timestamp_ms"]), atr_bucket(a))
        cands = pool.get(key) or pool.get((key[0], -1)) or []
        if not cands:
            # fall back to any bar in the same hour, any ATR bucket
            cands = [b.ts_ms for j, b in enumerate(es5)
                     if et_hour(b.ts_ms) == key[0] and atr[j]]
        if cands:
            triggers.append(rng.choice(cands))
    return triggers


# ---------------------------------------------------------------------------
# Reporting helpers
# ---------------------------------------------------------------------------


def summarize(trades: list[dict]) -> dict:
    rs = [t["r_outcome"] for t in trades]
    n = len(rs)
    return {
        "n": n,
        "win": (sum(1 for r in rs if r > 0) / n) if n else float("nan"),
        "er": stats.fmean(rs) if n else float("nan"),
        "sessions": len({t["session_date"] for t in trades}),
        "sumR": sum(rs),
    }


def print_row(name: str, s: dict) -> None:
    print(f"  {name:<26} n={s['n']:>3}  win%={s['win']*100:>5.1f}  "
          f"E[R]={s['er']:+.3f}  sumR={s['sumR']:+6.2f}  sessions={s['sessions']:>2}")


# ---------------------------------------------------------------------------
# Microstructure event-study around the REAL break
# ---------------------------------------------------------------------------


def microstructure_window(c: sqlite3.Connection, t0: int, t1: int) -> dict | None:
    """Mean OFI, sell-imbalance (aggressive_sell - aggressive_buy) and microprice
    deviation (bp) for ES samples in [t0, t1). None if no samples in-window."""
    rows = c.execute(
        "SELECT ofi_window, aggressive_buy_volume, aggressive_sell_volume, "
        "microprice_deviation_bp FROM microstructure_sample "
        "WHERE symbol='ES' AND timestamp_ms >= ? AND timestamp_ms < ? "
        "ORDER BY timestamp_ms", (t0, t1),
    ).fetchall()
    if not rows:
        return None
    ofi = [r[0] for r in rows if r[0] is not None]
    imb = [(r[2] or 0.0) - (r[1] or 0.0) for r in rows]      # sell - buy (>0 = sell pressure)
    mpd = [r[3] for r in rows if r[3] is not None]
    return {
        "n": len(rows),
        "ofi": stats.fmean(ofi) if ofi else float("nan"),
        "sell_imb": stats.fmean(imb) if imb else float("nan"),
        "mpd": stats.fmean(mpd) if mpd else float("nan"),
    }


def microstructure_eventstudy(c: sqlite3.Connection, real_trades: list[dict]) -> None:
    print(f"\n{'='*72}\nMICROSTRUCTURE EVENT-STUDY around the REAL VWAP loss (ES samples)\n{'='*72}")
    print("  baseline = [break-30min, break)   post = [break, break+15min)")
    pre_rows, post_rows = [], []
    used = 0
    for t in real_trades:
        bts = t["timestamp_ms"] + TF * 60_000      # the break is the entry instant (5-min close)
        pre = microstructure_window(c, bts - 30 * 60_000, bts)
        post = microstructure_window(c, bts, bts + 15 * 60_000)
        if pre is None or post is None:
            continue
        used += 1
        pre_rows.append(pre)
        post_rows.append(post)

    if not post_rows:
        print("  (no microstructure samples overlap the real break windows)")
        return

    def avg(rows, k):
        vals = [r[k] for r in rows if not math.isnan(r[k])]
        return stats.fmean(vals) if vals else float("nan")

    pre_ofi, post_ofi = avg(pre_rows, "ofi"), avg(post_rows, "ofi")
    pre_imb, post_imb = avg(pre_rows, "sell_imb"), avg(post_rows, "sell_imb")
    pre_mpd, post_mpd = avg(pre_rows, "mpd"), avg(post_rows, "mpd")

    print(f"  events with coverage: {used}/{len(real_trades)}")
    print(f"  {'metric':<28}{'baseline':>12}{'post-break':>14}{'delta':>12}")
    print(f"  {'mean OFI (window)':<28}{pre_ofi:>12.2f}{post_ofi:>14.2f}{post_ofi-pre_ofi:>+12.2f}")
    print(f"  {'mean sell-buy imbalance':<28}{pre_imb:>12.2f}{post_imb:>14.2f}{post_imb-pre_imb:>+12.2f}")
    print(f"  {'mean microprice dev (bp)':<28}{pre_mpd:>12.3f}{post_mpd:>14.3f}{post_mpd-pre_mpd:>+12.3f}")

    # Verdict: distribution-continuation requires BOTH (a) flow drifting sell-ward
    # vs baseline AND (b) the ABSOLUTE post-break flow actually being sell-side.
    # A relative drift on an economically trivial scale (or while OFI stays firmly
    # buy-initiated) is NOT continuation. OFI sign convention here: positive =
    # buy-initiated (it tracks aggressive_buy in this dataset), so sell-side = OFI
    # falling / going negative. Imbalance is sell-buy, so sell-side = positive.
    ofi_rel = post_ofi < pre_ofi                   # OFI drops vs baseline (more sell-initiated)
    imb_rel = post_imb > pre_imb                   # sell-buy imbalance rises vs baseline
    mpd_rel = post_mpd < pre_mpd                   # microprice leans further below last trade
    rel_votes = sum([ofi_rel, imb_rel, mpd_rel])
    # absolute sell-side: net sell-initiated flow on a non-trivial scale
    abs_sell = (post_ofi < 0) or (post_imb > 0)
    if rel_votes >= 2 and abs_sell:
        verdict = ("SUPPORTS distribution-continuation -- post-break flow is both more "
                   "sell-ward than baseline AND net sell-initiated (mechanically sold into).")
    elif rel_votes >= 2:
        verdict = ("MIXED / WEAK -- flow drifts sell-ward vs baseline, but the ABSOLUTE "
                   "post-break flow is still net BUY-initiated (OFI {:+.0f}, imbalance {:+.1f}); "
                   "the level is not being aggressively sold, so continuation support is weak."
                   ).format(post_ofi, post_imb)
    else:
        verdict = ("DOES NOT support distribution-continuation -- post-break flow is not "
                   "more sell-side than baseline; the break is not driven by fresh selling.")
    print(f"\n  VERDICT: {verdict}")


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------


def main() -> None:
    c = sqlite3.connect(DB)
    es1 = load_es_rth(c)
    es5 = dale.resample(es1, TF)
    atr = _atr(es5)
    imap = {b.ts_ms: i for i, b in enumerate(es5)}
    ts1 = [b.ts_ms for b in es1]
    sk = _session_keys(es5)
    sessions = sorted(set(sk))

    print(f"\n{'='*72}")
    print("VWAP PLACEBO EVENT-STUDY  (ES RTH 5-min; conservative shadow exit)")
    print(f"{'='*72}")
    print(f"  data: {len(es1)} 1-min bars -> {len(es5)} 5-min bars over {len(sessions)} sessions")
    print(f"  sessions: {sessions}")
    print(f"  exit (ALL sets): next-1m-open entry, stop max(1xATR,{MIN_STOP_PTS:g}), "
          f"target 2xATR, adverse-first, 1-trade-at-a-time, friction {FRICTION_PTS:g}pt")

    # --- REAL signal (frozen scorer, exact) ---
    real_trades = score_session(es1)
    # decorate with nothing extra; score_session already gives session_date/r_outcome/win
    real_for_micro = [{"timestamp_ms": t["timestamp_ms"], "session_date": t["session_date"]}
                      for t in real_trades]

    # --- Placebo anchors -> trigger bars -> identical scoring ---
    a_triggers = _held_above_then_lost(es5, sk, anchor_session_vwap(es5), require_hold=False)
    b_triggers = _held_above_then_lost(es5, sk, anchor_sma(es5, 20), require_hold=True)

    # C: average over several random offsets so we don't cherry-pick one seed
    c_runs = []
    C_SEEDS = 8
    for k in range(C_SEEDS):
        ctrig = _held_above_then_lost(es5, sk, anchor_shifted_vwap(es5, k), require_hold=True)
        c_runs.append(score_triggers(ctrig, es5, atr, imap, es1, ts1))
    # pool all offset runs (each seed is an independent placebo draw)
    c_trades = [t for run in c_runs for t in run]

    d_triggers = _held_above_then_lost(es5, sk, anchor_prior_day_vwap(es5), require_hold=True)
    e_triggers = placebo_E_random_short(
        [{"timestamp_ms": t["timestamp_ms"]} for t in real_trades], es5, atr, imap)

    a_trades = score_triggers(a_triggers, es5, atr, imap, es1, ts1)
    b_trades = score_triggers(b_triggers, es5, atr, imap, es1, ts1)
    d_trades = score_triggers(d_triggers, es5, atr, imap, es1, ts1)
    e_trades = score_triggers(e_triggers, es5, atr, imap, es1, ts1)

    # --- comparison table ---
    real_s = {
        "n": len(real_trades),
        "win": (sum(t["win"] for t in real_trades) / len(real_trades)) if real_trades else float("nan"),
        "er": stats.fmean([t["r_outcome"] for t in real_trades]) if real_trades else float("nan"),
        "sessions": len({t["session_date"] for t in real_trades}),
        "sumR": sum(t["r_outcome"] for t in real_trades),
    }
    print(f"\n{'-'*72}\nCOMPARISON  (hypothesis: REAL must BEAT every placebo)\n{'-'*72}")
    print_row("REAL vwap_loss_short", real_s)
    print_row("A no-15min-hold VWAP loss", summarize(a_trades))
    print_row("B SMA(20) loss w/ hold", summarize(b_trades))
    print_row(f"C random-shifted VWAP (x{C_SEEDS})", summarize(c_trades))
    print_row("D prior-day VWAP loss", summarize(d_trades))
    print_row("E time+ATR-matched random", summarize(e_trades))

    # --- verdict ---
    placebos = {
        "A no-hold VWAP": summarize(a_trades),
        "B SMA(20)": summarize(b_trades),
        "C shifted VWAP": summarize(c_trades),
        "D prior-day VWAP": summarize(d_trades),
        "E time/ATR random": summarize(e_trades),
    }
    real_er = real_s["er"]
    # A placebo only carries weight if it has enough trades to be a real comparison.
    # A and D are structurally capped (one cross per session) -> often n<MIN_N; we
    # report them but don't let a 2-4 trade placebo overturn the real signal.
    MIN_N = 6
    powered = {name: s for name, s in placebos.items()
               if s["n"] >= MIN_N and not math.isnan(s["er"])}
    beaten_by = [n for n, s in powered.items() if s["er"] >= real_er]
    matched = [n for n, s in powered.items() if abs(s["er"] - real_er) < 0.15]
    thin_beat = [n for n, s in placebos.items()
                 if 0 < s["n"] < MIN_N and not math.isnan(s["er"]) and s["er"] >= real_er]

    print(f"\n{'-'*72}\nVERDICT\n{'-'*72}")
    print(f"  REAL E[R] = {real_er:+.3f} over {real_s['n']} trades / {real_s['sessions']} sessions")
    print(f"  (well-powered placebos, n>={MIN_N}: {', '.join(powered) or 'none'}; "
          f"thin placebos shown for context only)")
    if beaten_by:
        print(f"  Well-powered placebos matching or BEATING the real signal: {', '.join(beaten_by)}")
        print("  => VWAP looks DECORATIVE: a level with no special standing reproduces the")
        print("     edge at comparable sample size, so the signal is most consistent with")
        print("     generic intraday momentum / time-of-day, not anything causal about VWAP.")
    elif matched:
        print(f"  No well-powered placebo beats it, but these are within 0.15R: {', '.join(matched)}")
        print("  => WEAK / DECORATIVE-LEANING: VWAP only edges out same-mechanism controls")
        print("     (esp. the random-shifted VWAP) by a hair -- not enough to call the live")
        print("     VWAP level causal vs generic momentum at this sample size.")
        if thin_beat:
            print(f"     (thin placebos {', '.join(thin_beat)} also >= real, but n<{MIN_N} so not decisive)")
    else:
        print("  No well-powered placebo matches or beats the real signal.")
        print("  => CONSISTENT WITH CAUSAL VWAP: an SMA level, a mis-timed VWAP, yesterday's")
        print("     VWAP, and a time/vol-matched random short all do clearly WORSE -- the")
        print("     specific live VWAP level appears to carry signal beyond momentum/timing.")
        if thin_beat:
            print(f"     (caveat: thin placebos {', '.join(thin_beat)} scored >= real on n<{MIN_N})")
    print(f"  (caveat: only ~{real_s['sessions']} independent sessions; directional, not proof.)")

    # --- microstructure mechanism check ---
    microstructure_eventstudy(c, real_for_micro)
    c.close()


if __name__ == "__main__":
    main()

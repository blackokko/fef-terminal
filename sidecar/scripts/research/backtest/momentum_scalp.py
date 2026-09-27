"""MOMENTUM SCALP — single-leg long 0DTE, CHASE-THE-THRUST entry, FIRST-TOUCH 5-10min exit.

The operator wants a SCALP bot (not the +30min RIDE, which lost): enter in the direction of a
move ALREADY going, ride 5-10 min MAX, first-touch exit. Archetype = 7490P bought @2.10 as SPX
flushed into the close, sold @~7 in 3m44s (option tripled). QUESTION: does a tight first-touch
scalp NET POSITIVE after paying the FULL option spread EVERY trade -- or does chasing-the-thrust +
spread-per-trade still lose even as a scalp (confirming the RIDE-is-a-loser finding)?

RULER (no self-reference): the INDEPENDENT IBKR SPX 1-min cash bars live in signal_log.db
historical_bar(symbol='SPX','1 min') -- VERIFIED byte-identical (max close diff 0.0) to the
ibkr_spx_1m.db ruler used by every other test, but WITH OHLC, so it gives the intra-minute HIGH/LOW
for the wick bound. This is the real SPX cash index, INDEPENDENT of the ThetaData option chain =>
NO parity-spot-reconstruction artifact (MEMORY [[validate-against-independent-price]]). We NEVER use
data.Session.spot() (parity) as the ruler.

ENTRY (momentum, not prediction): rising-edge thrust -- SPX close moved >= K pt over the last M min
on the ruler (grid K in {4,6,8}, M in {2,3,5}). Up-thrust -> buy CALL, down-thrust -> buy PUT.
Debounced (must reset below K before re-arming) so each thrust = one episode. Enter ONE MARK LATER
than the signal bar (ENTRY_LAT_MIN=1) at the REAL NBBO ASK+slip (causal; you chase, no lookahead).
STRIKES: ATM (nearest listed) and ~0.30-delta OTM (solved from entry ATM IV). GATES: all / VIX_prev
>=20 / first-hour(09:30-10:30) / last-hour(15:00-16:00) / first-or-last -- the high-magnitude windows.

EXIT (first-touch, whichever comes first): profit target (+30/50/100% of entry premium), tight stop
(-30/-50%), OR time-stop (5/8/10 min). Real NBBO: sell BID-slip; pay FULL spread + slip on entry AND
exit, every scalp; +$1.30 RT commission.

THE 1-MIN BLUR (quantified as a BOUND -- the whole point):
  (a) CLOSE-only  (conservative/laggy): triggers & fills use the 1-min mark BID only. You cannot sell
      mid-minute, so you MISS the intra-minute profit wick (the scalp's whole edge). Model-free.
  (b) HIGH/LOW    (optimistic): the profit target also fires INTRA-minute via the SPX 1-min HIGH/LOW
      repriced through the option's BSM delta (opt_high ~= bid + |delta|*favorable_SPX_wick); fill AT
      the target. Stops stay on the mark (dodge recoverable down-wicks). "You catch the wick."
  (c) MID: per-trade average of (a) and (b).
  The (a)->(b) GAP = the tick-resolution uncertainty. If it's large, that's the case for ThetaData tick.

METRICS per config (K,M,strike,target,stop,tmax,gate) x {a,b,c}, DEV/HOLDOUT/ALL, by-year sign:
  mean & median net $/scalp AND net %, win%, avg hold-min, trades/session, TOTAL spread+slip paid,
  worst scalp, max drawdown, exit-reason mix. Session-clustered where 1 trade/session; else plain.

Read-only. Run: .venv/Scripts/python.exe -m scripts.research.backtest.momentum_scalp [START END]
CSVs -> scratchpad.
"""
from __future__ import annotations

import csv
import datetime as dt
import os
import sqlite3
import sys
from collections import defaultdict

import numpy as np

from .data import BAD, ET, Session, connect
from .exits import COMMISSION_RT, SLIP
from .greeks import greeks, implied_vol
from .holdout import DEV_END, HOLDOUT_END, HOLDOUT_START

sys.stdout.reconfigure(encoding="utf-8")

SCRATCH = (r"D:\fef-data")
SIG_DB = os.path.expanduser("~/.fef-terminal/signal_log.db")
YEAR_MIN = 365.0 * 24 * 60
GLITCH = 0.05
ENTRY_LAT_MIN = 1              # enter one option mark AFTER the signal bar (causal, you chase)
ENTRY_TOL_MS = 150_000        # option entry mark must be within 2.5 min of target time
PREM_FLOOR = 0.05             # skip if entry ask <= this (junk)

# ------- grid -------
KS = [4.0, 6.0, 8.0]
MS = [2, 3, 5]
STRIKES = ["ATM", "OTM30"]
TARGETS = [0.30, 0.50, 1.00]
STOPS = [0.30, 0.50]
TMAXS = [5, 8, 10]
GATES = ["all", "VIX>=20", "first60", "last60", "first_or_last"]
QUICK = os.environ.get("SCALP_QUICK") == "1"
if QUICK:
    KS = [4.0, 8.0]; MS = [2, 5]; TARGETS = [0.50, 1.00]; STOPS = [0.50]; TMAXS = [8]


# ================================================================ ruler (IBKR SPX OHLC)
def load_ruler():
    """date -> (ts[], o[], h[], l[], c[]) RTH from historical_bar SPX 1-min (== IBKR ruler + OHLC).
    Glitch-filtered to a per-session median +/-5% band (same as ibkr_series)."""
    c = sqlite3.connect(f"file:{SIG_DB}?mode=ro", uri=True)
    rows = c.execute("SELECT timestamp_ms,open,high,low,close FROM historical_bar "
                     "WHERE symbol='SPX' AND bar_size='1 min' ORDER BY timestamp_ms").fetchall()
    c.close()
    by = defaultdict(list)
    for ts, o, h, l, cl in rows:
        d = dt.datetime.fromtimestamp(ts / 1000, ET).strftime("%Y-%m-%d")
        by[d].append((ts, o, h, l, cl))
    out = {}
    for d, rs in by.items():
        rs.sort()
        ts = np.array([r[0] for r in rs], np.int64)
        o = np.array([r[1] for r in rs], float); h = np.array([r[2] for r in rs], float)
        l = np.array([r[3] for r in rs], float); cl = np.array([r[4] for r in rs], float)
        med = np.median(cl); m = np.abs(cl - med) <= GLITCH * med
        if m.sum() < 30:
            continue
        out[d] = (ts[m], o[m], h[m], l[m], cl[m])
    return out


def load_vix_prev():
    c = sqlite3.connect(f"file:{SIG_DB}?mode=ro", uri=True)
    rows = c.execute("SELECT timestamp_ms, close FROM historical_bar "
                     "WHERE symbol='VIX' AND bar_size='1 day' ORDER BY timestamp_ms").fetchall()
    c.close()
    by = {}
    for ts, cl in rows:
        by[dt.datetime.fromtimestamp(ts / 1000, ET).strftime("%Y-%m-%d")] = float(cl)
    dates = sorted(by)
    import bisect

    def prev(d):
        i = bisect.bisect_left(dates, d)
        return by[dates[i - 1]] if i > 0 else None
    return prev


# ================================================================ thrust episodes (rising edge)
def thrust_episodes(ts, cl, K, M):
    """Rising-edge debounced thrusts: first minute |close[i]-close[i-Mmin]| >= K, in a direction.
    Returns [(i, ts_i, dir)] with dir +1 up (CALL) / -1 down (PUT). Re-arms only after cond resets."""
    out = []
    armed = True
    Mms = M * 60_000
    for i in range(len(ts)):
        back_ts = ts[i] - Mms
        j = int(np.searchsorted(ts, back_ts, "right")) - 1
        if j < 0:
            continue
        if abs(int(ts[j]) - int(back_ts)) > 90_000:   # need a bar ~M min back
            continue
        dm = cl[i] - cl[j]
        up = dm >= K
        dn = dm <= -K
        if not (up or dn):
            armed = True
            continue
        if armed:
            out.append((i, int(ts[i]), 1 if up else -1))
            armed = False
        # stay disarmed until cond resets to False
    return out


def ruler_at(ts, cl, target, tol=90_000):
    j = int(np.searchsorted(ts, target))
    best, bd = None, tol + 1
    for c in (j - 1, j):
        if 0 <= c < len(ts):
            dd = abs(int(ts[c]) - target)
            if dd <= tol and dd < bd:
                bd, best = dd, c
    return best


# ================================================================ option helpers
def mid_of(q):
    if not q or q[2] in BAD or q[0] is None or q[1] is None or q[1] <= 0:
        return None
    return (q[0] + q[1]) / 2.0


def atm_iv(sess, ts, k_atm, S, T):
    ivs = []
    for right in ("C", "P"):
        m = mid_of(sess.quote(ts, k_atm, right))
        if m is None:
            continue
        iv = implied_vol(m, S, k_atm, T, right)
        if iv:
            ivs.append(iv)
    return float(np.mean(ivs)) if ivs else None


def pick_atm(strikes_here, S):
    return min(strikes_here, key=lambda k: abs(k - S)) if strikes_here else None


def pick_delta(strikes_here, sess, ts, S, right, iv, T, target=0.30):
    if iv is None or iv <= 0:
        return None
    best, bd = None, 1e9
    for k in strikes_here:
        if right == "C" and k <= S:
            continue
        if right == "P" and k >= S:
            continue
        if mid_of(sess.quote(ts, k, right)) is None:
            continue
        g = greeks(S, k, T, iv, right)
        if not g:
            continue
        d = abs(abs(g["delta"]) - target)
        if d < bd:
            bd, best = d, k
    return best


def et_hm(ts):
    d = dt.datetime.fromtimestamp(ts / 1000, ET)
    return d.hour + d.minute / 60.0


# ================================================================ per-entry option path
def build_path(sess, ruler, d, entry_mark, k, right, iv0, tmax_max=10):
    """Marks from entry_mark+1 to entry_mark+tmax_max min: (t, bid, dmin, wick_high_add).
    wick_high_add = |delta(t)| * favorable_SPX_wick(t) [added to bid for the optimistic fill]."""
    rt, ro, rh, rl, rc = ruler[d]
    hi = entry_mark + tmax_max * 60_000
    path = []
    for t in sess.minutes:
        if t <= entry_mark or t > hi + 1:
            continue
        q = sess.quote(t, k, right)
        if not q or q[2] in BAD or q[0] is None:
            continue
        bid = float(q[0])
        dmin = (t - entry_mark) / 60_000.0
        # wick add (optimistic): reprice the intra-minute SPX high/low through BSM delta
        wadd = 0.0
        ri = ruler_at(rt, rc, t)
        if ri is not None and iv0 and iv0 > 0:
            S_t = float(rc[ri])
            mins_exp = max(0.5, (et_ms_from(d, 16, 0) - t) / 60_000.0)
            T_t = mins_exp / YEAR_MIN
            g = greeks(S_t, k, T_t, iv0, right)
            if g:
                if right == "C":
                    fav = max(0.0, float(rh[ri]) - S_t)
                else:
                    fav = max(0.0, S_t - float(rl[ri]))
                wadd = abs(g["delta"]) * fav
        path.append((int(t), bid, dmin, wadd))
    return path


def et_ms_from(d, h, m):
    y, mo, dd = map(int, d.split("-"))
    return int(dt.datetime(y, mo, dd, h, m, tzinfo=ET).timestamp() * 1000)


# ================================================================ first-touch exit sim
def sim_exit(ask0, path, g, s, tmax, fill):
    """First-touch exit. Returns (pnl_per_contract $, net_pct, hold_min, reason).
    ask0 = raw entry ask (pt). cost = ask0+SLIP. targets/stops on RAW bid vs ask0."""
    cost = ask0 + SLIP
    tgt = ask0 * (1.0 + g)
    stp = ask0 * (1.0 - s)
    last = None
    for (t, bid, dmin, wadd) in path:
        if dmin > tmax + 1e-9:
            break
        last = (t, bid, dmin)
        if fill == "b":
            # optimistic: profit fires intra-minute at the wick high, fill AT target
            if bid + wadd >= tgt:
                fill_bid = max(bid, tgt) if bid >= tgt else tgt
                return _pnl(cost, fill_bid), _pct(cost, fill_bid), dmin, "target"
            if bid <= stp:
                return _pnl(cost, bid), _pct(cost, bid), dmin, "stop"
        else:  # "a" close-only
            if bid >= tgt:
                return _pnl(cost, bid), _pct(cost, bid), dmin, "target"
            if bid <= stp:
                return _pnl(cost, bid), _pct(cost, bid), dmin, "stop"
    if last is None:
        return None
    t, bid, dmin = last
    return _pnl(cost, bid), _pct(cost, bid), dmin, "time"


def _pnl(cost, fill_bid):
    proceeds = max(0.0, fill_bid - SLIP)
    return (proceeds - cost) * 100.0 - COMMISSION_RT


def _pct(cost, fill_bid):
    proceeds = max(0.0, fill_bid - SLIP)
    return (proceeds - cost) / cost if cost > 0 else float("nan")


# ================================================================ build all trades
def build_trades(start, end):
    ruler = load_ruler()
    prev_vix = load_vix_prev()
    con = connect()
    days = [r[0] for r in con.execute(
        "SELECT DISTINCT session_date FROM option_quote_history "
        "WHERE session_date BETWEEN ? AND ? ORDER BY session_date", (start, end))]

    trades = []            # one row per (entry, strike) with cached path + entry meta
    n_sess = 0
    for di, d in enumerate(days):
        if di % 100 == 0:
            print(f"    build_trades: session {di}/{len(days)} ({d})  trades so far={len(trades)}", flush=True)
        if d not in ruler:
            continue
        rt, ro, rh, rl, rc = ruler[d]
        sess = Session(con, d)
        if sess.empty():
            continue
        n_sess += 1
        vixp = prev_vix(d)
        split = "DEV" if d <= DEV_END else "HOLD"
        # collect entries across the (K,M) grid; key by (entry_mark,dir) to load a path once/strike
        entries = {}   # (entry_mark, dir) -> set of (K,M)
        for K in KS:
            for M in MS:
                for (i, ts_i, dirn) in thrust_episodes(rt, rc, K, M):
                    sig_time = ts_i                       # ruler close of bar i ~ ts_i+60s known
                    want = sig_time + ENTRY_LAT_MIN * 60_000
                    em = sess.minute_at_or_after(want)
                    if em is None or abs(em - want) > ENTRY_TOL_MS:
                        continue
                    entries.setdefault((em, dirn), set()).add((K, M))
        for (em, dirn), kms in entries.items():
            right = "C" if dirn > 0 else "P"
            ri = ruler_at(rt, rc, em)
            if ri is None:
                continue
            S = float(rc[ri])
            mins_exp = max(1.0, (et_ms_from(d, 16, 0) - em) / 60_000.0)
            T = mins_exp / YEAR_MIN
            strikes_here = [k for k in sess.strikes if sess.quote(em, k, right)
                            and sess.quote(em, k, right)[2] not in BAD]
            if not strikes_here:
                continue
            k_atm = pick_atm(strikes_here, S)
            iv0 = atm_iv(sess, em, k_atm, S, T) if k_atm else None
            for sname in STRIKES:
                if sname == "ATM":
                    k = k_atm
                else:
                    k = pick_delta(strikes_here, sess, em, S, right, iv0, T, 0.30)
                if k is None:
                    continue
                q = sess.quote(em, k, right)
                if not q or q[2] in BAD or q[1] is None or q[1] <= 0:
                    continue
                ask0 = float(q[1]); bid0 = float(q[0]) if q[0] is not None else 0.0
                if ask0 <= PREM_FLOOR:
                    continue
                path = build_path(sess, ruler, d, em, k, right, iv0)
                if not path:
                    continue
                # max favorable excursion over the FULL 10-min window (did the option "triple"?)
                maxbid = max(b for (_, b, _, _) in path)
                maxfav = maxbid / ask0 - 1.0
                tod = et_hm(em)
                trades.append(dict(
                    date=d, split=split, year=d[:4], entry_ms=em, side=("CALL" if dirn > 0 else "PUT"),
                    strike=sname, K_M=list(kms), ask0=ask0, spread=(ask0 - bid0), tod=tod,
                    vix_prev=vixp, iv0=iv0, k=k, S=S, maxfav=maxfav, path=path))
    con.close()
    return trades, n_sess, len(days)


# ================================================================ aggregation (efficient)
def gate_ok(tr, gate):
    if gate == "all":
        return True
    if gate == "VIX>=20":
        return tr["vix_prev"] is not None and tr["vix_prev"] >= 20
    if gate == "first60":
        return 9.5 <= tr["tod"] < 10.5
    if gate == "last60":
        return 15.0 <= tr["tod"] < 16.0
    if gate == "first_or_last":
        return (9.5 <= tr["tod"] < 10.5) or (15.0 <= tr["tod"] < 16.0)
    return False


def max_dd(seq_pnls_time):
    """seq = list of (entry_ms, pnl) -> max peak-to-trough drawdown of cum P&L ($)."""
    seq = sorted(seq_pnls_time)
    cum = peak = dd = 0.0
    for _, p in seq:
        cum += p
        peak = max(peak, cum)
        dd = min(dd, cum - peak)
    return dd


def compute_stats(records, fill):
    """records: list of (pnl_a,pnl_b,pct_a,pct_b,hold_a,hold_b,spread$,ts,reason_a,reason_b).
    fill in {a,b,c}. Returns a full stat dict or None."""
    if not records:
        return None
    if fill == "a":
        pnl = np.array([r[0] for r in records]); pct = np.array([r[2] for r in records])
        hold = np.array([r[4] for r in records]); reasons = [r[8] for r in records]
    elif fill == "b":
        pnl = np.array([r[1] for r in records]); pct = np.array([r[3] for r in records])
        hold = np.array([r[5] for r in records]); reasons = [r[9] for r in records]
    else:  # c = per-trade average of a and b
        pnl = np.array([0.5 * (r[0] + r[1]) for r in records])
        pct = np.array([0.5 * (r[2] + r[3]) for r in records])
        hold = np.array([0.5 * (r[4] + r[5]) for r in records]); reasons = [r[9] for r in records]
    spreads = np.array([r[6] for r in records])
    seq = [(r[7], p) for r, p in zip(records, pnl)]
    n = len(pnl); sd = pnl.std(ddof=1) if n > 1 else 0.0
    return dict(
        n=n, mean=float(pnl.mean()), median=float(np.median(pnl)),
        meanpct=float(pct.mean() * 100), medpct=float(np.median(pct) * 100),
        win=100.0 * float(np.mean(pnl > 0)), hold=float(hold.mean()),
        worst=float(pnl.min()), total=float(pnl.sum()), spread=float(spreads.sum()),
        dd=max_dd(seq), t=float(pnl.mean() / (sd / np.sqrt(n))) if sd > 0 else float("nan"),
        rmix={r: reasons.count(r) for r in ("target", "stop", "time")})


def aggregate_grid(trades):
    """Single efficient pass structure: for each (g,s,tmax) exit combo, one sweep over trades,
    collecting per-(K,M,strike,gate,split) records, then stats for fills a/b/c.
    Returns RESULT[(K,M,strike,g,s,tmax,gate,split,fill)] = statdict, for split in DEV/HOLD/ALL."""
    RESULT = {}
    combos = [(g, s, tm) for g in TARGETS for s in STOPS for tm in TMAXS]
    for ci, (g, s, tm) in enumerate(combos):
        buckets = defaultdict(list)   # (K,M,strike,gate,split) -> records
        for tr in trades:
            ra = sim_exit(tr["ask0"], tr["path"], g, s, tm, "a")
            rb = sim_exit(tr["ask0"], tr["path"], g, s, tm, "b")
            if ra is None or rb is None:
                continue
            rec = (ra[0], rb[0], ra[1], rb[1], ra[2], rb[2],
                   (tr["spread"] + 2 * SLIP) * 100.0, tr["entry_ms"], ra[3], rb[3])
            strike = tr["strike"]; split = tr["split"]
            for (K, M) in tr["K_M"]:
                for gate in GATES:
                    if not gate_ok(tr, gate):
                        continue
                    buckets[(K, M, strike, gate, split)].append(rec)
        # collapse into RESULT (DEV, HOLD, ALL) x fills
        keys = {(K, M, st, ga) for (K, M, st, ga, _sp) in buckets}
        for (K, M, st, ga) in keys:
            dev = buckets.get((K, M, st, ga, "DEV"), [])
            hold = buckets.get((K, M, st, ga, "HOLD"), [])
            allr = dev + hold
            for split, recs in (("DEV", dev), ("HOLD", hold), ("ALL", allr)):
                for fill in ("a", "b", "c"):
                    stt = compute_stats(recs, fill)
                    if stt is not None:
                        RESULT[(K, M, st, g, s, tm, ga, split, fill)] = stt
        print(f"    exit-combo {ci+1}/{len(combos)} (+{int(g*100)}%/-{int(s*100)}%/{tm}m) done", flush=True)
    return RESULT


def by_year_mean(trades, K, M, strike, g, s, tm, gate, year, fill):
    """Targeted per-year mean net $ (for the by-year sign table only)."""
    vals = []
    for tr in trades:
        if tr["strike"] != strike or tr["year"] != year or (K, M) not in tr["K_M"] or not gate_ok(tr, gate):
            continue
        ra = sim_exit(tr["ask0"], tr["path"], g, s, tm, "a")
        rb = sim_exit(tr["ask0"], tr["path"], g, s, tm, "b")
        if ra is None or rb is None:
            continue
        vals.append(ra[0] if fill == "a" else (rb[0] if fill == "b" else 0.5 * (ra[0] + rb[0])))
    if not vals:
        return None
    return float(np.mean(vals)), len(vals)


# ================================================================ reporting
def block(t):
    print("\n" + "=" * 120)
    print(t)
    print("=" * 120)


def main():
    args = sys.argv[1:]
    start = args[0] if len(args) > 0 else "2022-01-03"
    end = args[1] if len(args) > 1 else HOLDOUT_END
    print("=" * 120)
    print("MOMENTUM SCALP — single-leg long 0DTE, chase-the-thrust, FIRST-TOUCH 5-10min exit")
    print(f"  window {start}..{end}   friction: FULL spread + ${SLIP}/side slip + ${COMMISSION_RT} RT commission, EVERY scalp")
    print(f"  ruler = INDEPENDENT IBKR SPX 1-min OHLC (historical_bar SPX == ibkr_spx_1m.db, no parity spot)")
    print(f"  entry = rising-edge thrust (>= K pt / M min), enter +{ENTRY_LAT_MIN} mark at NBBO ask+slip (you chase)")
    print("=" * 120)

    trades, n_sess, n_days = build_trades(start, end)
    nd = sum(t["split"] == "DEV" for t in trades); nh = sum(t["split"] == "HOLD" for t in trades)
    print(f"\n  sessions loaded: {n_sess}/{n_days}   entry-legs (entry x strike): {len(trades)}  (DEV {nd} / HOLD {nh})")
    # entry-premium & spread hurdle context (ATM)
    atm = [t for t in trades if t["strike"] == "ATM"]
    if atm:
        pr = np.array([t["ask0"] for t in atm]); sp = np.array([t["spread"] for t in atm])
        print(f"  ATM entry ask median {np.median(pr):.2f}pt (${np.median(pr)*100:.0f}/ct)  "
              f"NBBO spread median {np.median(sp):.2f}pt (${np.median(sp)*100:.0f}/ct)  "
              f"=> round-trip spread+slip hurdle ~= ${(np.median(sp)+2*SLIP)*100:.0f}/ct EVERY scalp")
        mf = np.array([t["maxfav"] for t in atm])
        print(f"  ATM max-favorable-excursion in 10min: median {100*np.median(mf):+.0f}%  "
              f"P(>=+50%)={100*np.mean(mf>=0.5):.0f}%  P(>=+100% i.e. 2x)={100*np.mean(mf>=1.0):.0f}%  "
              f"P(>=+200% i.e. 3x)={100*np.mean(mf>=2.0):.0f}%  (the raw upside a perfect exit could catch)")

    print("\n  aggregating grid (precompute per-trade exits once per exit-combo)...", flush=True)
    R = aggregate_grid(trades)

    def get(K, M, strike, g, s, tm, gate, split, fill):
        return R.get((K, M, strike, g, s, tm, gate, split, fill))

    G, S_, TM = 0.50, 0.50, 8
    K0, M0 = KS[len(KS) // 2], MS[len(MS) // 2]

    # ---------- HEADLINE 3-fill BAND across K x M x strike ----------
    block(f"HEADLINE — net $/scalp BAND across the 1-min fill assumptions   "
          f"[target +{G:.0%} / stop -{S_:.0%} / tmax {TM}min / gate=all, ALL years]")
    print(f"  {'K/M':>7s} {'strike':6s} | {'(a)close net$ (t,n)':>22s} | {'(c)mid net$':>12s} | "
          f"{'(b)wick net$ (t,n)':>22s} | {'win%a/b':>9s} {'hold_b':>6s} {'reasons(b) tgt/stp/tm':>22s}")
    for K in KS:
        for M in MS:
            for strike in STRIKES:
                a = get(K, M, strike, G, S_, TM, "all", "ALL", "a")
                b = get(K, M, strike, G, S_, TM, "all", "ALL", "b")
                c = get(K, M, strike, G, S_, TM, "all", "ALL", "c")
                if a is None or b is None:
                    continue
                rm = b["rmix"]
                print(f"  {int(K)}/{M:<5d} {strike:6s} | {a['mean']:+9.1f}(t{a['t']:+4.1f},{a['n']:4d}) | "
                      f"{c['mean']:+10.1f} | {b['mean']:+9.1f}(t{b['t']:+4.1f},{b['n']:4d}) | "
                      f"{a['win']:3.0f}/{b['win']:3.0f}% {b['hold']:5.1f} | "
                      f"{rm['target']:4d}/{rm['stop']:4d}/{rm['time']:4d}")

    # ---------- EXIT GRID at a representative entry ----------
    block(f"EXIT GRID — K={int(K0)}/M={M0} ATM gate=all ALL: sweep target x stop x tmax (net $/scalp, 3 fills a|c|b)")
    print(f"  {'tgt':>4s} {'stop':>4s} {'tmax':>4s} | {'(a)close':>9s} {'(c)mid':>9s} {'(b)wick':>9s} | "
          f"{'win%b':>5s} {'n':>4s} {'tgt/stp/tm (b)':>16s}")
    for g in TARGETS:
        for s in STOPS:
            for tm in TMAXS:
                a = get(K0, M0, "ATM", g, s, tm, "all", "ALL", "a")
                b = get(K0, M0, "ATM", g, s, tm, "all", "ALL", "b")
                c = get(K0, M0, "ATM", g, s, tm, "all", "ALL", "c")
                if a is None or b is None:
                    continue
                rm = b["rmix"]
                print(f"  {g:+.0%} {s:>4.0%} {tm:4d} | {a['mean']:+9.1f} {c['mean']:+9.1f} {b['mean']:+9.1f} | "
                      f"{b['win']:4.0f}% {b['n']:4d} {rm['target']:4d}/{rm['stop']:4d}/{rm['time']:4d}")

    # ---------- GATES ----------
    block(f"GATES — does the magnitude gate rescue it? [K={int(K0)}/M={M0} ATM, target +{G:.0%}/stop -{S_:.0%}/tmax{TM}, ALL]")
    print(f"  {'gate':14s} | {'(a)close net$(t,n)':>21s} | {'(b)wick net$(t,n)':>21s} | {'win%a/b':>9s} {'trd/sess':>8s}")
    for gate in GATES:
        a = get(K0, M0, "ATM", G, S_, TM, gate, "ALL", "a")
        b = get(K0, M0, "ATM", G, S_, TM, gate, "ALL", "b")
        if a is None or b is None:
            print(f"  {gate:14s} | (no trades)")
            continue
        print(f"  {gate:14s} | {a['mean']:+9.1f}(t{a['t']:+4.1f},{a['n']:4d}) | "
              f"{b['mean']:+9.1f}(t{b['t']:+4.1f},{b['n']:4d}) | {a['win']:3.0f}/{b['win']:3.0f}% | {a['n']/n_sess:6.3f}")

    # ---------- DEV vs HOLDOUT + by-year sign ----------
    block("DEV vs HOLDOUT + BY-YEAR SIGN — candidate configs (net $/scalp; fill (a) conservative & (b) optimistic)")
    cands = [
        (K0, M0, "ATM", 0.50, 0.50, 8, "all"),
        (8.0, 2, "ATM", 1.00, 0.50, 8, "all"),
        (K0, M0, "OTM30", 0.50, 0.50, 8, "all"),
        (8.0, 2, "ATM", 0.50, 0.50, 8, "last60"),
        (K0, M0, "ATM", 0.30, 0.30, 5, "all"),
        (4.0, 5, "ATM", 0.50, 0.50, 10, "VIX>=20"),
    ]
    print(f"  {'config':40s} {'fill':4s} | {'DEV':>13s} {'HOLD':>13s} | "
          + " ".join(f"{y:>7s}" for y in ["2022", "2023", "2024", "2025", "2026"]))
    for (K, M, strike, g, s, tm, gate) in cands:
        lbl = f"K{int(K)}/M{M} {strike} +{int(g*100)}/-{int(s*100)}/{tm}m {gate}"
        for fill in ("a", "b"):
            dev = get(K, M, strike, g, s, tm, gate, "DEV", fill)
            hold = get(K, M, strike, g, s, tm, gate, "HOLD", fill)
            yrs = []
            for y in ["2022", "2023", "2024", "2025", "2026"]:
                yy = by_year_mean(trades, K, M, strike, g, s, tm, gate, y, fill)
                yrs.append(f"{yy[0]:+7.0f}" if yy else f"{'·':>7s}")
            dv = f"{dev['mean']:+7.1f}({dev['n']})" if dev else f"{'·':>13s}"
            hv = f"{hold['mean']:+7.1f}({hold['n']})" if hold else f"{'·':>13s}"
            tag = lbl if fill == "a" else ""
            print(f"  {tag:40s} ({fill})  | {dv:>13s} {hv:>13s} | " + " ".join(f"{v:>7s}" for v in yrs))

    # ---------- example fast-tripling scalps (mechanics sanity vs the 7490P archetype) ----------
    block("MECHANICS SANITY — top fast-tripling scalps in-window (option >=2x within 10min; the 7490P archetype)")
    ex = sorted([t for t in trades if t["strike"] == "ATM"], key=lambda t: -t["maxfav"])[:12]
    print(f"  {'date':10s} {'entry_ET':8s} {'side':4s} {'K':>7s} {'ask0':>6s} {'maxfav%':>8s} {'VIXp':>5s}")
    for t in ex:
        et = dt.datetime.fromtimestamp(t["entry_ms"] / 1000, ET).strftime("%H:%M")
        print(f"  {t['date']:10s} {et:8s} {t['side']:4s} {t['k']:7.0f} {t['ask0']:6.2f} "
              f"{100*t['maxfav']:+7.0f}% {t['vix_prev'] if t['vix_prev'] else 0:5.1f}")

    # ---------- FULL config CSV ----------
    write_csv(R, trades, n_sess)

    block("VERDICT SCAFFOLD (read the numbers above)")
    print("  NET-POSITIVE scalp requires: mean net $/scalp > 0 with |t|>~2 on BOTH the (a) conservative")
    print("  AND (b) optimistic fill, repeating across years + holding on HOLDOUT. If (a)<0<(b), the answer")
    print("  is TICK-SENSITIVE => the (a)->(b) gap sizes the case for pulling ThetaData tick. If BOTH<0,")
    print("  chasing-the-thrust + spread-per-trade loses even as a scalp (RIDE-is-a-loser confirmed).")


def write_csv(R, trades, n_sess):
    rows = []
    for (K, M, strike, g, s, tm, gate, split, fill), st in R.items():
        rm = st["rmix"]
        rows.append(dict(
            K=int(K), M=M, strike=strike, target=g, stop=s, tmax=tm,
            gate=gate, split=split, fill=fill, n=st["n"],
            mean_net=round(st["mean"], 2), median_net=round(st["median"], 2),
            mean_pct=round(st["meanpct"], 2), median_pct=round(st["medpct"], 2),
            win_pct=round(st["win"], 1), t=round(st["t"], 2) if np.isfinite(st["t"]) else "",
            hold_min=round(st["hold"], 2), trades_per_sess=round(st["n"] / n_sess, 4),
            total_net=round(st["total"], 0), total_spread_paid=round(st["spread"], 0),
            worst=round(st["worst"], 0), max_dd=round(st["dd"], 0),
            n_target=rm["target"], n_stop=rm["stop"], n_time=rm["time"]))
    p = os.path.join(SCRATCH, "momentum_scalp_configs.csv")
    cols = ["K", "M", "strike", "target", "stop", "tmax", "gate", "split", "fill", "n", "mean_net",
            "median_net", "mean_pct", "median_pct", "win_pct", "t", "hold_min", "trades_per_sess",
            "total_net", "total_spread_paid", "worst", "max_dd", "n_target", "n_stop", "n_time"]
    with open(p, "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=cols, extrasaction="ignore")
        w.writeheader()
        for r in rows:
            w.writerow(r)
    print(f"\n  config CSV -> {p}  ({len(rows)} rows)")

    # per-trade CSV for the headline config family (ATM, all gate) for inspection
    tp = os.path.join(SCRATCH, "momentum_scalp_trades_headline.csv")
    with open(tp, "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["date", "split", "entry_ET", "side", "strike_type", "K", "ask0", "spread",
                    "tod", "vix_prev", "maxfav_pct",
                    "pnl_a_50_50_8", "pnl_b_50_50_8", "reason_a", "reason_b"])
        for t in trades:
            if t["strike"] != "ATM":
                continue
            ra = sim_exit(t["ask0"], t["path"], 0.50, 0.50, 8, "a")
            rb = sim_exit(t["ask0"], t["path"], 0.50, 0.50, 8, "b")
            if ra is None or rb is None:
                continue
            et = dt.datetime.fromtimestamp(t["entry_ms"] / 1000, ET).strftime("%H:%M")
            w.writerow([t["date"], t["split"], et, t["side"], t["strike"], f"{t['k']:.0f}",
                        f"{t['ask0']:.2f}", f"{t['spread']:.2f}", f"{t['tod']:.2f}",
                        t["vix_prev"] if t["vix_prev"] else "", f"{100*t['maxfav']:.0f}",
                        f"{ra[0]:.1f}", f"{rb[0]:.1f}", ra[3], rb[3]])
    print(f"  headline trades CSV -> {tp}")


if __name__ == "__main__":
    main()

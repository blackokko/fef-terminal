"""Shared Opening-Range-Breakout (ORB) core for the SPX 0DTE research backtest.

Opening Range (OR) = high/low of the first OR_MIN minutes of RTH (09:30 ET open), built
from the INDEPENDENT IBKR SPX 1-min close ruler (ibkr_ruler; independent of the option chain
-> no spot-reconstruction artifact, per [[validate-against-independent-price]]). NOTE: the
ruler stores 1-min CLOSES only (no true per-bar high/low), so the OR band is the max/min of
1-min closes -- slightly conservative vs true intrabar H/L, but every session is treated the
same and forward moves are measured on the SAME ruler, so the comparison is clean.

Breakout (a "fresh crossing"): after the OR window, a close ABOVE OR-high -> dir=+1 (would buy
a call); a close BELOW OR-low -> dir=-1 (would buy a put). A break is a state TRANSITION into
the above/below zone, so a pullback-and-re-break counts as a new event (and consecutive bars
beyond the OR are not double-counted). first_break = the first event; all_breaks = every fresh
crossing (this is what makes whipsaw days honest -- they emit up, then down, then up ...).

Read-only. Imported by orb_direction.py / orb_monetize.py.
"""
from __future__ import annotations

import datetime as dt

from .ibkr_ruler import ET
from .thetadata_real_spot_validate import SANE_HI, SANE_LO, real_at  # noqa: F401 (re-export)

OPEN_H, OPEN_M = 9, 30
OR_WINDOWS = [15, 30]          # first-15-min and first-30-min opening ranges
FILT_BUFFER_PT = 2.0           # filtered variant: break must clear the OR band by > this
FILT_MAX_MIN = 90             # filtered variant: only breaks within the first ~90 min


def open_ms(d):
    """The 09:30 ET epoch-ms for session date d (YYYY-MM-DD)."""
    y, m, dd = map(int, d.split("-"))
    return int(dt.datetime(y, m, dd, OPEN_H, OPEN_M, tzinfo=ET).timestamp() * 1000)


def or_band(rt, rp, t_open, or_min):
    """(hi, lo, range, or_end_ms) from ruler closes in offset [0, or_min) min. None if sparse."""
    end_ms = or_min * 60_000
    seg = [float(p) for t, p in zip(rt, rp)
           if 0 <= int(t) - t_open < end_ms and SANE_LO <= p <= SANE_HI]
    if len(seg) < max(5, or_min // 2):
        return None
    hi, lo = max(seg), min(seg)
    return hi, lo, hi - lo, end_ms


def breaks(rt, rp, t_open, hi, lo, or_end_ms, *, buffer_pt=0.0, max_min=None):
    """Fresh crossings after the OR window: list of (ts_ms, dir). `buffer_pt` widens the
    inside band (break must clear OR by > buffer_pt); `max_min` caps the scan (min from open)."""
    events = []
    zone = 0                                    # -1 below, 0 inside, +1 above
    for t, p in zip(rt, rp):
        off = int(t) - t_open
        if off < or_end_ms:
            continue
        if max_min is not None and off > max_min * 60_000:
            break
        if not (SANE_LO <= p <= SANE_HI):
            continue
        if p > hi + buffer_pt:
            nz = 1
        elif p < lo - buffer_pt:
            nz = -1
        else:
            nz = 0
        if nz != 0 and nz != zone:
            events.append((int(t), float(nz)))
        zone = nz
    return events


def eod_close(rt, rp):
    """Last sane ruler close of the session (EOD forward-move endpoint)."""
    for p in reversed(rp):
        if SANE_LO <= p <= SANE_HI:
            return float(p)
    return None

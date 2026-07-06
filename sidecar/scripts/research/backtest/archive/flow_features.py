"""Intraday signed-flow features at the decision minute, from option_flow_minute.

Captures the directional pressure knowable by ~10:00 ET: net premium being lifted on
calls vs hit on puts (cumulative + recent slope) and the call/put buy imbalance. All
strictly from minutes at/before the decision (no lookahead).
"""
from __future__ import annotations


def flow_features(con, session_date, dec_ts):
    rows = con.execute(
        "SELECT ts_ms,call_buy_prem,call_sell_prem,put_buy_prem,put_sell_prem,net_signed_prem "
        "FROM option_flow_minute WHERE session_date=? AND ts_ms<=? ORDER BY ts_ms",
        (session_date, dec_ts)).fetchall()
    if not rows:
        return None
    cum = sum(r[5] for r in rows)
    cbp = sum(r[1] for r in rows)
    csp = sum(r[2] for r in rows)
    pbp = sum(r[3] for r in rows)
    psp = sum(r[4] for r in rows)
    total = cbp + csp + pbp + psp
    last15 = [r for r in rows if r[0] >= dec_ts - 15 * 60000]
    slope15 = sum(r[5] for r in last15)
    return {
        "cum_signed_flow": cum,                                  # bullish > 0
        "flow_norm": (cum / total) if total else None,           # -1..+1 directionality
        "flow_slope15": slope15,                                 # recent pressure
        "net_call_pressure": cbp - csp,                          # calls lifted - hit
        "net_put_pressure": pbp - psp,                           # puts lifted - hit
        "call_buy_ratio": (cbp / (cbp + csp)) if (cbp + csp) else None,
        "put_buy_ratio": (pbp / (pbp + psp)) if (pbp + psp) else None,
        "flow_minutes": len(rows),
    }


if __name__ == "__main__":
    from .data import Session, connect
    from .label_dir import decision_ts
    con = connect()
    for d in ("2022-06-15", "2022-09-13", "2022-11-10"):
        s = Session(con, d)
        if s.empty():
            print(d, "no quote data")
            continue
        dec = decision_ts(s)
        f = flow_features(con, d, dec)
        if not f:
            print(d, "no flow data")
            continue
        print(f"\n{d}  spot~{s.spot(dec):.0f}")
        for k, v in f.items():
            print(f"   {k:18s} {v:,.4f}" if isinstance(v, float) else f"   {k:18s} {v}")

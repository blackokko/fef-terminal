"""H7: GEX-gated 0DTE iron condor (premium selling). DEV ONLY. Sell a near-ATM condor
at ~10:00 ET; the hypothesis is that in LONG_GAMMA (positive GEX = pinning) regimes price
stays in range and the premium decays to profit, while SHORT_GAMMA (trend) regimes blow
through and lose. The GEX regime is the gate/edge. Realistic fills, hold to PM-settle, 3ct.
"""
from __future__ import annotations

from .credit import simulate_iron_condor
from .data import Session, connect, sessions
from .h1_test import line, stat
from .holdout import assert_dev, dev_range

QTY, SHORT_OFF, WING = 3, 10, 10


def regime_at(con, d, dec_ts):
    return con.execute("SELECT regime,gex FROM option_dealer_exposure "
                       "WHERE session_date=? AND ts_ms<=? ORDER BY ts_ms DESC LIMIT 1",
                       (d, dec_ts)).fetchone()


def main():
    con = connect()
    start, end = dev_range()
    assert_dev(end)
    groups = {"LONG_GAMMA": [], "SHORT_GAMMA": [], "TRANSITION": [], "all": []}
    byyear = {}
    inrange = {"LONG_GAMMA": [], "SHORT_GAMMA": []}
    for d in sessions(con, start, end):
        s = Session(con, d)
        if s.empty():
            continue
        dec = s.minute_at_or_before(s.minutes[0] + 30 * 60000)
        if dec is None:
            continue
        rg = regime_at(con, d, dec)
        if not rg:
            continue
        regime = rg[0]
        atm = s.atm(dec)
        if atm is None:
            continue
        t = simulate_iron_condor(s, dec, atm, short_off=SHORT_OFF, wing=WING, qty=QTY)
        if not t:
            continue
        groups[regime].append(t["pnl"])
        groups["all"].append(t["pnl"])
        if regime in inrange:
            inrange[regime].append(t["in_range"])
        if regime == "LONG_GAMMA":
            byyear.setdefault(d[:4], []).append(t["pnl"])
    print(f"H7: GEX-gated 0DTE iron condor (short +/-{SHORT_OFF}, wing {WING}), realistic fills, hold to close, {QTY}ct")
    print("\n--- by regime at ~10:00 ---")
    line("LONG_GAMMA (pin) [H7]", stat(groups["LONG_GAMMA"]), "  <-- sell premium when pinning")
    line("SHORT_GAMMA (trend)", stat(groups["SHORT_GAMMA"]), "  <-- should LOSE")
    line("TRANSITION", stat(groups["TRANSITION"]))
    line("regime-blind (all)", stat(groups["all"]))
    for r in ("LONG_GAMMA", "SHORT_GAMMA"):
        if inrange[r]:
            print(f"  {r} stayed-in-range-by-close: {100*sum(inrange[r])/len(inrange[r]):.0f}%")
    print("\n--- LONG_GAMMA by year ---")
    for y in sorted(byyear):
        line(f"  {y}", stat(byyear[y]))


if __name__ == "__main__":
    main()

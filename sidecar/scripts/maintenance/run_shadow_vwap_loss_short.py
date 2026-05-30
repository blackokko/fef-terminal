"""Forward-shadow runner for the vwap_loss_short candidate (Phase 1, paper).

Scores all vwap_loss_short signals in the ES RTH bars on the FROZEN definition,
upserts them to shadow_forward_signal (idempotent), and reports the FORWARD-ONLY
methodology stack (sessions after the in-sample cutoff) — the real confirm/kill.

Run end-of-day (after ES RTH bars for the session are captured by the sidecar's
es_vwap_loop, or after running backfill_es_rth_bars):
    python -m scripts.maintenance.run_shadow_vwap_loss_short
"""
import asyncio
import sqlite3
import statistics as stats
from collections import defaultdict

from fef_sidecar.db.engine import get_session, init_db
from fef_sidecar.db.models import HistoricalBar, ShadowForwardSignal
from fef_sidecar.engine.signals import shadow_vwap_loss_short as S
from fef_sidecar.engine.signals.dale import Bar
from scripts.research.dale_vwap_backtest import DB, stat_block

FORWARD_TARGET = 35  # ~mid of Chat's 30-40 forward-trade bar before live sizing


def load_es_rth(c):
    rows = c.execute(
        "SELECT timestamp_ms,open,high,low,close,volume FROM historical_bar "
        "WHERE symbol='ES' AND bar_size=? ORDER BY timestamp_ms", (S.ES_BAR_SIZE,),
    ).fetchall()
    return [Bar(ts_ms=r[0], open=r[1], high=r[2], low=r[3], close=r[4], volume=r[5] or 0) for r in rows]


async def persist(outcomes):
    from sqlalchemy.exc import IntegrityError
    added = 0
    for o in outcomes:
        try:
            async with get_session() as s:
                s.add(ShadowForwardSignal(**o))
                await s.flush()
            added += 1
        except IntegrityError:
            continue
    return added


async def main():
    await init_db()
    c = sqlite3.connect(DB)
    bars = load_es_rth(c)
    outcomes = S.score_session(bars)
    added = await persist(outcomes)

    # read back the full shadow set
    rows = c.execute(
        "SELECT session_date,in_sample,r_outcome,win FROM shadow_forward_signal "
        "WHERE signal_name=? AND definition_version=?", (S.SIGNAL_NAME, S.DEFINITION_VERSION),
    ).fetchall()
    insample = [(sd, r, w) for sd, ins, r, w in rows if ins]
    forward = [(sd, r, w) for sd, ins, r, w in rows if not ins]

    print(f"\n{'='*64}\nSHADOW: {S.SIGNAL_NAME}  def={S.DEFINITION_VERSION} hash={S.definition_hash()}")
    print(f"in-sample cutoff <= {S.IN_SAMPLE_CUTOFF}  (scored {len(outcomes)} sigs this run, +{added} new rows)\n{'='*64}")

    def block(label, data, n_trials):
        rs = [r for _, r, _ in data]
        if not rs:
            print(f"  {label}: 0 trades"); return
        st = stat_block(rs, tf_min=5, n_trials=n_trials, var_sr=0.05)
        sess = len({sd for sd, _, _ in data})
        print(f"  {label}: n={st['n']} sessions={sess} win%={st['win_rate']*100:.1f} "
              f"E[R]={st['expectancy_r']:+.3f} DSR={st['dsr']:.3f}")

    block("IN-SAMPLE (selection — reference only)", insample, 8)
    block("FORWARD-ONLY (the real test)", forward, 8)

    if forward:
        per = defaultdict(list)
        for sd, r, _ in forward:
            per[sd].append(r)
        print("  forward per-session:")
        for sd in sorted(per):
            print(f"    {sd}: n={len(per[sd])} sumR={sum(per[sd]):+.2f}")
        n_fwd = len(forward)
        e = stats.fmean([r for _, r, _ in forward])
        print(f"\n  PROGRESS: {n_fwd}/{FORWARD_TARGET} forward trades.")
        print(f"  Winner's-curse note: in-sample was +0.62; forward +0.25..+0.40 = consistent with real edge;")
        print(f"  forward <= 0 = selection artifact (kill). Current forward E[R]={e:+.3f}.")
    else:
        print("  FORWARD-ONLY: no forward sessions yet (all data is in-sample).")
        print(f"  Need {FORWARD_TARGET} forward trades (~12-20 sessions after {S.IN_SAMPLE_CUTOFF}) before a live decision.")


if __name__ == "__main__":
    asyncio.run(main())

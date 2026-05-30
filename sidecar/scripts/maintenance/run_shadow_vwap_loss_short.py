"""On-demand report for the vwap_loss_short forward-shadow candidate.

The nightly_scheduler already scores + persists this automatically (step 9). This
runner just RE-scores (idempotent) and prints the FORWARD-ONLY methodology stack
so you can check progress any time:
    python -m scripts.maintenance.run_shadow_vwap_loss_short

Scoring/persistence/ledger logic lives in engine/signals/shadow_vwap_loss_short.py
(single source of truth — shared with the nightly chain).
"""
import asyncio
import sqlite3
import statistics as stats
from collections import defaultdict

from fef_sidecar.db.engine import init_db
from fef_sidecar.engine.signals import shadow_vwap_loss_short as S
from scripts.research.dale_vwap_backtest import DB, stat_block

FORWARD_TARGET = 35


async def main():
    await init_db()
    bars = await S.load_es_rth_bars()
    added = await S.persist_outcomes(S.score_session(bars))
    await S.register_variant()
    n_trials = await S.forward_n_trials()

    c = sqlite3.connect(DB)
    rows = c.execute(
        "SELECT session_date,in_sample,r_outcome FROM shadow_forward_signal "
        "WHERE signal_name=? AND definition_version=?", (S.SIGNAL_NAME, S.DEFINITION_VERSION),
    ).fetchall()
    insample = [(sd, r) for sd, ins, r in rows if ins]
    forward = [(sd, r) for sd, ins, r in rows if not ins]

    print(f"\n{'='*64}\nSHADOW {S.SIGNAL_NAME}  def={S.DEFINITION_VERSION} hash={S.definition_hash()}")
    print(f"+{added} rows this run | N_trials={n_trials} (from variant_ledger, floor 8)\n{'='*64}")

    def block(label, data):
        rs = [r for _, r in data]
        if not rs:
            print(f"  {label}: 0 trades"); return
        st = stat_block(rs, tf_min=5, n_trials=n_trials, var_sr=0.05)
        sess = len({sd for sd, _ in data})
        print(f"  {label}: n={st['n']} sessions={sess} win%={st['win_rate']*100:.1f} "
              f"E[R]={st['expectancy_r']:+.3f} DSR={st['dsr']:.3f}")

    block("IN-SAMPLE (selection — reference)", insample)
    block("FORWARD-ONLY (the real test)", forward)
    if forward:
        per = defaultdict(list)
        for sd, r in forward:
            per[sd].append(r)
        for sd in sorted(per):
            print(f"    {sd}: n={len(per[sd])} sumR={sum(per[sd]):+.2f}")
        e = stats.fmean([r for _, r in forward])
        print(f"\n  PROGRESS {len(forward)}/{FORWARD_TARGET} fwd trades | E[R]={e:+.3f}")
        print("  +0.25..+0.40 = consistent w/ real edge (in-sample +0.62 deflated); <=0 = kill.")
    else:
        print(f"  FORWARD: none yet (all data <= {S.IN_SAMPLE_CUTOFF}). Need ~{FORWARD_TARGET} fwd trades.")


if __name__ == "__main__":
    asyncio.run(main())

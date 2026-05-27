"""Machine-readable companion to VARIANT_LEDGER.md — honest N_trials for DSR.

Per Harvey & Liu 2015 + Bailey & López de Prado 2014: N_trials must include
EVERY variant ever tested (active + dropped + superseded + shadow), not just
the currently-active set. The SQL `variant_ledger` table only tracks the 12
shadow_exit_replay variants; the rest of this project's variant history lives
in `engine_change` rows, git history, scratchpads, walk-forward runs, and
tournament presets.

This file is the canonical roll-up. Re-encode VARIANT_LEDGER.md whenever a
new variant is shipped, dropped, or proposed → tested. Keep the two files
in sync.

Counting rules (de-duplicated, conservative):
  - ACTIVE / SHADOW / SUPERSEDED / DROPPED all count as 1 trial each
  - PROPOSED (identified but not yet tested) does NOT count
  - Data plumbing changes do NOT count toward strategy N_trials
  - Variants appearing in multiple families count once (de-duplicated)
"""
from __future__ import annotations

# Per-family de-duplicated N_trials (strategy variants only — excludes
# PROPOSED and data-plumbing).
HONEST_N_BY_FAMILY: dict[str, int] = {
    "exit_logic": 30,        # 24 registered shadow (incl. 12 theta-tighten sweep 5/26) + 6 historical superseded/dropped
    "entry_filter": 14,      # 10 prior + 3 direction_filter shadow variants (5/26) + 1 graduated to ablation_backtest (5/26)
    "direction_filter": 4,   # conjunction_req_2 (shadow) + conjunction_req_1 + drop_low_counter_bias + conjunction_req_2 (ablation_backtest graduation, 5/26)
    "regime_gate": 3,        # excludes 1 PROPOSED (regime-conditional strikes)
    "strike_selection": 10,  # 1 ACTIVE + 1 DROPPED + 3 SHADOW chain-pending + strike_vol_adaptive (5/26) + 4 SHADOW BSM strike-offset sweep (10/15/20/35pt, Report 27, 5/26)
    "composite": 2,          # conjunction_req_2_AND_vol_adaptive (5/26) + conjunction_req_2_AND_bcd (ablation_backtest 5/26)
    "risk_management": 6,
    "sizing": 5,             # excludes 2 PROPOSED (contract scaling)
    "engine_formula": 13,    # v1/v2 transitions count separately
    "parameter_sweep": 9,    # walk-forward + per_regime_sweep winners
    "tournament_preset": 10, # 6 named + 4 multi_strategy
}

# Honest project-wide N_trials (de-duplicated across families).
# Used by deflated_sharpe_ratio() to correct apparent Sharpe for the
# selection bias of testing this many variants over the project's life.
# 2026-05-26: +2 from ablation_backtest graduation of conjunction_req_2
#   - conjunction_req_2 standalone (Baseline exit + verdict filter) — same
#     filter as the shadow but tested under the full DSR/PBO/CPCV stack
#   - conjunction_req_2 + B+C+D composite (new composite variant)
# 2026-05-26 (Report 27): +4 BSM-priced strike-offset shadow variants
#   (10pt, 15pt, 20pt, 35pt OTM — sweep to quantify strike picker change)
HONEST_N_PROJECT: int = 84

# Counts by status (informational — sum != HONEST_N_PROJECT because of
# multi-family entries; HONEST_N_PROJECT is the de-duplicated answer).
COUNTS_BY_STATUS: dict[str, int] = {
    "ACTIVE": 25,
    "SHADOW": 48,    # +3 strike_selection scaffold (5/26), +5 shadow_signal_replay verdict variants (5/26), +2 ablation_backtest conjunction_req_2 (5/26), +4 BSM strike-offset sweep (5/26 Report 27)
    "SUPERSEDED": 14,
    "DROPPED": 5,
    "PROPOSED": 22,   # not counted toward N_trials
    "DATA_PLUMBING": 12,  # not counted toward N_trials
}

# Total variants the project has ever considered, including PROPOSED.
# Useful for "honest about the search space" reporting but NOT for DSR.
TOTAL_EVER_CONSIDERED: int = HONEST_N_PROJECT + COUNTS_BY_STATUS["PROPOSED"]


def honest_n_trials(family: str | None = None) -> int:
    """Return the honest N_trials for DSR/PBO multiple-testing correction.

    Args:
        family: if provided, return the per-family count for stratified DSR
            (use this when ablation-testing within a single family). If None,
            return the project-wide de-duplicated count.

    Returns:
        Integer N_trials. Always >= 1.
    """
    if family is None:
        return HONEST_N_PROJECT
    return HONEST_N_BY_FAMILY.get(family, HONEST_N_PROJECT)


def all_family_counts() -> dict[str, int]:
    """Return a copy of the per-family N_trials map."""
    return dict(HONEST_N_BY_FAMILY)

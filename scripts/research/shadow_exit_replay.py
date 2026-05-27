"""Shadow exit-logic replay over live_order parents.

Post-hoc counterfactual: for each parent order placed during the date
window, walk forward through SPX 1-min bars from the parent's entry_spx
+ submitted_at and compute what each registered exit variant would have
realized. Persisted into `shadow_exit_outcome` (one row per parent ×
variant) for downstream analysis without ever touching the live engine's
exit code path.

Built 2026-05-21 after Chat recommended observational shadow mode for
B+C+D during the remaining freeze sessions. Generalized from the
original B+C+D-specific replay so any new exit variant — tighter
trail, different partial shares, time-based exits — can be tested
without a schema change. Variants register themselves in REGISTERED_VARIANTS.

Methodology matches scripts/research/ablation_backtest.py (so deltas
between this replay and ablation results reflect entry universe — actual
live parents vs all fired signals — not methodology drift).

Adding a new variant (the only thing you should ever edit):
    REGISTERED_VARIANTS = [
        ...,
        ExitVariant(
            label="bcd_tight_trail",
            stop_r=-1.0,
            targets=[(1.0, 1.0)],
            trail_peak_floor_r=0.4,        # stricter activation
            trail_pullback_r=0.10,         # tighter pullback
        ),
        # 2026-05-26 — theta-aware BE-ladder sweep. Each variant tightens
        # the SPX stop by `theta_tighten_per_min_pts * (mins_held - kickin)`
        # past `theta_kick_in_min` AFTER the BE ladder fires. Heuristic
        # SPX-space approximation of theta drag; see docstring on
        # ExitVariant.theta_tighten_per_min_pts for details.
        ExitVariant(
            label="bcd_theta_tighten_05pts_30min_with_be_ladder",
            stop_r=-1.0,
            targets=[(1.0, 0.5), (2.0, 0.3), (3.0, 0.2)],
            be_ladder=[(0.5, 0.0), (1.0, 0.5), (1.5, 1.0)],
            theta_tighten_per_min_pts=0.05,
            theta_kick_in_min=30,
        ),
    ]

CLI:
    python shadow_exit_replay.py 2026-05-18 2026-05-21
    python shadow_exit_replay.py 2026-05-18 2026-05-21 --variants bcd_trail_15pct,baseline_partial
    python shadow_exit_replay.py 2026-05-18 2026-05-21 --account-class paper --json

Programmatic:
    from shadow_exit_replay import run_replay, REGISTERED_VARIANTS, ExitVariant
    result = run_replay("2026-05-18", "2026-05-21")
"""
from __future__ import annotations

import argparse
import bisect
import json
import math
import sqlite3
import sys
from collections import defaultdict
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path

DEFAULT_DB = str(Path.home() / ".fef-terminal" / "signal_log.db")
DOLLARS_PER_R_PER_CONTRACT = 500.0  # 10pt SPX × $50/pt × 1 contract

# BSM-pricing defaults for strike-offset shadow variants (added 2026-05-26
# per Report 26 — quantify expected R/$ impact of changing strike picker's
# hardcoded offset_pts from 35 to 15 BEFORE the freeze unlock). See the
# strike_offset_Npt_OTM_with_be_ladder variants and _bsm_call/_bsm_put.
BSM_DEFAULT_IV = 0.15        # fallback when no VIX or signal IV is available
BSM_DEFAULT_R = 0.05         # risk-free rate (~5y treasury)
BSM_DEFAULT_Q = 0.0          # SPX index — no dividend
# 0DTE intraday session in UTC. Market close 20:00 UTC (16:00 ET) on weekdays;
# signals fire roughly 13:30-19:30 UTC (9:30 ET - 15:30 ET). The "time to
# expiry" for an SPX 0DTE option is measured in HOURS not days, so we use
# the exact submitted_at vs 16:00 ET cutoff converted to fraction-of-year.
SECONDS_PER_YEAR = 365.0 * 24.0 * 3600.0

# Canonical R-unit helper (Chat audit Priority 3 — 2026-05-22). R is the
# per-signal |entry - stop_spx| in SPX points, not a hardcoded 10pt.
sys.path.insert(0, str(Path(__file__).parent))
from _r_unit import compute_r_unit  # noqa: E402


# ---------------------------------------------------------------------------
# Variant definition + registry
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class ExitVariant:
    """A named exit-logic configuration. R-values are in R-units where 1R
    is per-signal |entry - stop_spx| in SPX points (canonical post-2026-05-22
    per Chat audit Priority 3). Pre-migration this was hardcoded to 10pt.

    Fields:
        label: stable identifier used as the row key in shadow_exit_outcome.
        stop_r: adverse R-level that closes the residual share. Negative.
        targets: ordered list of (favor_r_level, share_fraction). Share
            fractions should sum to ≤ 1.0; whatever is left when targets
            are exhausted exits either at trail or at timeout. Set to
            [(1.0, 1.0)] for a single hard +1R target (B+C+D's style).
            Set to [(1.0, 0.5), (2.0, 0.3), (3.0, 0.2)] for the standard
            50/30/20 partial scale-out.
        trail_peak_floor_r: peak_r must reach this level before the trail
            arms. None = trail disabled entirely.
        trail_pullback_r: once armed, trail exits residual when the bar's
            close drops to (peak_r - trail_pullback_r). In R-units.
        timeout_minutes: total forward window for the walk. Residual at
            the last bar exits at its close.
        regime_routes: optional regime → variant_label mapping. When set,
            this variant becomes a *meta-variant*: for each parent,
            look up the parent's regime_at_fire and dispatch to the routed
            leaf variant. The leaf's stop/targets/trail fields are used —
            this variant's own fields are ignored. Use default_route for
            regimes not in the dict.
        default_route: fallback variant_label when regime is missing or
            not present in regime_routes. Required if regime_routes set.
    """
    label: str
    stop_r: float = -1.0
    targets: list[tuple[float, float]] = field(default_factory=list)
    trail_peak_floor_r: float | None = None
    trail_pullback_r: float | None = None
    timeout_minutes: int = 60
    regime_routes: dict[str, str] | None = None
    default_route: str | None = None
    # Optional entry-side gate. None = always trade. When set, the
    # variant evaluates the gate at parent entry time; if it blocks,
    # the row is persisted with realized_r=0 (representing "would have
    # skipped this fire"). Composes cleanly with any exit logic so we
    # can isolate the entry filter as the only knob changed vs baseline.
    # Supported kinds: 'direction_30m' (params: threshold_pts).
    entry_filter: dict | None = None
    # Break-even ladder — ordered list of (peak_r_trigger, new_stop_r).
    # Once peak_r crosses a trigger, the dynamic stop ratchets UP to
    # new_stop_r (only upward, never down). Captures the live engine's
    # runner-protection behavior. spx_target_watcher.py uses
    # [(0.5, 0.0), (1.0, 0.5), (1.5, 1.0)]: hit +0.5R → stop to BE;
    # hit +1.0R → lock +0.5R; hit +1.5R → lock +1R. The variants
    # without be_ladder set keep the original -1R fixed stop (a
    # pessimistic-vs-live model — useful for measuring the BE ladder's
    # incremental contribution).
    be_ladder: list[tuple[float, float]] | None = None
    # When True, the BE ladder R-levels reference OPTION PRICE rather
    # than SPX price (variant 2b in memory). The shadow simulator
    # cannot evaluate this honestly without an option-price evolution
    # model (Greeks + theta decay + IV path). Variants with this flag
    # set return realized_r=NaN + exit_reason='requires_option_price_evolution'
    # until the option-pricing layer ships. Hypothesis documented in
    # the ledger so the test is reproducible whenever the simulator
    # catches up.
    option_price_be_ladder: bool = False
    # Theta-haircut SPX-stop tightener (Option 1 in 2026-05-26 design doc).
    # Heuristic approximation of theta drag without needing option-mid
    # time series. Once the BE ladder has fired AT LEAST ONCE, every
    # subsequent bar tightens dynamic_stop_r by
    #   haircut_r = max(0, minutes_held - kick_in_min)
    #             * theta_tighten_per_min_pts / risk_pts
    # ratcheted into dynamic_stop_r monotonically (the stop only moves
    # toward profit, never the other way). Capped at the most recent
    # BE-ladder rung's new_stop_r so theta-tighten doesn't lift the stop
    # above the BE level on its own — it can only compound on top of
    # what the BE ladder already locked in. Composes with the existing
    # be_ladder + targets + trail layers; if be_ladder is None the
    # tighten never activates (theta drag is only relevant once you've
    # got something to protect). DOES NOT account for vol crush or
    # actual option premium dynamics — it's an SPX-space heuristic that
    # behaves more conservatively as the trade ages.
    theta_tighten_per_min_pts: float | None = None
    theta_kick_in_min: float | None = None
    # Alternate strike selection at entry — when set, the variant ignores
    # the actual parent's traded strike and re-computes hypothetical P&L
    # from the chain data captured at entry/exit time for whichever strike
    # the rule selects. Each kind reads option mid prices from
    # historical_option_chain_point joined on (timestamp_ms, strike, right).
    # See _resolve_alt_strike + _compute_alt_strike_dollars for the lookup.
    #
    # Supported kinds:
    #   - 'atm_call_only': nearest 5pt strike at ATM (delta ~0.50)
    #   - 'atm_minus_5pt_ITM': 5pt ITM (higher delta, more directional)
    #   - 'atm_plus_5pt_OTM': 5pt OTM (lower delta, cheaper premium)
    #
    # Variants with strike_selection set skip the R-based dollar computation
    # entirely; their realized_r still reflects the bar-walk exit (so the
    # exit timing matches the comparable non-strike variant) but the
    # dollars field comes from option-mid evolution. When no chain data
    # is available for the parent's entry minute, the row is persisted
    # with exit_reason='no_chain_data' and dollars=0.
    strike_selection: str | None = None
    # BSM-priced strike-offset variant (added 2026-05-26 per Report 26).
    # When set, the variant overrides the strike at entry to
    # `round((spot ± strike_offset_pts) / 5) * 5` and recomputes dollars
    # using Black-Scholes-Merton at entry + exit. Unlike strike_selection
    # (which requires historical_option_chain_point data), this path is
    # always evaluable — it uses BSM approximation with IV from
    # signal_iter.extras.vix_at_fire → vix_sample → fallback. Caveats:
    # does NOT account for skew/smile, intraday IV changes, or actual
    # market-maker quote dynamics. SPX-based exit timing is unchanged
    # (same bar walk as baseline_partial_with_be_ladder); only the
    # dollars field is BSM-derived. realized_r still reflects SPX-side
    # exit. The point is to bound the expected dollar impact of the
    # strike-picker offset change (35pt → 15pt) before the freeze
    # unlocks for live deployment.
    strike_offset_pts: float | None = None
    # Ledger metadata — populated at variant definition time, written
    # to the variant_ledger table at every replay run. Hierarchical
    # taxonomy enables family-level N_trials for DSR/PBO corrections
    # per LdP/Bailey multiple-testing convention.
    family: str = ""
    sub_family: str | None = None
    description: str = ""
    hypothesis: str = ""

    @property
    def is_meta(self) -> bool:
        return self.regime_routes is not None

    def to_dict(self) -> dict:
        d = asdict(self)
        # tuple → list for JSON storage
        d["targets"] = [list(t) for t in self.targets]
        return d


# Canonical variants. Append to this list to register new ones; the
# replay loop picks them up automatically.
REGISTERED_VARIANTS: list[ExitVariant] = [
    ExitVariant(
        # The current live exit logic shape: 50/30/20 partial scale-out
        # with -1R stop, no trail. Matches ablation_backtest._simulate_baseline.
        label="baseline_partial_50_30_20",
        stop_r=-1.0,
        targets=[(1.0, 0.5), (2.0, 0.3), (3.0, 0.2)],
        family="exit_logic",
        sub_family="timeout",  # 60min implicit baseline for the timeout sweep
        description="Pessimistic shadow baseline: 50/30/20 partials + -1R fixed stop + 60min timeout. Does NOT model the live BE ladder.",
        hypothesis="Reference. Pessimistic vs actual live (which includes BE ladder); the gap quantifies the ladder's incremental contribution.",
    ),
    ExitVariant(
        # B+C+D as evaluated in ablation_backtest._simulate_bcd: 100%
        # hard exit at T1 with peak-trail at 15% pullback after peak >= 0.3R.
        # This is the "headline winner" from the P1 ablation.
        label="bcd_trail_15pct",
        stop_r=-1.0,
        targets=[(1.0, 1.0)],
        trail_peak_floor_r=0.3,
        trail_pullback_r=0.15,
        family="exit_logic",
        sub_family="trail_tightness",
        description="P1 ablation B+C+D: 100% T1 hard exit + peak-trail at 15% pullback after peak >= 0.3R.",
        hypothesis="Trail-exit on chop/red days saves stops the baseline takes at -1R. Costs upside on clean trend days.",
    ),
    ExitVariant(
        # Regime-conditional B+C+D — added 2026-05-21 per Chat's
        # recommendation after the shadow replay surfaced the
        # trending-vs-chop split (B+C+D costs $5k on 5/19's trend day,
        # saves $32k on 5/20+5/21 chop/red days).
        # Routes:
        #   TREND_DAY / DRIFT_BULL → baseline (timeouts ride the trend)
        #   everything else        → bcd_trail_15pct (trail saves stops)
        # All fields below (stop_r, targets, ...) are IGNORED — the
        # routed leaf variant's params are used at simulate time.
        label="bcd_only_in_chop",
        regime_routes={
            "TREND_DAY": "baseline_partial_50_30_20",
            "DRIFT_BULL": "baseline_partial_50_30_20",
        },
        default_route="bcd_trail_15pct",
        family="regime_gate",
        sub_family=None,
        description="Regime-conditional: baseline on TREND_DAY/DRIFT_BULL, B+C+D elsewhere.",
        hypothesis="Routing exit logic by regime captures the trend-vs-chop trade-off identified in 5/19-5/21 shadow.",
    ),
    # Trail-pullback parameter sweep — registered 2026-05-21 per Chat's
    # "register variants proactively so you have data by session 10" plan.
    # Identical to bcd_trail_15pct except the pullback knob, so the 3-way
    # comparison (10% / 15% / 25%) isolates the trail tightness as the
    # only variable. Tests whether 15% is at a noise-influenced local
    # optimum or actually the right pullback distance.
    ExitVariant(
        # Tighter trail — exits residual faster after MFE pullback.
        label="bcd_trail_10pct",
        stop_r=-1.0,
        targets=[(1.0, 1.0)],
        trail_peak_floor_r=0.3,
        trail_pullback_r=0.10,
        family="exit_logic",
        sub_family="trail_tightness",
        description="Tighter trail than B+C+D default — exits residual at 10% pullback from peak.",
        hypothesis="On fast-reversal days like 5/21 the current 15% lets too much MFE leak before trail triggers.",
    ),
    ExitVariant(
        # Looser trail — lets winners run longer before pullback exit.
        label="bcd_trail_25pct",
        stop_r=-1.0,
        targets=[(1.0, 1.0)],
        trail_peak_floor_r=0.3,
        trail_pullback_r=0.25,
        family="exit_logic",
        sub_family="trail_tightness",
        description="Looser trail than B+C+D default — exits at 25% pullback.",
        hypothesis="On trending days like 5/19 the current 15% triggers too early on small dips and misses subsequent runs.",
    ),
    # ----- Other deferred-change tests (added 2026-05-21) -----
    # Each variant below differs from the current live baseline by
    # exactly one knob, so the comparison isolates that knob's effect.
    ExitVariant(
        # Pure trail, no hard T1 target.
        label="pure_trail_15pct",
        stop_r=-1.0,
        targets=[],
        trail_peak_floor_r=0.3,
        trail_pullback_r=0.15,
        family="exit_logic",
        sub_family="trail_tightness",
        description="Trail-only exit (no T1 hard target). Stop at -1R, trail at 15% pullback after peak >= 0.3R.",
        hypothesis="Hard +1R T1 caps upside on trades that run past 1R peak; trail alone captures more on big movers.",
    ),
    ExitVariant(
        # Baseline structure with longer timeout window.
        label="baseline_timeout_90min",
        stop_r=-1.0,
        targets=[(1.0, 0.5), (2.0, 0.3), (3.0, 0.2)],
        timeout_minutes=90,
        family="exit_logic",
        sub_family="timeout",
        description="Baseline 50/30/20 + -1R stop, but 90min timeout instead of 60min.",
        hypothesis="60min cuts trending-day residual too early; extending lets trends run.",
    ),
    ExitVariant(
        # Deferred change: direction filter at entry (the ablation
        # variant we discussed but didn't ship). Filter logic identical
        # to ablation_backtest's direction filter — block PUTS when 30m
        # SPX > +3pt; block CALLS when 30m SPX < -3pt. Pairs with
        # baseline exit so the filter is the only knob changed vs the
        # current live logic (NOT mixed with B+C+D exit, per isolation
        # requirement).
        label="entry_dir_filter_then_baseline",
        stop_r=-1.0,
        targets=[(1.0, 0.5), (2.0, 0.3), (3.0, 0.2)],
        entry_filter={"kind": "direction_30m", "params": {"threshold_pts": 3.0}},
        family="entry_filter",
        sub_family=None,
        description="Block PUTS when 30m SPX > +3pt; block CALLS when 30m SPX < -3pt. Baseline exit on entries that pass.",
        hypothesis="Direction filter blocks bad-direction fires that B+C+D's trail can't always save.",
    ),
    # ----- BE-ladder variants -----
    # Critical discovery 2026-05-21: spx_target_watcher.py applies a BE
    # ladder [(0.5, 0.0), (1.0, 0.5), (1.5, 1.0)] to all parents. The
    # variants above (no be_ladder set) are pessimistic vs actual live
    # behavior — they show stop-outs at -1R that the live engine would
    # have ratcheted to BE before adverse moves hit. The two variants
    # below model the ladder explicitly so we can measure (a) accurate
    # current-live behavior, and (b) the 2b+B+C+D combination the user
    # asked about (runner protection from BE ladder + B+C+D's trail).
    ExitVariant(
        label="baseline_partial_with_be_ladder",
        stop_r=-1.0,
        targets=[(1.0, 0.5), (2.0, 0.3), (3.0, 0.2)],
        be_ladder=[(0.5, 0.0), (1.0, 0.5), (1.5, 1.0)],
        family="exit_logic",
        sub_family="be_ladder_combo",
        description="Accurate model of current live exit logic: 50/30/20 partials + live BE ladder [(0.5,0), (1.0,0.5), (1.5,1.0)].",
        hypothesis="The pessimistic baseline understates live performance; BE ladder rescues -1R stops that ratcheted to BE first.",
    ),
    ExitVariant(
        label="bcd_trail_15pct_with_be_ladder",
        stop_r=-1.0,
        targets=[(1.0, 1.0)],
        trail_peak_floor_r=0.3,
        trail_pullback_r=0.15,
        be_ladder=[(0.5, 0.0), (1.0, 0.5), (1.5, 1.0)],
        family="exit_logic",
        sub_family="be_ladder_combo",
        description="B+C+D trail + live BE ladder. Two protection mechanisms composed.",
        hypothesis="BE ladder + trail are complementary — ladder limits worst-case loss past T1, trail captures intermediate MFE.",
    ),
    ExitVariant(
        label="pure_trail_15pct_with_be_ladder",
        stop_r=-1.0,
        targets=[],
        trail_peak_floor_r=0.3,
        trail_pullback_r=0.15,
        be_ladder=[(0.5, 0.0), (1.0, 0.5), (1.5, 1.0)],
        family="exit_logic",
        sub_family="be_ladder_combo",
        description="Pure trail + live BE ladder. No T1 hard target.",
        hypothesis="If trail-alone is doing the work, adding BE ladder runner protection should compose into the best variant.",
    ),
    # ----- Placeholder for option-price BE ladder (variant 2b proper) -----
    ExitVariant(
        # Variant 2b as user originally meant: BE ladder R-levels
        # measured in OPTION PRICE units, not SPX. Captures theta drag
        # that the SPX-based BE ladder doesn't see (a trade that hit
        # SPX +0.5R at 11am may be option +20%; at 2pm same SPX move
        # nets +10% because theta ate gains). Shadow simulator can't
        # evaluate this honestly without Greeks-based option-price
        # evolution. Registered as a placeholder so the hypothesis is
        # captured; lights up retroactively when the option-pricing
        # module ships post-session-10.
        label="bcd_trail_15pct_with_option_price_be_ladder",
        stop_r=-1.0,
        targets=[(1.0, 1.0)],
        trail_peak_floor_r=0.3,
        trail_pullback_r=0.15,
        be_ladder=[(0.5, 0.0), (1.0, 0.5), (1.5, 1.0)],
        option_price_be_ladder=True,
        family="exit_logic",
        sub_family="be_ladder_combo",
        description="Variant 2b proper: BE ladder R-levels in OPTION PRICE units (theta-aware). NOT YET EVALUABLE — requires option-price evolution module.",
        hypothesis="Option-price BE captures theta drag that SPX-based BE misses; should outperform on slow-moving favorable trades where theta erodes profit.",
    ),
    # ----- Theta-aware BE-ladder tighten sweep (registered 2026-05-26) -----
    # User briefed Option 1: SPX-space heuristic theta approximation that
    # tightens the BE-ladder stop additively as time held grows past a
    # kick-in threshold. Designed to mitigate the 79.5% BE-kill rate on
    # 5/22 where 31/39 stopped trades had a BE rung pre-activated and
    # gave back avg 0.65R from a climbed peak — exactly the slow-grind-
    # then-roll-over pathology that pure SPX BE doesn't catch.
    #
    # 4 tighten rates × 3 kick-in thresholds = 12 sibling variants of
    # baseline_partial_with_be_ladder. The baseline itself is unchanged
    # (still the comparison ref). Each variant differs from the baseline
    # by exactly the (tighten_per_min_pts, kick_in_min) pair, so any
    # delta in cumR is attributable to the theta-haircut layer alone.
    #
    # Heuristic: does NOT model vol crush, gamma asymmetry, or actual
    # option premium dynamics. Approximates theta drag as a linear
    # SPX-stop tightening rate past kick_in_min. The 5/18-5/22 backtest
    # is n=5 sessions and noisy — these variants are deliberately
    # forward-looking, intended to gather data across the next 10
    # sessions before deciding whether to ship one live.
    *[
        ExitVariant(
            label=f"bcd_theta_tighten_{int(round(tighten*100)):02d}pts_{kickin}min_with_be_ladder",
            stop_r=-1.0,
            targets=[(1.0, 0.5), (2.0, 0.3), (3.0, 0.2)],
            be_ladder=[(0.5, 0.0), (1.0, 0.5), (1.5, 1.0)],
            theta_tighten_per_min_pts=tighten,
            theta_kick_in_min=kickin,
            family="exit_logic",
            sub_family="theta_tighten",
            description=(
                f"Theta-aware BE ladder: 50/30/20 partials + live BE ladder, "
                f"tightens SPX stop by {tighten:.2f}pts × (minutes_held - {kickin}min) "
                f"after BE has fired. Heuristic theta approximation; does NOT "
                f"account for vol crush or actual option premium dynamics."
            ),
            hypothesis=(
                f"Once BE rung 0.5R fires, every minute past {kickin}min should "
                f"tighten the SPX stop by {tighten:.2f}pts to compensate for theta "
                f"erosion of premium. Targets the 79.5% BE-kill pathology on 5/22 "
                f"where slow-grind trades climbed past 1R and gave it all back."
            ),
        )
        for tighten in (0.03, 0.05, 0.08, 0.12)
        for kickin in (15, 30, 45)
    ],
    # ----- Alternate strike-selection variants (registered 2026-05-26) -----
    # All three share exit logic with baseline_partial_with_be_ladder (the
    # proven baseline). The only knob changed is the strike picked at entry:
    # 5pt-OTM vs ATM vs 5pt-ITM. Dollars are recomputed from
    # historical_option_chain_point mid prices at entry + exit, NOT from
    # SPX-point R movement. This isolates the strike choice as the only
    # variable; the SPX-side exit decision is unchanged.
    #
    # No data exists for these variants until the option chain capture loop
    # starts populating historical_option_chain_point. Once the next session
    # begins they begin accumulating outcomes alongside the existing variants.
    # Rows for signals before capture started will persist with
    # exit_reason='no_chain_data'.
    ExitVariant(
        label="bcd_atm_strike_with_be_ladder",
        stop_r=-1.0,
        targets=[(1.0, 0.5), (2.0, 0.3), (3.0, 0.2)],
        be_ladder=[(0.5, 0.0), (1.0, 0.5), (1.5, 1.0)],
        strike_selection="atm_call_only",
        family="strike_selection",
        sub_family=None,
        description=(
            "Same exit as baseline_partial_with_be_ladder but strike is "
            "ATM (nearest 5pt, delta ~0.50). Dollars from option-mid "
            "evolution at entry + exit via historical_option_chain_point."
        ),
        hypothesis=(
            "ATM delta ~0.50 captures more $/SPX-pt than the current ~30pt "
            "OTM (delta ~0.15) on directional days; user-trading-style "
            "memory says ATM amplifies losses on bad-direction days so "
            "this is CONDITIONAL on direction filter — priority 3 per the "
            "revised post-freeze plan."
        ),
    ),
    ExitVariant(
        label="bcd_atm_minus_5pt_ITM_with_be_ladder",
        stop_r=-1.0,
        targets=[(1.0, 0.5), (2.0, 0.3), (3.0, 0.2)],
        be_ladder=[(0.5, 0.0), (1.0, 0.5), (1.5, 1.0)],
        strike_selection="atm_minus_5pt_ITM",
        family="strike_selection",
        sub_family=None,
        description=(
            "5pt ITM strike — higher delta (~0.55-0.65), more directional. "
            "Exit logic identical to baseline_partial_with_be_ladder."
        ),
        hypothesis=(
            "ITM amplifies winning trades but caps loss recovery via earlier "
            "delta-driven price floor near intrinsic value. Useful test of "
            "whether ITM premiums shift the win/loss tail asymmetry."
        ),
    ),
    ExitVariant(
        label="bcd_atm_plus_5pt_OTM_with_be_ladder",
        stop_r=-1.0,
        targets=[(1.0, 0.5), (2.0, 0.3), (3.0, 0.2)],
        be_ladder=[(0.5, 0.0), (1.0, 0.5), (1.5, 1.0)],
        strike_selection="atm_plus_5pt_OTM",
        family="strike_selection",
        sub_family=None,
        description=(
            "5pt OTM strike — closer to ATM than the current ~30pt OTM "
            "default but slightly OTM. Exit logic identical to "
            "baseline_partial_with_be_ladder."
        ),
        hypothesis=(
            "Bridge between ATM (delta ~0.50) and current default OTM "
            "(delta ~0.15) — tests whether moderate OTM keeps the cheaper-"
            "premium upside while not over-amplifying losses on chop days."
        ),
    ),
    # ----- BSM-priced strike-offset sweep (registered 2026-05-26) -----
    # Per Report 26: 0% strike-reach rate across 188 parents 5/19-5/26
    # with current hardcoded offset_pts=35.0 at engine/strikes.py:24-30.
    # Counterfactual 60-min lookahead suggests 15pt offset → 24-50%
    # reach rate. Translates the reach-rate observation into a quantified
    # R/$ impact via BSM approximation. Exit logic identical to
    # baseline_partial_with_be_ladder (50/30/20 + BE-ladder + -1R stop)
    # so the only difference vs the baseline is the strike picked at
    # entry. Dollars are BSM-derived from entry + exit (does NOT account
    # for skew, smile, intraday IV moves, or actual MM quote dynamics —
    # see sensitivity analysis in report 27).
    #
    # Unlike strike_selection (which awaits historical_option_chain_point
    # capture data), strike_offset_pts is always evaluable. The bar walk
    # remains SPX-driven (so exit_reason + realized_r match the baseline);
    # only the dollars field changes.
    *[
        ExitVariant(
            label=f"bcd_strike_offset_{int(offset_pts)}pt_with_be_ladder",
            stop_r=-1.0,
            targets=[(1.0, 0.5), (2.0, 0.3), (3.0, 0.2)],
            be_ladder=[(0.5, 0.0), (1.0, 0.5), (1.5, 1.0)],
            strike_offset_pts=offset_pts,
            family="strike_selection",
            sub_family="strike_offset_bsm",
            description=(
                f"BSM-priced strike-offset sweep variant: {int(offset_pts)}pt OTM "
                f"strike (vs current live 35pt). Same exit logic as "
                f"baseline_partial_with_be_ladder; only the strike — and "
                f"therefore the option premium curve — differs. Dollars "
                f"computed via BSM at entry + exit using IV from "
                f"signal_iter.extras.vix_at_fire → nearest vix_sample → "
                f"15%% fallback. Does NOT account for IV skew/smile or "
                f"intraday IV path."
            ),
            hypothesis=(
                f"Per Report 26, closer-to-ATM strikes should give better "
                f"gamma response to favorable SPX moves. At {int(offset_pts)}pt "
                f"OTM (delta ~{0.45 - offset_pts*0.012:.2f}), a +1R SPX move "
                f"should yield substantially more $-per-contract than the "
                f"current 35pt OTM (delta ~0.10). Question: does the better "
                f"delta translate to net positive R/$ AFTER the engine's "
                f"current stop+BE-ladder discipline, or do losses also "
                f"amplify?"
            ),
        )
        for offset_pts in (10.0, 15.0, 20.0)
    ],
    # 35pt BSM baseline — same BSM methodology as the sweep but at the
    # current live engine offset. This is the apples-to-apples comparison
    # ref for the sweep above. Note: differs from baseline_partial_with_be_ladder
    # because that variant uses the idealized R*$500/contract conversion
    # (which assumes delta ~0.5 — i.e. ATM); at 35pt OTM with delta ~0.07
    # the actual per-contract gain on a +1R SPX move is ~$70, not $500.
    # The R-figures match; only $ differs in interpretation.
    ExitVariant(
        label="bcd_strike_offset_35pt_with_be_ladder",
        stop_r=-1.0,
        targets=[(1.0, 0.5), (2.0, 0.3), (3.0, 0.2)],
        be_ladder=[(0.5, 0.0), (1.0, 0.5), (1.5, 1.0)],
        strike_offset_pts=35.0,
        family="strike_selection",
        sub_family="strike_offset_bsm",
        description=(
            "BSM-priced 35pt OTM (current live engine default). Apples-to-"
            "apples BSM baseline for comparison with the 10/15/20 sweep. "
            "Same R-realization as baseline_partial_with_be_ladder; "
            "dollars are BSM-derived per-strike instead of R*$500/contract."
        ),
        hypothesis=(
            "BSM baseline at current engine offset — used to compute "
            "Δ$ deltas for the strike-offset sweep without methodology "
            "noise from comparing BSM dollars to R*$500 estimates."
        ),
    ),
]


def _variant_by_label(label: str) -> ExitVariant | None:
    return next((v for v in REGISTERED_VARIANTS if v.label == label), None)


def upsert_variant_ledger(
    db_cursor,
    variants: list[ExitVariant],
    tested_in: str = "shadow_replay",
    sample_window: str = "",
) -> int:
    """Idempotent upsert of every registered variant into variant_ledger.
    Updates `last_seen_at` + sample_window on existing rows; preserves
    `outcome_summary` and `still_active` (those are managed separately).
    Called from run_replay so the ledger stays current automatically.

    Returns count of rows touched."""
    now = datetime.now(timezone.utc).isoformat()
    n = 0
    for v in variants:
        params_json = json.dumps(v.to_dict())
        # Upsert by variant_id (unique). On conflict, refresh
        # last_seen_at + sample_window + variant_params; leave
        # outcome_summary + still_active alone.
        db_cursor.execute(
            """
            INSERT INTO variant_ledger
                (variant_id, family, sub_family, description, hypothesis,
                 registered_at, last_seen_at, tested_in, sample_window,
                 outcome_summary, still_active, variant_params)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 1, ?)
            ON CONFLICT(variant_id) DO UPDATE SET
                last_seen_at = excluded.last_seen_at,
                sample_window = excluded.sample_window,
                variant_params = excluded.variant_params,
                family = excluded.family,
                sub_family = excluded.sub_family,
                description = excluded.description,
                hypothesis = excluded.hypothesis,
                tested_in = excluded.tested_in
            """,
            (
                v.label, v.family or "unclassified", v.sub_family,
                v.description, v.hypothesis,
                now, now, tested_in, sample_window,
                "placeholder" if v.option_price_be_ladder else "pending",
                params_json,
            ),
        )
        n += 1
    return n


def _norm_cdf(x: float) -> float:
    """Standard normal CDF — math.erf only, no scipy dependency. Matches
    scipy.stats.norm.cdf to ~7 decimal places, plenty for BSM pricing."""
    return 0.5 * (1.0 + math.erf(x / math.sqrt(2.0)))


def _bsm_call(spot: float, strike: float, T: float, sigma: float,
              r: float = BSM_DEFAULT_R, q: float = BSM_DEFAULT_Q) -> float:
    """Black-Scholes-Merton call price (no dividend for SPX index).

    Returns the call premium per share (multiply by 100 for $-per-contract).
    Handles edge cases: at expiry (T<=0) returns intrinsic value; on
    pathological sigma/T inputs returns intrinsic as a safe fallback.
    """
    if T <= 1e-9 or sigma <= 1e-9 or spot <= 0 or strike <= 0:
        return max(0.0, spot - strike)
    sqrt_T = math.sqrt(T)
    d1 = (math.log(spot / strike) + (r - q + 0.5 * sigma * sigma) * T) / (sigma * sqrt_T)
    d2 = d1 - sigma * sqrt_T
    return spot * math.exp(-q * T) * _norm_cdf(d1) - strike * math.exp(-r * T) * _norm_cdf(d2)


def _bsm_put(spot: float, strike: float, T: float, sigma: float,
             r: float = BSM_DEFAULT_R, q: float = BSM_DEFAULT_Q) -> float:
    """Black-Scholes-Merton put price (no dividend for SPX index).

    Returns the put premium per share (multiply by 100 for $-per-contract).
    """
    if T <= 1e-9 or sigma <= 1e-9 or spot <= 0 or strike <= 0:
        return max(0.0, strike - spot)
    sqrt_T = math.sqrt(T)
    d1 = (math.log(spot / strike) + (r - q + 0.5 * sigma * sigma) * T) / (sigma * sqrt_T)
    d2 = d1 - sigma * sqrt_T
    return strike * math.exp(-r * T) * _norm_cdf(-d2) - spot * math.exp(-q * T) * _norm_cdf(-d1)


def _bsm_delta(spot: float, strike: float, T: float, sigma: float, right: str,
               r: float = BSM_DEFAULT_R, q: float = BSM_DEFAULT_Q) -> float:
    """BSM delta (call: 0..1; put: -1..0). Used for gamma-curve diagnostics."""
    if T <= 1e-9 or sigma <= 1e-9 or spot <= 0 or strike <= 0:
        if right == "C":
            return 1.0 if spot > strike else 0.0
        return -1.0 if spot < strike else 0.0
    sqrt_T = math.sqrt(T)
    d1 = (math.log(spot / strike) + (r - q + 0.5 * sigma * sigma) * T) / (sigma * sqrt_T)
    if right == "C":
        return math.exp(-q * T) * _norm_cdf(d1)
    return math.exp(-q * T) * (_norm_cdf(d1) - 1.0)


def _bsm_price(spot: float, strike: float, T: float, sigma: float, right: str,
               r: float = BSM_DEFAULT_R, q: float = BSM_DEFAULT_Q) -> float:
    """Dispatch to call/put based on right."""
    if right == "C":
        return _bsm_call(spot, strike, T, sigma, r, q)
    return _bsm_put(spot, strike, T, sigma, r, q)


def _time_to_expiry_years(entry_ms: int, session_date: str) -> float:
    """SPX 0DTE: expiry = same day at 16:00 ET (= 20:00 UTC for May/EDT).

    Returns years remaining from entry_ms to expiry. Capped at a tiny
    positive value if entry is at/after the close (so BSM doesn't divide
    by zero). Uses 20:00 UTC for the EDT-window months in our dataset
    (May 2026). DST-aware callers should adjust; for the 5/19-5/26
    window this is correct.
    """
    yyyy, mm, dd = session_date.split("-")
    # 20:00 UTC = 16:00 ET (EDT — May timezone)
    close_dt = datetime(int(yyyy), int(mm), int(dd), 20, 0, 0, tzinfo=timezone.utc)
    close_ms = int(close_dt.timestamp() * 1000)
    seconds_remaining = max(60.0, (close_ms - entry_ms) / 1000.0)  # floor at 1 min
    return seconds_remaining / SECONDS_PER_YEAR


def _resolve_iv_for_signal(
    db_cursor,
    entry_ms: int,
    signal_iter_extras_json: str | None,
    iv_override: float | None = None,
) -> tuple[float, str]:
    """Return (iv_decimal, source). Tries in order:

    1. iv_override (sensitivity-analysis param)
    2. signal_iter.extras.vix_at_fire / 100
    3. nearest vix_sample / 100 (within ±60min)
    4. BSM_DEFAULT_IV fallback

    IV is returned as a decimal (e.g. 0.15 not 15.0).
    """
    if iv_override is not None and iv_override > 0:
        return float(iv_override), "override"
    if signal_iter_extras_json:
        try:
            ex = json.loads(signal_iter_extras_json)
            vix = ex.get("vix_at_fire")
            if vix is not None and 5.0 < float(vix) < 100.0:
                return float(vix) / 100.0, "extras.vix_at_fire"
        except (json.JSONDecodeError, TypeError, ValueError):
            pass
    # Nearest vix_sample lookup. VIX moves slowly enough that the
    # exact intraday minute doesn't matter much — falling back through
    # progressively wider windows so we get *some* native VIX whenever
    # the sampler captured even part of the trading day.
    for window_min in (60, 240, 720, 1440):  # 1h → 4h → 12h → 24h
        try:
            row = db_cursor.execute(
                """
                SELECT value, ABS(timestamp_ms - ?) AS diff
                FROM vix_sample
                WHERE timestamp_ms BETWEEN ? AND ?
                ORDER BY diff ASC
                LIMIT 1
                """,
                (entry_ms, entry_ms - window_min * 60_000, entry_ms + window_min * 60_000),
            ).fetchone()
            if row is not None and row["value"] is not None:
                v = float(row["value"])
                if 5.0 < v < 100.0:
                    src = f"vix_sample_nearest_{window_min}m"
                    return v / 100.0, src
        except Exception:
            pass
    return BSM_DEFAULT_IV, "default_fallback"


def _compute_bsm_strike_dollars(
    db_cursor,
    *,
    strike_offset_pts: float,
    direction_sign: int,
    entry_spx: float,
    entry_ms: int,
    exit_ms: int | None,
    exit_spx: float | None,
    session_date: str,
    qty: int,
    signal_iter_extras_json: str | None,
    iv_override: float | None = None,
) -> tuple[float, dict]:
    """BSM-based dollar computation for strike-offset shadow variants.

    The variant overrides the live engine's strike pick with
    `strike = round((spot ± offset_pts) / 5) * 5` (matching
    _nearest_otm_5pt rounding), then prices entry and exit with BSM.

    Returns (dollars, diagnostics_dict). dollars is always finite (BSM
    gives a price for any positive T). When exit_ms is None (no forward
    bars), entry_spx is used as the exit spot — equivalent to "trade
    instantly closed at entry" which nets ~0 dollars (intent: the row
    persists but isn't misleading).
    """
    right = "C" if direction_sign > 0 else "P"
    offset_signed = strike_offset_pts if direction_sign > 0 else -strike_offset_pts
    new_strike = round((entry_spx + offset_signed) / 5.0) * 5.0

    iv, iv_source = _resolve_iv_for_signal(
        db_cursor, entry_ms, signal_iter_extras_json, iv_override
    )

    T_entry = _time_to_expiry_years(entry_ms, session_date)
    entry_price = _bsm_price(entry_spx, new_strike, T_entry, iv, right)
    entry_delta = _bsm_delta(entry_spx, new_strike, T_entry, iv, right)

    if exit_ms is None or exit_spx is None:
        # No forward bars at all — degenerate case. Return zero P&L.
        return 0.0, {
            "strike": new_strike, "right": right,
            "iv": round(iv, 4), "iv_source": iv_source,
            "T_entry_yr": round(T_entry, 6),
            "entry_premium": round(entry_price, 3),
            "entry_delta": round(entry_delta, 4),
            "exit_premium": round(entry_price, 3),
            "exit_delta": round(entry_delta, 4),
            "T_exit_yr": round(T_entry, 6),
            "note": "no_exit_bar",
        }

    T_exit = _time_to_expiry_years(exit_ms, session_date)
    exit_price = _bsm_price(exit_spx, new_strike, T_exit, iv, right)
    exit_delta = _bsm_delta(exit_spx, new_strike, T_exit, iv, right)

    dollars = (exit_price - entry_price) * qty * 100.0

    return dollars, {
        "strike": new_strike, "right": right,
        "iv": round(iv, 4), "iv_source": iv_source,
        "T_entry_yr": round(T_entry, 6),
        "T_exit_yr": round(T_exit, 6),
        "entry_premium": round(entry_price, 3),
        "entry_delta": round(entry_delta, 4),
        "exit_premium": round(exit_price, 3),
        "exit_delta": round(exit_delta, 4),
    }


def _resolve_alt_strike(
    selection: str,
    direction_sign: int,
    spot: float,
    step: float = 5.0,
) -> tuple[float, str] | None:
    """Translate a strike_selection kind into a concrete (strike, right) pair.

    direction_sign +1 → LONG_CALLS (right='C'); -1 → LONG_PUTS (right='P').
    The ITM/OTM directions flip with put/call:
      - 'atm_minus_5pt_ITM' for LONG_CALLS = ATM - 5pt (lower strike = ITM for call)
                          for LONG_PUTS  = ATM + 5pt (higher strike = ITM for put)
      - 'atm_plus_5pt_OTM' for LONG_CALLS = ATM + 5pt; for LONG_PUTS = ATM - 5pt.
    Returns None on unknown selection.
    """
    if spot <= 0 or step <= 0:
        return None
    atm = round(spot / step) * step
    right = "C" if direction_sign > 0 else "P"
    if selection == "atm_call_only":
        return (float(atm), right)
    if selection == "atm_minus_5pt_ITM":
        offset = -step if direction_sign > 0 else step
        return (float(atm + offset), right)
    if selection == "atm_plus_5pt_OTM":
        offset = step if direction_sign > 0 else -step
        return (float(atm + offset), right)
    return None


def _lookup_chain_mid(
    db_cursor,
    *,
    timestamp_ms: int,
    strike: float,
    right: str,
    expiry_yyyymmdd: str,
    window_ms: int = 180_000,  # 3 min — capture is every 2 min so this brackets
) -> float | None:
    """Find the option mid closest in time to `timestamp_ms` for the
    (strike, right, expiry) triple. Returns None if no row exists within
    `window_ms` either direction.

    Falls back through mid → (bid+ask)/2 → last → none.
    """
    try:
        row = db_cursor.execute(
            """
            SELECT timestamp_ms, bid, ask, last, mid
            FROM historical_option_chain_point
            WHERE strike = ? AND right = ? AND expiry = ?
              AND timestamp_ms BETWEEN ? AND ?
            ORDER BY ABS(timestamp_ms - ?) ASC
            LIMIT 1
            """,
            (
                float(strike), right, expiry_yyyymmdd,
                timestamp_ms - window_ms, timestamp_ms + window_ms,
                timestamp_ms,
            ),
        ).fetchone()
    except Exception:
        return None
    if row is None:
        return None
    mid = row["mid"] if "mid" in row.keys() else None
    if mid is not None and mid == mid and mid > 0:
        return float(mid)
    bid = row["bid"] if "bid" in row.keys() else None
    ask = row["ask"] if "ask" in row.keys() else None
    if bid is not None and ask is not None and bid > 0 and ask > 0:
        return (float(bid) + float(ask)) / 2.0
    last = row["last"] if "last" in row.keys() else None
    if last is not None and last == last and last > 0:
        return float(last)
    return None


def _compute_alt_strike_dollars(
    db_cursor,
    *,
    selection: str,
    direction_sign: int,
    entry_spx: float,
    entry_ms: int,
    exit_ms: int | None,
    exit_spx: float | None,
    expiry_yyyymmdd: str,
    qty: int,
) -> tuple[float | None, str | None, str]:
    """Compute hypothetical P&L for an alt-strike variant from chain data.

    Returns (dollars, exit_reason_override, note):
      - dollars: None when chain data is missing — caller persists row with
        exit_reason='no_chain_data' and dollars=0.
      - exit_reason_override: 'no_chain_data' when entry mid missing; None
        otherwise (caller keeps the simulator's own exit reason).
      - note: human-readable strike picked (for variant_params snapshot).

    Uses spot_at_capture from the chain data to identify entry/exit
    minutes, but resolves the strike from `entry_spx` / `exit_spx` passed
    by the caller (entry_spx = parent's entry_spx_price; exit_spx =
    bar.close at exit_ms or last forward bar).
    """
    resolved = _resolve_alt_strike(selection, direction_sign, entry_spx)
    if resolved is None:
        return None, "no_chain_data", f"unknown_selection:{selection}"
    alt_strike, alt_right = resolved
    note = f"strike={alt_strike} {alt_right}"

    entry_mid = _lookup_chain_mid(
        db_cursor,
        timestamp_ms=entry_ms, strike=alt_strike, right=alt_right,
        expiry_yyyymmdd=expiry_yyyymmdd,
    )
    if entry_mid is None:
        return None, "no_chain_data", note

    # If no exit timestamp (e.g. no forward bars), use last available chain
    # point in the day for this strike as a best-effort.
    if exit_ms is None:
        return None, "no_chain_data", note + ":missing_exit"

    exit_mid = _lookup_chain_mid(
        db_cursor,
        timestamp_ms=exit_ms, strike=alt_strike, right=alt_right,
        expiry_yyyymmdd=expiry_yyyymmdd,
    )
    if exit_mid is None:
        return None, "no_chain_data", note + ":missing_exit_mid"

    dollars = (exit_mid - entry_mid) * qty * 100.0
    return float(dollars), None, note


def _evaluate_entry_filter(
    entry_filter: dict,
    direction_sign: int,
    prior_bars: list[dict],
) -> tuple[bool, str]:
    """Returns (passes, reason). `passes=False` means the trade is
    blocked at entry and the variant's row should be persisted with
    realized_r=0. Pure read-only — adding new filter kinds is just a
    new elif branch here."""
    kind = entry_filter.get("kind")
    params = entry_filter.get("params") or {}
    if kind == "direction_30m":
        # Block PUTS when SPX moved up ≥ threshold pts over the prior
        # 30 1-min bars; block CALLS when SPX moved down. Same logic as
        # ablation_backtest's direction filter — moved into the shadow
        # framework so we can isolate the filter's effect on real entries.
        threshold = float(params.get("threshold_pts", 3.0))
        if len(prior_bars) < 5:
            return True, "no_prior_data"
        spx_30m_change = prior_bars[-1]["close"] - prior_bars[0]["close"]
        if direction_sign > 0 and spx_30m_change < -threshold:
            return False, f"calls_blocked_spx_30m_{spx_30m_change:+.1f}pt"
        if direction_sign < 0 and spx_30m_change > threshold:
            return False, f"puts_blocked_spx_30m_{spx_30m_change:+.1f}pt"
        return True, "passed"
    return True, f"unknown_kind:{kind}"


# ---------------------------------------------------------------------------
# Parametric bar-walk simulator
# ---------------------------------------------------------------------------

def simulate_variant(
    entry_spx: float,
    direction_sign: int,
    fwd_bars: list[dict],
    variant: ExitVariant,
    risk_pts: float = 10.0,
    entry_ms: int | None = None,
) -> dict:
    """Walk fwd_bars in order, applying the variant's stop / targets /
    trail rules. Stop-priority on ambiguous bars (matches the rest of the
    research stack — conservative).

    `risk_pts` is the per-signal R-unit in SPX points (= |entry - stop|).
    Canonical post-2026-05-22 (Chat audit Priority 3): callers should pass
    the matched parent's |entry - stop_spx| so R numbers match
    daily_review / realistic_fill_estimator. Falls back to 10pt only for
    callers that haven't been migrated.

    `entry_ms` is the parent submitted_at in UTC ms. Required by the
    theta-tighten variants (added 2026-05-26); legacy callers may omit
    it and any theta-haircut logic will silently no-op.

    Returns:
        {realized_r, exit_reason, peak_r, exit_ms}

    exit_reason in {'no_data', 'stop', 'be_stop', 'be_stop_theta', 'targets',
    'trail', 'timeout', 'requires_option_price_evolution'}.
    """
    # Variants requiring option-price evolution can't be simulated by
    # the SPX-only bar walk. Return a sentinel so the row is still
    # persisted (for ledger continuity) but flagged as unevaluable.
    if variant.option_price_be_ladder:
        return {"realized_r": float("nan"),
                "exit_reason": "requires_option_price_evolution",
                "peak_r": 0.0, "exit_ms": None}

    if not fwd_bars:
        return {"realized_r": 0.0, "exit_reason": "no_data",
                "peak_r": 0.0, "exit_ms": None}

    realized_r = 0.0
    remaining_share = 1.0
    tiers_fired: set[int] = set()
    peak_r = 0.0
    # Dynamic stop — starts at variant.stop_r, ratchets via be_ladder.
    # Conservative ordering: a bar's adverse_r is checked against the
    # PREVIOUS bar's dynamic_stop_r (i.e., ratchet only takes effect
    # on the NEXT bar). Matches the live engine where the watcher
    # updates state after observing the bar, so adverse-move-this-bar
    # uses the stop in effect at bar start.
    dynamic_stop_r = variant.stop_r
    ladder = sorted(variant.be_ladder or [], key=lambda x: x[0])
    trail_enabled = (
        variant.trail_peak_floor_r is not None
        and variant.trail_pullback_r is not None
    )

    # Theta-tighten configuration (Option 1 — heuristic SPX-space theta
    # approximation, added 2026-05-26). Active only when (a) the variant
    # configures both knobs, (b) the variant has a be_ladder (theta drag
    # only matters once you have profit to protect), and (c) entry_ms is
    # supplied. After the BE ladder fires at least one rung, the dynamic
    # stop tightens UPWARD each bar past the kickin minute.
    theta_enabled = (
        variant.theta_tighten_per_min_pts is not None
        and variant.theta_kick_in_min is not None
        and ladder
        and entry_ms is not None
        and risk_pts > 0
    )
    theta_per_min_pts = variant.theta_tighten_per_min_pts or 0.0
    theta_kick_in = variant.theta_kick_in_min or 0.0
    # Track the highest BE-ladder rung that has been triggered. The
    # theta-tighten layer compounds ON TOP of this (capped so it can never
    # exceed peak_r — never force-exit a still-open winner).
    be_ladder_max_step_r = variant.stop_r  # i.e. no rung fired yet
    theta_stop_active = False  # True once first BE rung fires + kickin elapsed

    for b in fwd_bars:
        adverse_pts = b["low"] if direction_sign > 0 else b["high"]
        favor_pts = b["high"] if direction_sign > 0 else b["low"]
        adverse_r = direction_sign * (adverse_pts - entry_spx) / risk_pts
        favor_r = direction_sign * (favor_pts - entry_spx) / risk_pts

        # 1. Stop (priority on ambiguous bars). Uses the dynamic stop
        # as ratcheted by prior bars — this bar's peak update hasn't
        # happened yet.
        if adverse_r <= dynamic_stop_r and remaining_share > 0:
            realized_r += remaining_share * dynamic_stop_r
            # Distinguish three stop flavors: vanilla -1R fixed (stop),
            # BE-ladder ratcheted (be_stop), and additionally theta-tightened
            # on top of BE (be_stop_theta). Useful when slicing exit-reason
            # mix to see which mechanism saved/cost R.
            if dynamic_stop_r <= variant.stop_r + 1e-9:
                exit_reason = "stop"
            elif theta_stop_active and dynamic_stop_r > be_ladder_max_step_r + 1e-9:
                exit_reason = "be_stop_theta"
            else:
                exit_reason = "be_stop"
            return {"realized_r": realized_r,
                    "exit_reason": exit_reason,
                    "peak_r": peak_r, "exit_ms": b["timestamp_ms"]}

        # Update peak now that we know the bar's high didn't precede
        # the stop. (For partials/trail we check vs this bar's
        # favorable extreme below.)
        if favor_r > peak_r:
            peak_r = favor_r

        # BE ladder ratchet — only after peak update + stop check, so
        # the new (higher) stop applies starting next bar.
        for trig, new_stop in ladder:
            if peak_r >= trig and new_stop > dynamic_stop_r:
                dynamic_stop_r = new_stop
                if new_stop > be_ladder_max_step_r:
                    be_ladder_max_step_r = new_stop

        # Theta-tighten layer — additive over the BE ladder. Active once
        # ANY BE rung has fired (be_ladder_max_step_r > variant.stop_r) AND
        # time-held exceeds kick-in. Tightens monotonically UP, capped at
        # peak_r so it can never trigger an immediate exit on a still-
        # rallying bar. By design only nudges the stop toward profit —
        # never relaxes it (the ratchet uses max() with dynamic_stop_r).
        if theta_enabled and be_ladder_max_step_r > variant.stop_r + 1e-9:
            mins_held = (b["timestamp_ms"] - entry_ms) / 60000.0
            haircut_pts = max(0.0, mins_held - theta_kick_in) * theta_per_min_pts
            haircut_r = haircut_pts / risk_pts
            theta_stop_r = be_ladder_max_step_r + haircut_r
            # Don't tighten above the current bar's peak — that would force
            # a stop-out on the same bar that just made a new high (since
            # adverse_r checks the bar low, which can be < high).
            theta_stop_r = min(theta_stop_r, peak_r)
            if theta_stop_r > dynamic_stop_r:
                dynamic_stop_r = theta_stop_r
                theta_stop_active = True

        # 2. Targets in order — fire any not yet hit
        for idx, (r_level, share) in enumerate(variant.targets):
            if idx in tiers_fired:
                continue
            if favor_r >= r_level and remaining_share > 0:
                actual_share = min(share, remaining_share)
                realized_r += actual_share * r_level
                remaining_share -= actual_share
                tiers_fired.add(idx)

        # 3. All targets exhausted → exit
        if remaining_share <= 0.001:
            return {"realized_r": realized_r, "exit_reason": "targets",
                    "peak_r": peak_r, "exit_ms": b["timestamp_ms"]}

        # 4. Trail exit on residual (after target fires this bar)
        if trail_enabled and peak_r >= (variant.trail_peak_floor_r or 0):
            current_r = direction_sign * (b["close"] - entry_spx) / risk_pts
            trail_level = peak_r - (variant.trail_pullback_r or 0)
            if current_r <= trail_level and remaining_share > 0:
                realized_r += remaining_share * trail_level
                return {"realized_r": realized_r, "exit_reason": "trail",
                        "peak_r": peak_r, "exit_ms": b["timestamp_ms"]}

    # Timeout: residual exits at last close
    if remaining_share > 0:
        final_r = direction_sign * (fwd_bars[-1]["close"] - entry_spx) / risk_pts
        realized_r += remaining_share * final_r
    return {"realized_r": realized_r, "exit_reason": "timeout",
            "peak_r": peak_r, "exit_ms": fwd_bars[-1]["timestamp_ms"]}


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _ts_to_ms(ts_str: str) -> int:
    dt = datetime.fromisoformat(ts_str.replace(" ", "T"))
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return int(dt.timestamp() * 1000)


def _resolve_account_class(notes: str | None, accept: set[str]) -> bool:
    """live_order.notes encodes account_class as 'account_class=paper' etc."""
    if not notes:
        return ("paper" in accept) or ("unknown" in accept)
    cls = "unknown"
    for part in notes.split(";"):
        part = part.strip()
        if part.startswith("account_class="):
            cls = part.split("=", 1)[1].strip().lower() or "unknown"
            break
    if cls in accept:
        return True
    if cls == "unknown" and "paper" in accept:
        return True
    return False


def _parse_account_class_arg(raw: str) -> set[str]:
    if not raw:
        return {"paper"}
    raw = raw.strip().lower()
    if raw == "all":
        return {"paper", "live", "mock", "unknown"}
    return {x.strip() for x in raw.split(",") if x.strip()}


# ---------------------------------------------------------------------------
# Replay entry point
# ---------------------------------------------------------------------------

def run_replay(
    date_from: str,
    date_to: str,
    db_path: str = DEFAULT_DB,
    accept_classes: set[str] | None = None,
    variants: list[ExitVariant] | None = None,
    persist: bool = True,
) -> dict:
    """Replay every parent live_order in the window through every variant.

    Args:
        date_from / date_to: YYYY-MM-DD inclusive.
        db_path: sqlite path. Defaults to ~/.fef-terminal/signal_log.db.
        accept_classes: account_class filter (default {'paper'}).
        variants: list of ExitVariant to test. Defaults to
            REGISTERED_VARIANTS so adding a variant to the registry
            makes it appear in the next run automatically.
        persist: whether to upsert into shadow_exit_outcome.
    """
    accept = accept_classes or {"paper"}
    variants = variants or REGISTERED_VARIANTS
    if not variants:
        return {"date_from": date_from, "date_to": date_to,
                "n_parents": 0, "rows": [], "by_variant": {}}

    db = sqlite3.connect(db_path)
    db.row_factory = sqlite3.Row
    cur = db.cursor()

    # Auto-register variants into the ledger before doing any work.
    # Idempotent — first run creates rows, subsequent runs touch
    # last_seen_at + refresh metadata.
    try:
        upsert_variant_ledger(
            cur, REGISTERED_VARIANTS,
            tested_in="shadow_replay",
            sample_window=f"{date_from}_{date_to}",
        )
        db.commit()
    except sqlite3.OperationalError as e:
        # Table doesn't exist yet (sidecar hasn't booted to run init_db).
        # Continue without ledger — replay still works.
        print(f"[warn] variant_ledger not available: {e}", file=sys.stderr)

    # Include STOPPED parents — those are exactly the failure-mode cases
    # B+C+D / trail-variants are designed to test against (would they have
    # trail-exited with profit before the bracket stop hit?). Skip
    # CANCELLED / REJECTED / SUBMITTED-only parents — those never resulted
    # in real positions.
    parents = cur.execute(
        """
        SELECT id, submitted_at, symbol, strike, right, expiry, qty,
               avg_fill_price, entry_spx_price, spx_target_stop, status, notes
        FROM live_order
        WHERE bracket_role = 'parent'
          AND status IN ('FILLED', 'CLOSED', 'PARTIAL', 'STOPPED')
          AND entry_spx_price IS NOT NULL
          AND substr(submitted_at, 1, 10) BETWEEN ? AND ?
        ORDER BY submitted_at
        """,
        (date_from, date_to),
    ).fetchall()
    parents = [p for p in parents if _resolve_account_class(p["notes"], accept)]

    # Pull SPX 1-min bars with 2h forward buffer (last fire of day needs
    # full lookahead window).
    bars = cur.execute(
        """
        SELECT timestamp_ms, open, high, low, close
        FROM historical_bar
        WHERE symbol = 'SPX' AND bar_size = '1 min'
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

    def bars_before(ms: int, n: int) -> list[dict]:
        """Up to n 1-min bars strictly prior to ms, in chronological order."""
        idx = bisect.bisect_left(bar_ms_sorted, ms)
        return [bar_by_ms[m] for m in bar_ms_sorted[max(0, idx - n):idx]]

    # Build parent_id → regime_at_fire lookup. Match each parent to the
    # closest signal_iter row in the 5-minute window preceding its
    # submitted_at; parse extras JSON for regime_at_fire. Used by
    # meta-variants (regime_routes) — leaf variants ignore it.
    # ALSO build parent_id → signal_iter.extras lookup for BSM-priced
    # strike-offset variants (need vix_at_fire to bound IV).
    parent_regimes: dict[int, str] = {}
    parent_signal_extras: dict[int, str | None] = {}
    needs_signal_extras = (
        any(v.is_meta for v in variants)
        or any(v.strike_offset_pts is not None for v in variants)
    )
    if needs_signal_extras and parents:
        sig_rows = cur.execute(
            """
            SELECT id, timestamp, extras, session_date
            FROM signal_iter
            WHERE session_date BETWEEN ? AND ?
            ORDER BY timestamp
            """,
            (date_from, date_to),
        ).fetchall()
        sigs_by_session: dict[str, list[tuple[int, str]]] = defaultdict(list)
        for s in sig_rows:
            sigs_by_session[s["session_date"]].append((s["timestamp"], s["extras"]))
        for p in parents:
            parent_ms = _ts_to_ms(p["submitted_at"])
            session_date = p["submitted_at"][:10]
            best_regime = "UNKNOWN"
            best_extras: str | None = None
            best_diff = 5 * 60 * 1000  # 5min window
            for sig_ts, sig_extras in sigs_by_session.get(session_date, []):
                sig_ms = _ts_to_ms(sig_ts)
                diff = parent_ms - sig_ms
                if 0 <= diff <= best_diff:
                    best_diff = diff
                    best_extras = sig_extras
                    try:
                        ex = json.loads(sig_extras) if sig_extras else {}
                        regime = (
                            ex.get("regime_at_fire")
                            or (ex.get("regime_demoted") or {}).get("regime")
                            or "NORMAL"
                        )
                        best_regime = regime
                    except (json.JSONDecodeError, AttributeError):
                        pass
            parent_regimes[p["id"]] = best_regime
            parent_signal_extras[p["id"]] = best_extras

    rows: list[dict] = []
    for p in parents:
        entry_spx = float(p["entry_spx_price"])
        direction = "LONG_CALLS" if p["right"] == "C" else "LONG_PUTS"
        direction_sign = +1 if direction == "LONG_CALLS" else -1
        entry_ms = _ts_to_ms(p["submitted_at"])
        qty = int(p["qty"] or 0)
        session_date = p["submitted_at"][:10]
        # Per-signal R-unit (Chat audit Priority 3 — 2026-05-22). |entry -
        # spx_target_stop| in SPX points. Falls back to 10pt only if the
        # parent's spx_target_stop is missing (legacy rows pre-target tracking).
        stop_spx = p["spx_target_stop"] if "spx_target_stop" in p.keys() else None
        risk_pts = compute_r_unit(entry_spx, stop_spx, default_pts=10.0)

        for v in variants:
            # Resolve meta-variants to a leaf variant for this parent's
            # regime. Leaf variants simulate directly.
            if v.is_meta:
                regime = parent_regimes.get(p["id"], "UNKNOWN")
                routed_label = (v.regime_routes or {}).get(regime, v.default_route)
                leaf = _variant_by_label(routed_label) if routed_label else None
                if leaf is None or leaf.is_meta:
                    # Skip — misconfigured meta-variant; persist nothing.
                    continue
                sim_variant = leaf
                params_snapshot = {
                    "meta": v.to_dict(),
                    "resolved_regime": regime,
                    "resolved_leaf": leaf.to_dict(),
                }
            else:
                sim_variant = v
                params_snapshot = v.to_dict()

            # Entry filter — gate evaluated before the bar walk. When the
            # filter blocks the trade, persist realized_r=0 so portfolio
            # aggregates correctly treat this as "did not trade." Filter
            # is stored on the *meta or leaf* whichever exposed it; we
            # check both so a meta-variant can specify a filter and route
            # to leaf exit logic independently.
            applicable_filter = v.entry_filter or sim_variant.entry_filter
            if applicable_filter is not None:
                prior_bars = bars_before(entry_ms, 30)
                passes, filter_reason = _evaluate_entry_filter(
                    applicable_filter, direction_sign, prior_bars
                )
                if not passes:
                    rows.append({
                        "parent_order_id": p["id"],
                        "variant_label": v.label,
                        "variant_params": {
                            **params_snapshot,
                            "entry_filter_eval": filter_reason,
                        },
                        "session_date": session_date,
                        "entry_spx": entry_spx,
                        "direction": direction,
                        "qty": qty,
                        "realized_r": 0.0,
                        "exit_reason": f"blocked:{filter_reason}",
                        "peak_r": 0.0,
                        "exit_ms": None,
                        "dollars": 0.0,
                    })
                    continue

            end_ms = entry_ms + sim_variant.timeout_minutes * 60 * 1000
            fwd = bars_in_window(entry_ms, end_ms)
            out = simulate_variant(
                entry_spx, direction_sign, fwd, sim_variant,
                risk_pts=risk_pts, entry_ms=entry_ms,
            )
            # BSM-priced strike-offset variants (added 2026-05-26 per Report 26).
            # Same bar-walk exit timing as the baseline (so realized_r is
            # apples-to-apples), but dollars come from BSM applied at the
            # alternate strike (e.g. 15pt OTM instead of 35pt). Always
            # evaluable — no dependency on chain capture infrastructure.
            strike_offset = v.strike_offset_pts
            if strike_offset is None and sim_variant is not v:
                strike_offset = sim_variant.strike_offset_pts
            if strike_offset is not None:
                exit_ms_for_lookup = out.get("exit_ms")
                exit_spx_at_exit: float | None = None
                if fwd:
                    if exit_ms_for_lookup is not None:
                        for fb in fwd:
                            if fb["timestamp_ms"] == exit_ms_for_lookup:
                                exit_spx_at_exit = float(fb["close"])
                                break
                    if exit_spx_at_exit is None:
                        exit_spx_at_exit = float(fwd[-1]["close"])
                sig_extras = parent_signal_extras.get(p["id"])
                bsm_dollars, bsm_diag = _compute_bsm_strike_dollars(
                    cur,
                    strike_offset_pts=strike_offset,
                    direction_sign=direction_sign,
                    entry_spx=entry_spx,
                    entry_ms=entry_ms,
                    exit_ms=exit_ms_for_lookup,
                    exit_spx=exit_spx_at_exit,
                    session_date=session_date,
                    qty=qty,
                    signal_iter_extras_json=sig_extras,
                )
                rows.append({
                    "parent_order_id": p["id"],
                    "variant_label": v.label,
                    "variant_params": {
                        **params_snapshot,
                        "bsm_diag": bsm_diag,
                    },
                    "session_date": session_date,
                    "entry_spx": entry_spx,
                    "direction": direction,
                    "qty": qty,
                    "realized_r": round(out["realized_r"], 4),
                    "exit_reason": out["exit_reason"],
                    "peak_r": round(out["peak_r"], 4),
                    "exit_ms": out["exit_ms"],
                    "dollars": round(bsm_dollars, 2),
                })
                continue

            # Alt-strike variants: replace the R-derived dollar calc with
            # option-mid evolution from historical_option_chain_point. The
            # realized_r still reflects the bar-walk exit (so the exit
            # mechanism comparison stays apples-to-apples) but the dollars
            # are looked up from the chain table. When no chain data exists
            # for the entry minute (pre-capture sessions), persist with
            # exit_reason='no_chain_data' and dollars=0.
            strike_sel = v.strike_selection or sim_variant.strike_selection
            if strike_sel is not None:
                exit_ms_for_lookup = out.get("exit_ms")
                # Use the bar close at exit_ms (or last fwd bar) for the
                # exit-time SPX reference; the lookup itself keys on
                # timestamp_ms × strike × right.
                exit_spx_at_exit: float | None = None
                if fwd:
                    if exit_ms_for_lookup is not None:
                        for fb in fwd:
                            if fb["timestamp_ms"] == exit_ms_for_lookup:
                                exit_spx_at_exit = float(fb["close"])
                                break
                    if exit_spx_at_exit is None:
                        exit_spx_at_exit = float(fwd[-1]["close"])
                # The chain expiry is the same YYYYMMDD as the session_date
                # for 0DTE; the live capture writes session_date.replace('-', '').
                expiry_yyyymmdd = session_date.replace("-", "")
                dollars, exit_override, note = _compute_alt_strike_dollars(
                    cur,
                    selection=strike_sel,
                    direction_sign=direction_sign,
                    entry_spx=entry_spx, entry_ms=entry_ms,
                    exit_ms=exit_ms_for_lookup,
                    exit_spx=exit_spx_at_exit,
                    expiry_yyyymmdd=expiry_yyyymmdd,
                    qty=qty,
                )
                if dollars is None:
                    rows.append({
                        "parent_order_id": p["id"],
                        "variant_label": v.label,
                        "variant_params": {
                            **params_snapshot,
                            "alt_strike_note": note,
                        },
                        "session_date": session_date,
                        "entry_spx": entry_spx,
                        "direction": direction,
                        "qty": qty,
                        "realized_r": 0.0,
                        "exit_reason": exit_override or "no_chain_data",
                        "peak_r": round(out["peak_r"], 4),
                        "exit_ms": out["exit_ms"],
                        "dollars": 0.0,
                    })
                else:
                    rows.append({
                        "parent_order_id": p["id"],
                        "variant_label": v.label,
                        "variant_params": {
                            **params_snapshot,
                            "alt_strike_note": note,
                        },
                        "session_date": session_date,
                        "entry_spx": entry_spx,
                        "direction": direction,
                        "qty": qty,
                        "realized_r": round(out["realized_r"], 4),
                        "exit_reason": out["exit_reason"],
                        "peak_r": round(out["peak_r"], 4),
                        "exit_ms": out["exit_ms"],
                        "dollars": round(dollars, 2),
                    })
                continue

            rows.append({
                "parent_order_id": p["id"],
                "variant_label": v.label,  # meta label kept for grouping
                "variant_params": params_snapshot,
                "session_date": session_date,
                "entry_spx": entry_spx,
                "direction": direction,
                "qty": qty,
                "realized_r": round(out["realized_r"], 4),
                "exit_reason": out["exit_reason"],
                "peak_r": round(out["peak_r"], 4),
                "exit_ms": out["exit_ms"],
                "dollars": round(
                    out["realized_r"] * DOLLARS_PER_R_PER_CONTRACT * qty, 2
                ),
            })

    if persist and rows:
        now_iso = datetime.now(timezone.utc).isoformat()
        # Skip rows with NaN realized_r — placeholder variants
        # (option-price BE ladder etc.) can't satisfy the NOT NULL
        # constraint on realized_r and the ledger row already captures
        # the intent. SQLite binds NaN as NULL in some driver versions,
        # which would fail the constraint anyway.
        persist_rows = [r for r in rows if r["realized_r"] == r["realized_r"]]
        cur.executemany(
            """
            INSERT OR REPLACE INTO shadow_exit_outcome
                (parent_order_id, variant_label, session_date, computed_at,
                 entry_spx, direction, qty, variant_params,
                 realized_r, exit_reason, peak_r, exit_ms,
                 actual_realized_pnl, actual_realized_r)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, NULL, NULL)
            """,
            [
                (
                    r["parent_order_id"], r["variant_label"], r["session_date"],
                    now_iso, r["entry_spx"], r["direction"], r["qty"],
                    json.dumps(r["variant_params"]),
                    r["realized_r"], r["exit_reason"], r["peak_r"], r["exit_ms"],
                )
                for r in persist_rows
            ],
        )
        db.commit()
    db.close()

    # Aggregate by variant × session. NaN realized_r rows (variants
    # requiring option-price evolution etc.) are excluded from sums so
    # they don't poison the aggregates, but their count is surfaced
    # via n_unevaluable so the placeholder is visible.
    def _is_finite(x: float) -> bool:
        return x == x and x not in (float("inf"), float("-inf"))

    by_variant: dict[str, dict] = {}
    for v in variants:
        v_rows = [r for r in rows if r["variant_label"] == v.label]
        eval_rows = [r for r in v_rows if _is_finite(r["realized_r"])]
        unevaluable = len(v_rows) - len(eval_rows)
        by_session: dict[str, dict] = defaultdict(
            lambda: {"n": 0, "qty_total": 0, "sum_r": 0.0, "sum_dollars": 0.0,
                     "wins": 0, "losses": 0, "exit_reasons": defaultdict(int)}
        )
        for r in eval_rows:
            b = by_session[r["session_date"]]
            b["n"] += 1
            b["qty_total"] += r["qty"]
            b["sum_r"] += r["realized_r"]
            b["sum_dollars"] += r["dollars"]
            if r["realized_r"] > 0.05:
                b["wins"] += 1
            elif r["realized_r"] < -0.05:
                b["losses"] += 1
            b["exit_reasons"][r["exit_reason"]] += 1
        for s in by_session.values():
            s["exit_reasons"] = dict(s["exit_reasons"])
        by_variant[v.label] = {
            "params": v.to_dict(),
            "by_session": dict(by_session),
            "aggregate": {
                "n": len(eval_rows),
                "n_unevaluable": unevaluable,
                "sum_r": round(sum(r["realized_r"] for r in eval_rows), 4),
                "sum_dollars": round(sum(r["dollars"] for r in eval_rows), 2),
                "wins": sum(1 for r in eval_rows if r["realized_r"] > 0.05),
                "losses": sum(1 for r in eval_rows if r["realized_r"] < -0.05),
                "exit_reasons": dict(defaultdict(int, {
                    er: sum(1 for r in v_rows if r["exit_reason"] == er)
                    for er in {r["exit_reason"] for r in v_rows}
                })),
            },
        }

    return {
        "date_from": date_from,
        "date_to": date_to,
        "account_classes": sorted(accept),
        "variants": [v.label for v in variants],
        "n_parents": len({r["parent_order_id"] for r in rows}),
        "n_outcome_rows": len(rows),
        "by_variant": by_variant,
        "rows": rows,
    }


# ---------------------------------------------------------------------------
# Reporting
# ---------------------------------------------------------------------------

def format_report(result: dict) -> str:
    lines: list[str] = []
    lines.append(f"## Shadow Exit Replay: {result['date_from']} to {result['date_to']}")
    lines.append("")
    lines.append(f"- Account classes: {', '.join(result['account_classes'])}")
    lines.append(f"- Variants: {', '.join(result['variants'])}")
    lines.append(
        f"- Parent orders replayed: **{result['n_parents']}** "
        f"({result['n_outcome_rows']} outcome rows)"
    )
    lines.append("")
    lines.append("### Aggregate by variant")
    lines.append("")
    lines.append("| Variant | n | Sum R | Sum $ | W | L | WR | avg R |")
    lines.append("|---|---:|---:|---:|---:|---:|---:|---:|")
    for label, v in result["by_variant"].items():
        agg = v["aggregate"]
        wl = agg["wins"] + agg["losses"]
        wr = (agg["wins"] / wl * 100) if wl else 0
        avg_r = agg["sum_r"] / agg["n"] if agg["n"] else 0
        lines.append(
            f"| {label} | {agg['n']} | {agg['sum_r']:+.2f} | "
            f"{agg['sum_dollars']:+,.0f} | {agg['wins']} | {agg['losses']} | "
            f"{wr:.1f}% | {avg_r:+.3f} |"
        )

    lines.append("")
    lines.append("### Per-session × variant breakdown")
    lines.append("")
    lines.append("| Variant | Session | n | qty | Sum R | Sum $ | W/L | exit mix |")
    lines.append("|---|---|---:|---:|---:|---:|---:|---|")
    for label, v in result["by_variant"].items():
        for date in sorted(v["by_session"].keys()):
            b = v["by_session"][date]
            exit_summary = ", ".join(
                f"{k}:{v}" for k, v in sorted(b["exit_reasons"].items())
            )
            lines.append(
                f"| {label} | {date} | {b['n']} | {b['qty_total']} | "
                f"{b['sum_r']:+.2f} | {b['sum_dollars']:+,.0f} | "
                f"{b['wins']}/{b['losses']} | {exit_summary} |"
            )

    # Pair-wise deltas (each non-first variant vs the first)
    if len(result["by_variant"]) >= 2:
        labels = list(result["by_variant"].keys())
        ref_label = labels[0]
        lines.append("")
        lines.append(f"### Variant deltas vs `{ref_label}`")
        lines.append("")
        lines.append("| Variant | Δ Sum R | Δ $ | Δ avg R |")
        lines.append("|---|---:|---:|---:|")
        ref = result["by_variant"][ref_label]["aggregate"]
        for label in labels[1:]:
            cur = result["by_variant"][label]["aggregate"]
            dR = cur["sum_r"] - ref["sum_r"]
            d_dollars = cur["sum_dollars"] - ref["sum_dollars"]
            avg_ref = ref["sum_r"] / ref["n"] if ref["n"] else 0
            avg_cur = cur["sum_r"] / cur["n"] if cur["n"] else 0
            lines.append(
                f"| {label} | {dR:+.2f} | {d_dollars:+,.0f} | "
                f"{(avg_cur - avg_ref):+.3f} |"
            )

    return "\n".join(lines)


def _filter_variants(arg: str | None) -> list[ExitVariant]:
    if not arg:
        return REGISTERED_VARIANTS
    requested = {x.strip() for x in arg.split(",") if x.strip()}
    selected = [v for v in REGISTERED_VARIANTS if v.label in requested]
    unknown = requested - {v.label for v in REGISTERED_VARIANTS}
    if unknown:
        raise SystemExit(
            f"Unknown variant(s): {sorted(unknown)}. "
            f"Available: {[v.label for v in REGISTERED_VARIANTS]}"
        )
    return selected


def main() -> int:
    ap = argparse.ArgumentParser(
        description="Shadow exit-logic replay over live_order parents."
    )
    ap.add_argument("date_from", help="YYYY-MM-DD")
    ap.add_argument("date_to", help="YYYY-MM-DD")
    ap.add_argument("--db", default=DEFAULT_DB)
    ap.add_argument(
        "--account-class", default="paper",
        help="Filter by live_order.notes account_class. 'paper' (default), 'live', "
             "'mock', 'unknown', 'all', or comma-separated.",
    )
    ap.add_argument(
        "--variants", default="",
        help="Comma-separated variant labels to test. Default: all registered.",
    )
    ap.add_argument("--no-persist", action="store_true",
                    help="Skip writing shadow_exit_outcome rows (dry run).")
    ap.add_argument("--json", action="store_true",
                    help="Emit raw JSON instead of markdown.")
    args = ap.parse_args()

    accept = _parse_account_class_arg(args.account_class)
    variants = _filter_variants(args.variants)
    result = run_replay(
        args.date_from, args.date_to, args.db, accept,
        variants=variants, persist=not args.no_persist,
    )
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except (AttributeError, OSError):
        pass
    if args.json:
        print(json.dumps(
            {k: v for k, v in result.items() if k != "rows"},
            indent=2, default=str,
        ))
    else:
        print(format_report(result))
    return 0


if __name__ == "__main__":
    sys.exit(main())

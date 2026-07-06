"""Tests for the watcher-managed fixed_dollar exit decision (2026-06-30).

Covers _fixed_dollar_actions — the pure function that decides which staged
market sells / stop to fire from the live option mid vs the operator's $ targets.
"""

from __future__ import annotations

from fef_sidecar.engine.spx_target_watcher import _all_tiers_banked, _fixed_dollar_actions

# qty 10 split 3/4/3, targets $300/$500/$1000, stop -$500 (the live defaults).
QTY = 10
TIERS = [3, 4, 3]
TARGETS = [300.0, 500.0, 1000.0]
STOP = -500.0


def _act(entry, mid, fired=(False, False, False)):
    return _fixed_dollar_actions(
        entry_px=entry, mid=mid, original_qty=QTY,
        tier_qtys=TIERS, tier_fired=list(fired),
        d_targets=TARGETS, d_stop=STOP,
    )


def test_no_fire_below_first_target() -> None:
    # +$0.10 premium → +$100 on 10ct, below the $300 T1 and above the -$500 stop.
    assert _act(5.00, 5.10) == (0, [])


def test_t1_fires_alone() -> None:
    # +$0.35 → +$350 ≥ $300 (T1) but < $500 (T2).
    assert _act(5.00, 5.35) == (0, [("T1", 3)])


def test_t1_and_t2_fire_together_on_a_jump() -> None:
    # +$0.55 → +$550 clears both T1 and T2 in one check.
    assert _act(5.00, 5.55) == (0, [("T1", 3), ("T2", 4)])


def test_already_fired_tiers_are_skipped() -> None:
    # T1 already banked; +$550 now only fires T2.
    assert _act(5.00, 5.55, fired=(True, False, False)) == (0, [("T2", 4)])


def test_all_three_targets_fire() -> None:
    # +$1.00 → +$1000 clears T1/T2/T3.
    assert _act(5.00, 6.00) == (0, [("T1", 3), ("T2", 4), ("T3", 3)])


def test_stop_fires_all_remaining() -> None:
    # -$0.60 → open loss on the full 10ct = -$600 ≤ -$500 stop → sell all 10.
    assert _act(5.00, 4.40) == (10, [])


def test_stop_measured_on_remaining_qty_only() -> None:
    # T1+T2 banked (only 3ct remain). Same -$0.60 → -$180 on 3ct, NOT past the
    # -$500 stop; and full mark -$600 < targets → nothing fires.
    assert _act(5.00, 4.40, fired=(True, True, False)) == (0, [])


def test_stop_takes_priority_over_targets() -> None:
    # Degenerate inputs can't both trigger; verify stop short-circuits targets:
    # a huge loss returns a stop with no target fires.
    stop_qty, fires = _act(5.00, 1.00)
    assert stop_qty == 10 and fires == []


def test_missing_prices_no_action() -> None:
    assert _fixed_dollar_actions(
        entry_px=None, mid=5.0, original_qty=QTY, tier_qtys=TIERS,
        tier_fired=[False, False, False], d_targets=TARGETS, d_stop=STOP,
    ) == (0, [])
    assert _fixed_dollar_actions(
        entry_px=5.0, mid=None, original_qty=QTY, tier_qtys=TIERS,
        tier_fired=[False, False, False], d_targets=TARGETS, d_stop=STOP,
    ) == (0, [])


def test_zero_qty_tier_skipped() -> None:
    # A tier with 0 qty never fires even if its $ target is reached.
    out = _fixed_dollar_actions(
        entry_px=5.0, mid=6.0, original_qty=10, tier_qtys=[0, 10, 0],
        tier_fired=[False, False, False], d_targets=TARGETS, d_stop=STOP,
    )
    assert out == (0, [("T2", 10)])


def test_none_target_skipped() -> None:
    # A None $ target tier is skipped (e.g. operator left it blank).
    out = _fixed_dollar_actions(
        entry_px=5.0, mid=6.0, original_qty=10, tier_qtys=[3, 4, 3],
        tier_fired=[False, False, False], d_targets=[300.0, None, 1000.0],
        d_stop=STOP,
    )
    assert out == (0, [("T1", 3), ("T3", 3)])


# ---- cumulative_realized semantics (Option 3) --------------------------------
# TIERS 3/4/3, CUMULATIVE targets 300/500/1000 → per-tier INCREMENTS 300/200/500.
# Per-contract bar to fire: T1 300/3=100, T2 200/4=50, T3 500/3≈166.7 $/ct.


def _act_cum(entry, mid, fired=(False, False, False)):
    return _fixed_dollar_actions(
        entry_px=entry, mid=mid, original_qty=QTY,
        tier_qtys=TIERS, tier_fired=list(fired),
        d_targets=TARGETS, d_stop=STOP, semantics="cumulative_realized",
    )


def test_cum_no_fire_below_first_increment() -> None:
    # +$0.60 → +$60/ct. T2's raw increment ($50/ct) IS met, but the chain guard
    # blocks it because T1 ($100/ct) is not → nothing fires (no out-of-order T2).
    assert _act_cum(5.00, 5.60) == (0, [])


def test_cum_t1_t2_cofire_when_t1_clears() -> None:
    # +$1.00 → +$100/ct. T1 (needs 100) clears, and T2 (needs 50) rides with it.
    assert _act_cum(5.00, 6.00) == (0, [("T1", 3), ("T2", 4)])


def test_cum_all_three_bank_the_top_target() -> None:
    # +$1.70 → +$170/ct clears T3's $166.7/ct bar → all three fire. Slices earn
    # 3*170 + 4*170 + 3*170 well past the 1000 cumulative target.
    assert _act_cum(5.00, 6.70) == (0, [("T1", 3), ("T2", 4), ("T3", 3)])


def test_cum_chain_guard_blocks_t2_before_t1() -> None:
    # Direct guard proof: at +$0.60 T2 alone WOULD fire on its own increment
    # ($60/ct*4=$240 ≥ $200) but must not, because T1 hasn't banked yet.
    stop_qty, fires = _act_cum(5.00, 5.60)
    assert ("T2", 4) not in fires and fires == []


def test_cum_resumes_after_t1_t2_already_banked() -> None:
    # T1+T2 booked; +$1.70 now fires only T3 (chain intact through fired tiers).
    assert _act_cum(5.00, 6.70, fired=(True, True, False)) == (0, [("T3", 3)])


def test_cum_stop_path_unchanged() -> None:
    # The stop is semantics-independent: -$0.60 → -$600 on 10ct ≤ -$500 → sell all.
    assert _act_cum(5.00, 4.40) == (10, [])


# ---- _all_tiers_banked: the CLOSED gate that prevents orphaned runners --------
# Regression guard for the 2026-07-01 7520C bug: a parent must only flip to
# CLOSED when the BANKED tiers cover the full original position, never on flags.


def test_banked_full_position_can_close() -> None:
    # 3-lot 1/1/1, all three actually sold → safe to mark CLOSED.
    assert _all_tiers_banked([1, 1, 1], [True, True, True], 3) is True


def test_banked_open_runner_must_not_close() -> None:
    # The exact 7520C case: T1's sell was refused (flag NOT set post-fix), T2+T3
    # sold → 1 contract still open → NOT banked → parent must stay watched.
    assert _all_tiers_banked([1, 1, 1], [False, True, True], 3) is False


def test_banked_tiers_undersum_qty_never_closes() -> None:
    # Tiers only account for 2 of a 3-lot; even all-fired leaves 1 open.
    assert _all_tiers_banked([1, 1, 0], [True, True, True], 3) is False


def test_banked_uneven_tiers() -> None:
    assert _all_tiers_banked([3, 4, 3], [True, True, True], 10) is True
    assert _all_tiers_banked([3, 4, 3], [True, True, False], 10) is False  # 7 of 10 sold

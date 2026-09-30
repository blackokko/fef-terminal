"""Pattern matching engine for codifying discretionary setups.

A Pattern is a named set of conditions. Each condition compares a field
(extracted from the current FEF iter's inputs + verdict) against a value
using an operator. When ALL conditions match, the pattern fires.

User-defined patterns persist in the `pattern` SQLite table. Matches log
to `pattern_match` and broadcast over WS as PatternMatchEvent.

IMPROVEMENTS #R (feature-loaded).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from fef_sidecar import et_time
from fef_sidecar.engine.types import FEFInputs, FEFVerdict


# ---------------------------------------------------------------------------
# Field extraction — flattens FEFInputs + FEFVerdict into a value dict
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class FieldDef:
    name: str
    label: str
    kind: str  # "number" | "int" | "bool" | "enum"
    enum_values: tuple[str, ...] = ()
    description: str = ""


# Single source of truth — frontend pulls this list via /patterns/fields
# to populate the condition builder dropdowns.
FIELDS: tuple[FieldDef, ...] = (
    FieldDef("spx_price", "SPX price", "number", description="Current SPX index value"),
    FieldDef("drift_cum_net_m", "Drift cum net ($M)", "number", description="Cumulative session net premium flow"),
    FieldDef("drift_bias", "Drift bias", "enum", ("BULLISH", "BEARISH", "NEUTRAL")),
    FieldDef("ntm_max_pain", "NTM max pain", "number"),
    FieldDef("ntm_mp_distance", "NTM MP distance (signed pts)", "number", description="spot - mp"),
    FieldDef("ntm_mp_distance_abs", "NTM MP distance (abs pts)", "number"),
    FieldDef("raw_max_pain", "Raw chain max pain", "number"),
    FieldDef("top_gex_strike", "Top GEX wall strike", "number"),
    FieldDef("top_gex_net_m", "Top GEX wall net ($M)", "number"),
    FieldDef("top_dex_strike", "Top DEX wall strike", "number"),
    FieldDef("top_dex_net_m", "Top DEX wall net ($M)", "number"),
    FieldDef("wall_proximity_pts", "Wall proximity (pts)", "number"),
    FieldDef("aggression_call_pct", "Call aggression %", "number"),
    FieldDef("aggression_put_pct", "Put aggression %", "number"),
    FieldDef("aggression_diff", "Call − put aggression %", "number"),
    FieldDef("iv_rank_call_pct", "IVR call %", "number"),
    FieldDef("iv_rank_put_pct", "IVR put %", "number"),
    FieldDef("delta_price", "ΔPrice 3-iter (pts)", "number"),
    FieldDef("delta_drift_m", "ΔDrift 3-iter ($M)", "number"),
    FieldDef("fe_ratio", "FE ratio", "number"),
    FieldDef("aligned", "Aligned", "bool"),
    FieldDef("absorption", "Absorption", "bool"),
    FieldDef("direction", "Verdict direction", "enum", ("LONG_CALLS", "LONG_PUTS", "WAIT")),
    FieldDef("confidence", "Verdict confidence", "enum", ("HIGH", "MEDIUM", "LOW", "NONE")),
    FieldDef("iter_num", "Iter number", "int"),
    FieldDef("minutes_since_open", "Minutes since open (ET)", "int"),
    FieldDef("vix", "VIX", "number"),
)

FIELD_BY_NAME = {f.name: f for f in FIELDS}


def extract_fields(
    inputs: FEFInputs,
    verdict: FEFVerdict,
    *,
    vix: float | None = None,
    iter_num: int | None = None,
) -> dict[str, Any]:
    """Flatten one iter's full context into a name->value dict for matching."""
    now_et = et_time.now_et()
    minutes_since_open = (now_et.hour - 9) * 60 + (now_et.minute - 30)

    top_gex = inputs.top_gex_walls[0] if inputs.top_gex_walls else None
    top_dex = inputs.top_dex_walls[0] if inputs.top_dex_walls else None

    ntm_dist = inputs.spx_price - inputs.ntm_max_pain

    aggression_diff = None
    if inputs.aggression_call_pct is not None and inputs.aggression_put_pct is not None:
        aggression_diff = inputs.aggression_call_pct - inputs.aggression_put_pct

    return {
        "spx_price": inputs.spx_price,
        "drift_cum_net_m": inputs.drift_cum_net_m,
        "drift_bias": inputs.drift_bias,
        "ntm_max_pain": inputs.ntm_max_pain,
        "ntm_mp_distance": ntm_dist,
        "ntm_mp_distance_abs": abs(ntm_dist),
        "raw_max_pain": inputs.raw_max_pain,
        "top_gex_strike": top_gex.strike if top_gex else None,
        "top_gex_net_m": top_gex.net_m if top_gex else None,
        "top_dex_strike": top_dex.strike if top_dex else None,
        "top_dex_net_m": top_dex.net_m if top_dex else None,
        "wall_proximity_pts": verdict.nearest_wall_distance,
        "aggression_call_pct": inputs.aggression_call_pct,
        "aggression_put_pct": inputs.aggression_put_pct,
        "aggression_diff": aggression_diff,
        "iv_rank_call_pct": inputs.iv_rank_call_pct,
        "iv_rank_put_pct": inputs.iv_rank_put_pct,
        "delta_price": verdict.delta_price,
        "delta_drift_m": verdict.delta_drift_m,
        "fe_ratio": verdict.fe_ratio,
        "aligned": verdict.aligned,
        "absorption": verdict.absorption,
        "direction": verdict.direction.value,
        "confidence": verdict.confidence.value,
        "iter_num": iter_num,
        "minutes_since_open": minutes_since_open,
        "vix": vix,
    }


# ---------------------------------------------------------------------------
# Condition evaluation
# ---------------------------------------------------------------------------


VALID_OPERATORS = frozenset(
    {"==", "!=", "<", "<=", ">", ">=", "in", "not_in", "between"}
)


def evaluate_condition(field_value: Any, operator: str, value: Any) -> bool:
    """Apply one comparison. Returns False if field_value is None (missing data)."""
    if field_value is None:
        return False
    if operator not in VALID_OPERATORS:
        return False
    try:
        if operator == "==":
            return field_value == value
        if operator == "!=":
            return field_value != value
        if operator == "<":
            return field_value < value
        if operator == "<=":
            return field_value <= value
        if operator == ">":
            return field_value > value
        if operator == ">=":
            return field_value >= value
        if operator == "in":
            return field_value in value
        if operator == "not_in":
            return field_value not in value
        if operator == "between":
            lo, hi = value
            return lo <= field_value <= hi
    except (TypeError, ValueError):
        return False
    return False


def evaluate_pattern(
    conditions: list[dict[str, Any]],
    field_values: dict[str, Any],
) -> tuple[bool, list[dict[str, Any]]]:
    """All conditions AND-ed. Returns (matched, condition_results).

    condition_results is a list of {field, operator, value, observed, matched}
    for transparent debugging — useful for the 'why didn't this fire' view.
    """
    results: list[dict[str, Any]] = []
    all_matched = True
    for c in conditions:
        field = c.get("field")
        op = c.get("operator")
        target = c.get("value")
        observed = field_values.get(field)
        matched = evaluate_condition(observed, op, target)
        results.append({
            "field": field,
            "operator": op,
            "value": target,
            "observed": observed,
            "matched": matched,
        })
        if not matched:
            all_matched = False
    return all_matched, results


# ---------------------------------------------------------------------------
# Default starter templates
# ---------------------------------------------------------------------------


DEFAULT_TEMPLATES: tuple[dict[str, Any], ...] = (
    # ---------- FEF core setups ----------
    {
        "name": "FEF: Aligned HIGH long calls",
        "description": "Model fired HIGH-conviction LONG_CALLS — aligned ΔPrice + ΔDrift, all gates passed",
        "conditions": [
            {"field": "aligned", "operator": "==", "value": True},
            {"field": "confidence", "operator": "==", "value": "HIGH"},
            {"field": "direction", "operator": "==", "value": "LONG_CALLS"},
        ],
    },
    {
        "name": "FEF: Aligned HIGH long puts",
        "description": "Model fired HIGH-conviction LONG_PUTS — aligned ΔPrice + ΔDrift, all gates passed",
        "conditions": [
            {"field": "aligned", "operator": "==", "value": True},
            {"field": "confidence", "operator": "==", "value": "HIGH"},
            {"field": "direction", "operator": "==", "value": "LONG_PUTS"},
        ],
    },
    {
        "name": "FEF: Wall collapse special",
        "description": "Dominant wall lost >20% in one iter — HIGH conviction without alignment requirement",
        "conditions": [
            {"field": "confidence", "operator": "==", "value": "HIGH"},
            {"field": "aligned", "operator": "==", "value": False},
        ],
    },
    {
        "name": "FEF: Bullish absorption",
        "description": "Drift accumulating bullishly but price refusing — reversal candidate brewing",
        "conditions": [
            {"field": "drift_bias", "operator": "==", "value": "BULLISH"},
            {"field": "drift_cum_net_m", "operator": ">", "value": 20.0},
            {"field": "aligned", "operator": "==", "value": False},
            {"field": "fe_ratio", "operator": ">", "value": 5.0},
        ],
    },
    {
        "name": "FEF: Bearish absorption",
        "description": "Drift accumulating bearishly but price refusing — reversal candidate brewing",
        "conditions": [
            {"field": "drift_bias", "operator": "==", "value": "BEARISH"},
            {"field": "drift_cum_net_m", "operator": "<", "value": -20.0},
            {"field": "aligned", "operator": "==", "value": False},
            {"field": "fe_ratio", "operator": ">", "value": 5.0},
        ],
    },
    {
        "name": "FEF: FE explosion (no alignment yet)",
        "description": "Extreme effort/result mismatch — big drift, small price move. Watch for the resolution",
        "conditions": [
            {"field": "fe_ratio", "operator": ">", "value": 10.0},
            {"field": "aligned", "operator": "==", "value": False},
        ],
    },
    # ---------- Wall structure setups ----------
    {
        "name": "Wall: Bullish wall stack",
        "description": "Top GEX call wall AND top DEX call wall both stacked above spot — strong floor",
        "conditions": [
            {"field": "top_gex_net_m", "operator": ">", "value": 30.0},
            {"field": "top_dex_net_m", "operator": ">", "value": 500.0},
            {"field": "drift_bias", "operator": "==", "value": "BULLISH"},
        ],
    },
    {
        "name": "Wall: Bearish wall stack",
        "description": "Top GEX put wall AND top DEX put wall stacked below spot — strong ceiling",
        "conditions": [
            {"field": "top_gex_net_m", "operator": "<", "value": -30.0},
            {"field": "top_dex_net_m", "operator": "<", "value": -500.0},
            {"field": "drift_bias", "operator": "==", "value": "BEARISH"},
        ],
    },
    {
        "name": "Wall: Test + bullish bias",
        "description": "Spot pinned near dominant wall, bullish drift confirming defense",
        "conditions": [
            {"field": "wall_proximity_pts", "operator": "<", "value": 8.0},
            {"field": "drift_bias", "operator": "==", "value": "BULLISH"},
            {"field": "top_gex_net_m", "operator": ">", "value": 30.0},
        ],
    },
    {
        "name": "Pin: regime entry",
        "description": "Tight pin near wall, no absorption — sit out, classic pin day",
        "conditions": [
            {"field": "absorption", "operator": "==", "value": False},
            {"field": "wall_proximity_pts", "operator": "<", "value": 10.0},
            {"field": "fe_ratio", "operator": "<", "value": 1.0},
        ],
    },
    # ---------- Time-of-day setups ----------
    {
        "name": "Time: AM trend reversal candidate",
        "description": "After 10:00 ET, drift opposes price direction with magnitude — reversal brewing",
        "conditions": [
            {"field": "minutes_since_open", "operator": ">", "value": 30},
            {"field": "minutes_since_open", "operator": "<", "value": 120},
            {"field": "absorption", "operator": "==", "value": True},
            {"field": "fe_ratio", "operator": ">", "value": 6.0},
        ],
    },
    {
        "name": "Time: Lunchtime coil",
        "description": "11:00-13:00 ET, tight range, low aggression — position for afternoon",
        "conditions": [
            {"field": "minutes_since_open", "operator": "between", "value": [90, 210]},
            {"field": "fe_ratio", "operator": "<", "value": 2.0},
            {"field": "wall_proximity_pts", "operator": "<", "value": 12.0},
            {"field": "drift_bias", "operator": "==", "value": "NEUTRAL"},
        ],
    },
    {
        "name": "Time: Power hour MP magnet",
        "description": "After 14:30 ET, spot stretched from NTM MP, neutral/weak drift — mean revert candidate",
        "conditions": [
            {"field": "minutes_since_open", "operator": ">", "value": 300},
            {"field": "ntm_mp_distance_abs", "operator": ">", "value": 15.0},
            {"field": "drift_bias", "operator": "in", "value": ["NEUTRAL", "BEARISH"]},
        ],
    },
    {
        "name": "Time: Power hour bull breakout",
        "description": "After 14:00 ET, aligned HIGH long calls — afternoon trend continuation",
        "conditions": [
            {"field": "minutes_since_open", "operator": ">", "value": 270},
            {"field": "aligned", "operator": "==", "value": True},
            {"field": "confidence", "operator": "==", "value": "HIGH"},
            {"field": "direction", "operator": "==", "value": "LONG_CALLS"},
        ],
    },
    {
        "name": "Time: Power hour bear breakout",
        "description": "After 14:00 ET, aligned HIGH long puts — afternoon trend continuation",
        "conditions": [
            {"field": "minutes_since_open", "operator": ">", "value": 270},
            {"field": "aligned", "operator": "==", "value": True},
            {"field": "confidence", "operator": "==", "value": "HIGH"},
            {"field": "direction", "operator": "==", "value": "LONG_PUTS"},
        ],
    },
    # ---------- Vol / aggression setups ----------
    {
        "name": "Vol: VIX-confirmed bullish break",
        "description": "VIX > 18 + aligned HIGH long calls — vol expansion confirming directional move",
        "conditions": [
            {"field": "vix", "operator": ">", "value": 18.0},
            {"field": "aligned", "operator": "==", "value": True},
            {"field": "confidence", "operator": "==", "value": "HIGH"},
            {"field": "direction", "operator": "==", "value": "LONG_CALLS"},
        ],
    },
    {
        "name": "Vol: Aggression spike bullish",
        "description": "Call aggression > put aggression by 8%+ with bullish drift — buyers chasing",
        "conditions": [
            {"field": "aggression_diff", "operator": ">", "value": 8.0},
            {"field": "drift_bias", "operator": "==", "value": "BULLISH"},
            {"field": "drift_cum_net_m", "operator": ">", "value": 10.0},
        ],
    },
)

"""PHASE DETECTOR — read WHERE in a move we are (ignition vs exhaustion), not just direction.

Motivation (2026-07-01 opening dump, validated live): the engine's failure was TIMING, not
direction — it followed a dump into PUTS at the capitulation low (worst entry) while the right
plays were (a) ride the dump early with puts, or (b) fade the exhaustion with calls (the user's
+$25k 7470C). Blindly inverting to "always fade" just gets chopped mid-move. So we score the move's
PHASE from a confluence of mechanical pillars and emit RIDE / FADE / WAIT.

SIX PILLARS (all mechanical, from data we already have), measured over a rolling window:
  1. VELOCITY   — SPX pt/min over ~75s. Ignition = |v| large; exhaustion = v crossed toward/through 0.
  2. EXTENSION  — distance off the rolling low/high (bounce-off-the-low / rejection-off-the-high).
  3. DRIFT      — drift_bias flip (BEARISH->BULLISH = bullish turn, and vice versa).
  4. AGGRESSION — call%-put% spread flip (put-panic exhausting -> calls stepping in, and vice versa).
  5. IV         — ATM/near IV climax->rollover (vol exhaustion) vs IV expanding (ignition).
  6. MAX_PAIN   — price overshoot beyond the max-pain pin (mean-reversion pull) vs breaking the pin.

Today at the low (9:38-39, the user's entry) 6/6 fade-up pillars fired; mid-dump (9:32) ~1/6 -> WAIT;
ignition (9:30) 3+ ride-down pillars.

CALIBRATION (2026-07-01 backtest, 866 sessions 2022-2025, independent IBKR ruler):
  * The loose fade_min=4 default OVER-FIRES (~13 signals/session, 47.6% hit, +0.03pt = no edge).
  * fade_min is a hard frequency valve; only fade_min>=5 (~0.1-0.4 signals/session) is both sane and
    positive. Defaults below are the recommended high-conviction config: vel_ignite=16, min_move=18,
    bounce_pt=3, fade_min=5.
  * RIDE is a STRUCTURAL LOSER at this resolution (pooled +30 = 40% hit / -41.6pt — riding the
    highest-velocity minute = buying the exhaustion bar). RIDE_MIN=7 > the 6-pillar max DISABLES it.
    The detector ships as a FADE-only "fade the exhaustion of a sharp move" gate.
  * Edge is real but modest/fragile (clustered t~+1.9), concentrated in high-vol regimes (2022, 2025),
    mostly PRICE-driven (velocity-stall + bounce-off-extreme), flow/IV/max-pain corroborating.
Lower min_move (12 / 8) trades conviction for frequency (the "balanced" preset) but small/choppy moves
show little edge. All thresholds stay tunable per the operator's settings.

This is a PURE, unit-tested module — no I/O, no engine coupling. The engine feeds it PhaseSamples and
gates on the returned PhaseVerdict.
"""
from __future__ import annotations

from collections import deque
from dataclasses import dataclass, field


@dataclass
class PhaseSample:
    ts_ms: int
    spx: float
    drift_bias: str | None = None          # "BULLISH" / "BEARISH" / "NEUTRAL"
    call_pct: float | None = None           # aggression call %
    put_pct: float | None = None            # aggression put %
    atm_iv: float | None = None             # ATM / near-money IV (e.g. 0.23)
    max_pain: float | None = None


@dataclass
class PhaseVerdict:
    state: str                              # "RIDE" / "FADE" / "WAIT"
    direction: str | None                   # "LONG_CALLS" / "LONG_PUTS" / None
    score: int = 0
    threshold: int = 0
    reasons: list[str] = field(default_factory=list)

    @property
    def actionable(self) -> bool:
        return self.state in ("RIDE", "FADE") and self.direction is not None


# --- tunable thresholds (defaults calibrated to the 2026-07-01 open move) --------------------
WINDOW_SEC = 300          # rolling context window
VEL_SEC = 75              # velocity lookback
VEL_IGNITE = 16.0         # |pt/min| ignition thrust (backtest: 16 sharpens the FADE setup)
VEL_STALL = 3.0           # |pt/min| under which the move has stalled
MIN_MOVE = 18.0           # min window range (pt) worth trading (backtest high-conviction floor)
BOUNCE_PT = 3.0           # pt off the extreme that confirms a reversal
IV_DELTA = 0.006          # IV climb/rollover magnitude
MP_OVERSHOOT = 8.0        # pt beyond the max-pain pin = overshoot
FADE_MIN = 5              # confluence to FADE (backtest: only >=5 is sane + positive)
RIDE_MIN = 7              # > the 6-pillar max => RIDE DISABLED (backtest: chasing thrust loses)


class PhaseDetector:
    def __init__(self, *, window_sec: int = WINDOW_SEC, vel_sec: int = VEL_SEC,
                 vel_ignite: float = VEL_IGNITE, vel_stall: float = VEL_STALL,
                 min_move: float = MIN_MOVE, bounce_pt: float = BOUNCE_PT,
                 iv_delta: float = IV_DELTA, mp_overshoot: float = MP_OVERSHOOT,
                 fade_min: int = FADE_MIN, ride_min: int = RIDE_MIN):
        self.window_ms = window_sec * 1000
        self.vel_ms = vel_sec * 1000
        self.vel_ignite = vel_ignite
        self.vel_stall = vel_stall
        self.min_move = min_move
        self.bounce_pt = bounce_pt
        self.iv_delta = iv_delta
        self.mp_overshoot = mp_overshoot
        self.fade_min = fade_min
        self.ride_min = ride_min
        self._w: deque[PhaseSample] = deque()

    # -- helpers -------------------------------------------------------------
    def _velocity(self) -> float | None:
        """pt/min over the last VEL_SEC (positive = up)."""
        if len(self._w) < 2:
            return None
        cur = self._w[-1]
        ref = None
        for s in self._w:
            if cur.ts_ms - s.ts_ms <= self.vel_ms:
                ref = s
                break
        if ref is None or ref.ts_ms == cur.ts_ms:
            return None
        return (cur.spx - ref.spx) / ((cur.ts_ms - ref.ts_ms) / 60000.0)

    def _extreme(self):
        los = min(self._w, key=lambda s: s.spx)
        his = max(self._w, key=lambda s: s.spx)
        return los, his

    # -- main ----------------------------------------------------------------
    def update(self, s: PhaseSample) -> PhaseVerdict:
        self._w.append(s)
        while self._w and s.ts_ms - self._w[0].ts_ms > self.window_ms:
            self._w.popleft()
        if len(self._w) < 3:
            return PhaseVerdict("WAIT", None, reasons=["warming up"])

        cur = self._w[-1]
        first = self._w[0]
        vel = self._velocity()
        low_s, high_s = self._extreme()
        rng = high_s.spx - low_s.spx
        off_low = cur.spx - low_s.spx
        off_high = high_s.spx - cur.spx
        down_leg = low_s.ts_ms > high_s.ts_ms      # low came after high -> a down move
        up_leg = high_s.ts_ms > low_s.ts_ms
        spread = ((cur.call_pct or 0) - (cur.put_pct or 0))
        first_spread = ((first.call_pct or 0) - (first.put_pct or 0))
        ivs = [x.atm_iv for x in self._w if x.atm_iv is not None]
        iv_peak = max(ivs) if ivs else None
        iv_min = min(ivs) if ivs else None

        # ---- FADE-UP (buy CALLS: a down-move is exhausting) ----
        fade_up, fu = [], 0
        if vel is not None and rng >= self.min_move and down_leg and vel >= -self.vel_stall:
            fu += 1; fade_up.append(f"stall(v={vel:+.1f}/min after -{rng:.0f}pt drop)")
        if rng >= self.min_move and off_low >= self.bounce_pt and cur.spx < high_s.spx:
            fu += 1; fade_up.append(f"bounce(+{off_low:.1f}pt off low)")
        if cur.drift_bias == "BULLISH" and first.drift_bias in ("BEARISH", "NEUTRAL"):
            fu += 1; fade_up.append("drift flip->BULLISH")
        if spread > 0 and first_spread <= 0:
            fu += 1; fade_up.append(f"aggr flip->calls({spread:+.0f})")
        if iv_peak is not None and cur.atm_iv is not None and iv_peak - cur.atm_iv >= self.iv_delta:
            fu += 1; fade_up.append(f"IV rollover(-{iv_peak - cur.atm_iv:.3f})")
        if cur.max_pain and cur.spx < cur.max_pain - self.mp_overshoot:
            fu += 1; fade_up.append(f"below max-pain by {cur.max_pain - cur.spx:.0f}pt")

        # ---- FADE-DOWN (buy PUTS: an up-move is exhausting) ----
        fade_dn, fd = [], 0
        if vel is not None and rng >= self.min_move and up_leg and vel <= self.vel_stall:
            fd += 1; fade_dn.append(f"stall(v={vel:+.1f}/min after +{rng:.0f}pt rip)")
        if rng >= self.min_move and off_high >= self.bounce_pt and cur.spx > low_s.spx:
            fd += 1; fade_dn.append(f"reject(-{off_high:.1f}pt off high)")
        if cur.drift_bias == "BEARISH" and first.drift_bias in ("BULLISH", "NEUTRAL"):
            fd += 1; fade_dn.append("drift flip->BEARISH")
        if spread < 0 and first_spread >= 0:
            fd += 1; fade_dn.append(f"aggr flip->puts({spread:+.0f})")
        if iv_peak is not None and cur.atm_iv is not None and iv_peak - cur.atm_iv >= self.iv_delta:
            fd += 1; fade_dn.append(f"IV rollover(-{iv_peak - cur.atm_iv:.3f})")
        if cur.max_pain and cur.spx > cur.max_pain + self.mp_overshoot:
            fd += 1; fade_dn.append(f"above max-pain by {cur.spx - cur.max_pain:.0f}pt")

        # ---- RIDE-DOWN (buy PUTS: a fresh breakdown igniting) ----
        # REQUIRES an active down-thrust: "riding" means the move is still going. Once
        # velocity stalls (near the low), RIDE turns OFF so it can't hold puts into the
        # capitulation — that hand-off to FADE is the whole point.
        ride_dn, rd = [], 0
        if vel is not None and vel <= -self.vel_ignite:
            rd += 1; ride_dn.append(f"thrust(v={vel:+.1f}/min)")
            if rng >= self.min_move and off_low <= self.bounce_pt:
                rd += 1; ride_dn.append("at/near new low")
            if cur.drift_bias == "BEARISH":
                rd += 1; ride_dn.append("drift BEARISH")
            if spread < 0:
                rd += 1; ride_dn.append(f"put-dominant({spread:+.0f})")
            if iv_min is not None and cur.atm_iv is not None and cur.atm_iv - iv_min >= self.iv_delta:
                rd += 1; ride_dn.append("IV expanding")
            if cur.max_pain and cur.spx < cur.max_pain:
                rd += 1; ride_dn.append("breaking below max-pain")

        # ---- RIDE-UP (buy CALLS: a fresh breakout igniting) — same active-thrust gate ----
        ride_up, ru = [], 0
        if vel is not None and vel >= self.vel_ignite:
            ru += 1; ride_up.append(f"thrust(v={vel:+.1f}/min)")
            if rng >= self.min_move and off_high <= self.bounce_pt:
                ru += 1; ride_up.append("at/near new high")
            if cur.drift_bias == "BULLISH":
                ru += 1; ride_up.append("drift BULLISH")
            if spread > 0:
                ru += 1; ride_up.append(f"call-dominant({spread:+.0f})")
            if iv_min is not None and cur.atm_iv is not None and cur.atm_iv - iv_min >= self.iv_delta:
                ru += 1; ride_up.append("IV expanding")
            if cur.max_pain and cur.spx > cur.max_pain:
                ru += 1; ride_up.append("breaking above max-pain")

        # ---- pick the winner (FADE gated tighter than RIDE) ----
        cands = [
            ("FADE", "LONG_CALLS", fu, self.fade_min, fade_up),
            ("FADE", "LONG_PUTS", fd, self.fade_min, fade_dn),
            ("RIDE", "LONG_PUTS", rd, self.ride_min, ride_dn),
            ("RIDE", "LONG_CALLS", ru, self.ride_min, ride_up),
        ]
        best = None
        for state, direction, score, thr, reasons in cands:
            if score >= thr and (best is None or score > best[2]):
                best = (state, direction, score, thr, reasons)
        if best is None:
            top = max(cands, key=lambda x: x[2])
            return PhaseVerdict("WAIT", None, score=top[2], threshold=top[3],
                                reasons=[f"no cluster >= threshold (best {top[0]}/{top[1]}={top[2]})"])
        return PhaseVerdict(best[0], best[1], score=best[2], threshold=best[3], reasons=best[4])

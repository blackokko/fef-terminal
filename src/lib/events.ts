// TypeScript event types matching backend Pydantic models in
// sidecar/src/fef_sidecar/events.py. Keep in sync — when backend
// adds a field, mirror it here.

export type FEFDirection = "LONG_CALLS" | "LONG_PUTS" | "WAIT";
export type FEFConfidence = "HIGH" | "MEDIUM" | "LOW" | "NONE";

export interface Bar {
  open: number;
  high: number;
  low: number;
  close: number;
  volume: number;
}

// [REMOVED 2026-08-25] WallRow — the {strike, net_m, side} row used by the
// top_gex / top_dex wall lists. The QuantData API and data subscription were
// CANCELLED; exposure-by-strike had no other source in this stack, so the
// walls, and the type describing them, are gone.

export interface OptionPricing {
  contract_strike: number;
  right: "C" | "P";
  entry_limit: number;
  delta: number | null;
  underlying_price_at_quote: number | null;
  t1: number;
  t2: number;
  t3: number;
  stop: number;
}

export interface FEFVerdict {
  direction: FEFDirection;
  confidence: FEFConfidence;
  reasoning: string;
  strike_rec: number | null;
  suggested_qty: number | null;
  t1: number | null;
  t2: number | null;
  t3: number | null;
  stop: number | null;
  delta_price: number | null;
  delta_drift_m: number | null;
  fe_ratio: number | null;
  aligned: boolean;
  absorption: boolean;
  wall_proximity_skip: boolean;
  wall_collapse_trigger: boolean;
  nearest_wall_strike: number | null;
  nearest_wall_distance: number | null;
  secondary_scores: Record<string, number>;
  option_pricing?: OptionPricing | null;
}

// [REMOVED 2026-08-25] IterSnapshot — every field on it (top_gex, top_dex,
// drift, ntm_max_pain, raw_max_pain, aggression) was QuantData-sourced, and
// that subscription was CANCELLED. IterEvent.snapshot went with it. The
// sidecar may still put a `snapshot` key on the wire; the frontend simply
// does not declare or read it any more.

export interface TickEvent {
  type: "tick";
  timestamp_ms: number;
  spx_price: number;
  bar?: Bar | null;
  bid?: number | null;
  ask?: number | null;
  bid_size?: number | null;
  ask_size?: number | null;
  symbol?: string | null;
  prior_close?: number | null;
}

export interface IterEvent {
  type: "iter";
  timestamp_ms: number;
  iter_num: number;
  session_date: string;
  spx_price: number;
  verdict: FEFVerdict;
}

// [REMOVED 2026-08-25] SnapshotEvent — the ~15s QuantData broadcast (walls,
// drift, max pain, aggression) that refreshed the sidebar ahead of the FEF
// iter cadence. QuantData subscription CANCELLED; the event carries nothing
// the frontend can still source, so the type, the "snapshot" case in
// WebSocketContext and `lastSnapshot` were all removed together.

export interface HFTEvent {
  type: "hft";
  timestamp_ms: number;
  short_vol_pts: number;
  long_vol_pts: number;
  vol_ratio: number;
  vol_regime: string;
  cancel_to_trade_ratio: number;
  quote_intensity_per_sec: number;
  stuffing_severity: string;
  book_imbalance: number;
  book_imbalance_5lvl: number;
  book_direction: string;
  book_confidence: string;
  weighted_mid_price: number | null;
  book_spread: number | null;
  composite_direction: string;
  fired_on: string;
  vol_source?: string;
  stuffing_source?: string;
  depth_source?: string;
}

export interface MicrostructureEvent {
  type: "microstructure";
  timestamp_ms: number;
  ofi_window: number;
  avi_pct: number;
  aggressive_buy_volume: number;
  aggressive_sell_volume: number;
  microprice: number | null;
  last_trade_price: number | null;
  microprice_deviation_bp: number | null;
  direction: string;
  confidence: string;
  reasoning: string;
  quote_count: number;
  trade_count: number;
}

export interface AccountEvent {
  type: "account";
  timestamp_ms: number;
  account: string;
  net_liquidation: number | null;
  buying_power: number | null;
  available_funds: number | null;
  daily_pnl: number | null;
  realized_pnl: number | null;
  unrealized_pnl: number | null;
  gross_position_value: number | null;
  open_positions_count: number;
  open_options_count: number;
  currency: string;
}

export interface RegimeEvent {
  type: "regime";
  timestamp_ms: number;
  regime: string;
  label: string;
  confidence: number;
  rationale: string;
  recommended_preset_hint: string;
  recommended_patterns: string[];
  bias: string;
  sizing: string;
}

export interface PatternMatchEvent {
  type: "pattern_match";
  timestamp_ms: number;
  pattern_id: number;
  pattern_name: string;
  pattern_description: string;
  signal_iter_id: number;
  iter_num: number;
  spx_price: number;
  audio_alert: boolean;
  toast_alert: boolean;
}

export interface TicketEvent {
  type: "ticket";
  timestamp_ms: number;
  ticket_id: number;
  signal_iter_id: number;
  direction: FEFDirection;
  contract_strike: number;
  contract_expiry: string;
  qty: number;
  entry_limit: number;
  t1: number;
  t2: number;
  t3: number;
  stop: number;
  status: string;
}

export interface StatusEvent {
  type: "status";
  timestamp_ms: number;
  // [REMOVED 2026-08-25] `quantdata: string` — provider status string
  // ("Connected" / "Mock ..."). QuantData subscription CANCELLED, so there is
  // no provider left to report on. The sidecar's /status may still emit the
  // key; nothing in the UI reads it.
  ibkr: string;
  engine: string;
  history_size: number;
}

export type SidecarEvent =
  | TickEvent
  | IterEvent
  | TicketEvent
  | StatusEvent
  | PatternMatchEvent
  | RegimeEvent
  | AccountEvent
  | MicrostructureEvent
  | HFTEvent;

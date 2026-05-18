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

export interface WallRow {
  strike: number;
  net_m: number;
  side: "CALL" | "PUT";
}

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

export interface IterSnapshot {
  top_gex: WallRow[];
  top_dex: WallRow[];
  drift: { cum_net_m: number; bias: string };
  ntm_max_pain: number;
  raw_max_pain: number;
  aggression: { call_pct: number; put_pct: number } | null;
}

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
}

export interface IterEvent {
  type: "iter";
  timestamp_ms: number;
  iter_num: number;
  session_date: string;
  spx_price: number;
  verdict: FEFVerdict;
  snapshot: IterSnapshot;
}

export interface SnapshotEvent {
  type: "snapshot";
  timestamp_ms: number;
  spx_price: number;
  top_gex: WallRow[];
  top_dex: WallRow[];
  drift: { cum_net_m: number; bias: string };
  ntm_max_pain: number;
  raw_max_pain: number;
  aggression: { call_pct: number; put_pct: number } | null;
}

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
  quantdata: string;
  ibkr: string;
  engine: string;
  history_size: number;
}

export type SidecarEvent =
  | TickEvent
  | IterEvent
  | TicketEvent
  | StatusEvent
  | SnapshotEvent
  | PatternMatchEvent
  | RegimeEvent
  | AccountEvent
  | MicrostructureEvent
  | HFTEvent;

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

export interface FEFVerdict {
  direction: FEFDirection;
  confidence: FEFConfidence;
  reasoning: string;
  strike_rec: number | null;
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

export type SidecarEvent = TickEvent | IterEvent | TicketEvent | StatusEvent;

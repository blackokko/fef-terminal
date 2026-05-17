import {
  createContext,
  useCallback,
  useContext,
  useEffect,
  useRef,
  useState,
  type ReactNode,
} from "react";
import type {
  AccountEvent,
  Bar,
  HFTEvent,
  IterEvent,
  MicrostructureEvent,
  PatternMatchEvent,
  RegimeEvent,
  SidecarEvent,
  SnapshotEvent,
  StatusEvent,
  TicketEvent,
  TickEvent,
} from "../lib/events";

const DEFAULT_WS_URL = "ws://127.0.0.1:8765/ws";

type ConnectionState = "connecting" | "connected" | "disconnected" | "error";

interface SidecarState {
  connection: ConnectionState;
  lastTick: TickEvent | null;
  lastIter: IterEvent | null;
  lastSnapshot: SnapshotEvent | null;
  lastStatus: StatusEvent | null;
  lastRegime: RegimeEvent | null;
  lastAccount: AccountEvent | null;
  lastMicrostructure: MicrostructureEvent | null;
  lastHFT: HFTEvent | null;
  activeSymbol: string | null;  // From /status — drives chart + engine
  recentTicks: TickEvent[];
  recentIters: IterEvent[];
  recentBars: Bar[];
  recentTickets: TicketEvent[];
  recentPatternMatches: PatternMatchEvent[];
}

const initialState: SidecarState = {
  connection: "connecting",
  lastTick: null,
  lastIter: null,
  lastSnapshot: null,
  lastStatus: null,
  lastRegime: null,
  lastAccount: null,
  lastMicrostructure: null,
  lastHFT: null,
  activeSymbol: null,
  recentTicks: [],
  recentIters: [],
  recentBars: [],
  recentTickets: [],
  recentPatternMatches: [],
};

const SidecarContext = createContext<SidecarState>(initialState);

const RECENT_LIMIT = 200;

export function SidecarProvider({
  children,
  url = DEFAULT_WS_URL,
}: {
  children: ReactNode;
  url?: string;
}) {
  const [state, setState] = useState<SidecarState>(initialState);
  const wsRef = useRef<WebSocket | null>(null);
  const reconnectTimerRef = useRef<number | null>(null);

  const handleEvent = useCallback((event: SidecarEvent) => {
    setState((prev) => {
      switch (event.type) {
        case "tick":
          return {
            ...prev,
            lastTick: event,
            recentTicks: [...prev.recentTicks, event].slice(-RECENT_LIMIT),
            recentBars: event.bar
              ? [...prev.recentBars, event.bar].slice(-RECENT_LIMIT)
              : prev.recentBars,
          };
        case "iter":
          return {
            ...prev,
            lastIter: event,
            recentIters: [...prev.recentIters, event].slice(-RECENT_LIMIT),
          };
        case "snapshot":
          return { ...prev, lastSnapshot: event };
        case "pattern_match":
          return {
            ...prev,
            recentPatternMatches: [...prev.recentPatternMatches, event].slice(-RECENT_LIMIT),
          };
        case "regime":
          return { ...prev, lastRegime: event };
        case "account":
          return { ...prev, lastAccount: event };
        case "microstructure":
          return { ...prev, lastMicrostructure: event };
        case "hft":
          return { ...prev, lastHFT: event };
        case "ticket":
          return {
            ...prev,
            recentTickets: [...prev.recentTickets, event].slice(-RECENT_LIMIT),
          };
        case "status":
          return { ...prev, lastStatus: event };
        default:
          return prev;  // unknown event type — keep state unchanged
      }
    });
  }, []);

  // Pre-fetch initial state on mount so the UI populates immediately
  // instead of waiting for WS events to trickle in.
  useEffect(() => {
    let cancelled = false;
    void (async () => {
      try {
        const [statusR, signalsR] = await Promise.all([
          fetch("http://127.0.0.1:8765/status"),
          fetch("http://127.0.0.1:8765/history/signals?limit=10"),
        ]);
        if (cancelled) return;
        if (statusR.ok) {
          const s = await statusR.json();
          // Synthesize a StatusEvent from /status payload
          setState((prev) => ({
            ...prev,
            activeSymbol: s.active_symbol ?? prev.activeSymbol,
            lastStatus: {
              type: "status",
              timestamp_ms: Date.now(),
              quantdata: s.quantdata,
              ibkr: s.ibkr,
              engine: s.engine_history_size > 0 ? "running" : "stopped",
              history_size: s.engine_history_size,
            },
          }));
        }
        if (signalsR.ok) {
          const signals = await signalsR.json();
          if (signals.length > 0) {
            // signals are descending — first is newest
            const newest = signals[0];
            // Build a synthetic IterEvent from the most recent SignalIter row
            setState((prev) => ({
              ...prev,
              lastIter: {
                type: "iter",
                timestamp_ms: newest.timestamp ? new Date(newest.timestamp).getTime() : Date.now(),
                iter_num: newest.iter_num,
                session_date: newest.session_date,
                spx_price: newest.spx_price,
                verdict: {
                  direction: newest.direction,
                  confidence: newest.confidence,
                  reasoning: newest.reasoning,
                  ...(newest.extras || {}),
                  suggested_qty: newest.extras?.suggested_qty ?? null,
                  strike_rec: newest.extras?.strike_rec ?? null,
                  t1: newest.extras?.t1 ?? null,
                  t2: newest.extras?.t2 ?? null,
                  t3: newest.extras?.t3 ?? null,
                  stop: newest.extras?.stop ?? null,
                  delta_price: newest.extras?.delta_price ?? null,
                  delta_drift_m: newest.extras?.delta_drift_m ?? null,
                  fe_ratio: newest.extras?.fe_ratio ?? null,
                  aligned: newest.extras?.aligned ?? false,
                  absorption: newest.extras?.absorption ?? false,
                  wall_proximity_skip: newest.extras?.wall_proximity_skip ?? false,
                  wall_collapse_trigger: newest.extras?.wall_collapse_trigger ?? false,
                  nearest_wall_strike: newest.extras?.nearest_wall_strike ?? null,
                  nearest_wall_distance: newest.extras?.nearest_wall_distance ?? null,
                  secondary_scores: newest.extras?.secondary_scores ?? {},
                  option_pricing: newest.extras?.option_pricing ?? null,
                },
                snapshot: {
                  top_gex: newest.top_gex_walls || [],
                  top_dex: newest.top_dex_walls || [],
                  drift: { cum_net_m: newest.drift_cum_net_m, bias: newest.drift_bias },
                  ntm_max_pain: newest.ntm_max_pain,
                  raw_max_pain: newest.raw_max_pain,
                  aggression: newest.aggression_call_pct != null ? {
                    call_pct: newest.aggression_call_pct,
                    put_pct: newest.aggression_put_pct,
                  } : null,
                },
              },
              recentIters: signals.reverse().map((r: any) => ({
                type: "iter",
                timestamp_ms: r.timestamp ? new Date(r.timestamp).getTime() : Date.now(),
                iter_num: r.iter_num,
                session_date: r.session_date,
                spx_price: r.spx_price,
                verdict: { direction: r.direction, confidence: r.confidence, reasoning: r.reasoning },
                snapshot: {
                  top_gex: r.top_gex_walls || [],
                  top_dex: r.top_dex_walls || [],
                  drift: { cum_net_m: r.drift_cum_net_m, bias: r.drift_bias },
                  ntm_max_pain: r.ntm_max_pain,
                  raw_max_pain: r.raw_max_pain,
                  aggression: null,
                },
              })),
            }));
          }
        }
      } catch (e) {
        // Sidecar not up yet — WS connect will retry
      }
    })();
    return () => {
      cancelled = true;
    };
  }, []);

  useEffect(() => {
    let cancelled = false;

    const connect = () => {
      if (cancelled) return;
      setState((p) => ({ ...p, connection: "connecting" }));

      const ws = new WebSocket(url);
      wsRef.current = ws;

      ws.onopen = () => {
        if (cancelled) return;
        setState((p) => ({ ...p, connection: "connected" }));
      };

      ws.onmessage = (msg) => {
        try {
          const parsed = JSON.parse(msg.data) as SidecarEvent;
          handleEvent(parsed);
        } catch (err) {
          console.error("Failed to parse WS message:", err, msg.data);
        }
      };

      ws.onerror = (err) => {
        console.error("WS error:", err);
        setState((p) => ({ ...p, connection: "error" }));
      };

      ws.onclose = () => {
        if (cancelled) return;
        setState((p) => ({ ...p, connection: "disconnected" }));
        reconnectTimerRef.current = window.setTimeout(connect, 2000);
      };
    };

    connect();

    return () => {
      cancelled = true;
      if (reconnectTimerRef.current !== null) {
        window.clearTimeout(reconnectTimerRef.current);
      }
      if (wsRef.current && wsRef.current.readyState === WebSocket.OPEN) {
        wsRef.current.close();
      }
    };
  }, [url, handleEvent]);

  return <SidecarContext.Provider value={state}>{children}</SidecarContext.Provider>;
}

export function useSidecar(): SidecarState {
  return useContext(SidecarContext);
}

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
  Bar,
  IterEvent,
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
  recentTicks: TickEvent[];
  recentIters: IterEvent[];
  recentBars: Bar[];
  recentTickets: TicketEvent[];
}

const initialState: SidecarState = {
  connection: "connecting",
  lastTick: null,
  lastIter: null,
  lastSnapshot: null,
  lastStatus: null,
  recentTicks: [],
  recentIters: [],
  recentBars: [],
  recentTickets: [],
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
        case "ticket":
          return {
            ...prev,
            recentTickets: [...prev.recentTickets, event].slice(-RECENT_LIMIT),
          };
        case "status":
          return { ...prev, lastStatus: event };
      }
    });
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

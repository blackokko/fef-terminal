import { useEffect, useRef, useState } from "react";
import { useSidecar } from "../context/WebSocketContext";
import { popoutChart } from "../lib/popoutChart";
import { SymbolSelector } from "./SymbolSelector";

const connectionColor: Record<string, string> = {
  connecting: "var(--color-amber)",
  connected: "var(--color-green)",
  disconnected: "var(--color-red)",
  error: "var(--color-red)",
};

const stateColor: Record<string, string> = {
  GREEN: "var(--color-green)",
  AMBER: "var(--color-amber)",
  RED: "var(--color-red)",
  NO_DATA: "var(--color-text-muted)",
};

function ModelStatePill() {
  const [state, setState] = useState<string>("NO_DATA");
  const [expectancy, setExpectancy] = useState<number | null>(null);

  useEffect(() => {
    const refresh = async () => {
      try {
        const r = await fetch("http://127.0.0.1:8765/tuning/recommendations?status=OPEN");
        if (!r.ok) return;
        const recs = await r.json();
        const sm = recs.find((x: any) => x.kind === "STATE_OF_MODEL");
        if (sm) {
          setState(sm.backing_data?.state ?? "NO_DATA");
          setExpectancy(sm.backing_data?.expectancy_avg_r ?? null);
        }
      } catch {}
    };
    void refresh();
    const id = window.setInterval(refresh, 60000);
    return () => window.clearInterval(id);
  }, []);

  return (
    <span title={`Model state — last 5 sessions avg expectancy ${expectancy != null ? expectancy.toFixed(2) + "R" : "—"}`}
          style={{
            display: "inline-block", padding: "2px 8px", marginLeft: 6,
            borderRadius: 3, fontSize: 10, fontWeight: 700,
            fontFamily: "var(--font-mono)",
            background: stateColor[state],
            color: state === "AMBER" ? "var(--color-bg)" : "white",
          }}>
      MDL {state}
    </span>
  );
}

export function TopBar() {
  const { connection, lastTick, lastIter, lastStatus, lastSnapshot, lastAccount } = useSidecar();
  const spx = lastTick?.spx_price ?? lastIter?.spx_price;

  // IMPROVEMENTS #I: price flash on tick — micro-flash green/red on direction
  const prevSpxRef = useRef<number | undefined>(spx);
  const [flash, setFlash] = useState<"up" | "down" | null>(null);
  useEffect(() => {
    if (spx == null || prevSpxRef.current == null) {
      prevSpxRef.current = spx;
      return;
    }
    if (spx > prevSpxRef.current) {
      setFlash("up");
    } else if (spx < prevSpxRef.current) {
      setFlash("down");
    }
    prevSpxRef.current = spx;
    const t = window.setTimeout(() => setFlash(null), 300);
    return () => window.clearTimeout(t);
  }, [spx]);

  // IMPROVEMENTS #N: stale-data audio chime if QuantData snapshot stalls >60s
  const staleAlertedRef = useRef(false);
  useEffect(() => {
    if (!lastSnapshot) return;
    const interval = window.setInterval(() => {
      const age = Date.now() - lastSnapshot.timestamp_ms;
      if (age > 60_000 && !staleAlertedRef.current) {
        staleAlertedRef.current = true;
        void import("../lib/audioAlert").then((m) =>
          m.playTones?.([392, 311.13], 0.25, 0.18),
        );  // descending warning tone
      } else if (age < 30_000) {
        staleAlertedRef.current = false;
      }
    }, 5000);
    return () => window.clearInterval(interval);
  }, [lastSnapshot]);

  return (
    <header className="topbar">
      <SymbolSelector />
      <div className="topbar-section">
        <span className="topbar-label">PRICE</span>
        <span className={`topbar-price ${flash ? `flash-${flash}` : ""}`}>
          {spx != null ? spx.toFixed(2) : "—"}
        </span>
      </div>

      <div className="topbar-section">
        <span className="topbar-label">VIX</span>
        <span className="topbar-value">
          {(lastStatus as any)?.vix != null ? (lastStatus as any).vix.toFixed(2) : "—"}
        </span>
      </div>

      <div className="topbar-section">
        <span className="topbar-label">Today P/L</span>
        <span className="topbar-value" style={{
          color: lastAccount?.daily_pnl == null ? undefined
            : lastAccount.daily_pnl >= 0 ? "var(--color-green)" : "var(--color-red)",
          fontWeight: 600,
        }}>
          {lastAccount?.daily_pnl != null
            ? `${lastAccount.daily_pnl >= 0 ? "+" : "-"}$${Math.abs(lastAccount.daily_pnl).toFixed(0)}`
            : "—"}
        </span>
      </div>

      <div className="topbar-section">
        <span className="topbar-label">Last iter</span>
        <span className="topbar-value">
          {lastIter
            ? `#${lastIter.iter_num} ${lastIter.verdict.direction}`
            : "—"}
        </span>
        <ModelStatePill />
      </div>

      <div className="topbar-section">
        <span className="topbar-label">QuantData</span>
        <span className="topbar-value">{lastStatus?.quantdata ?? "—"}</span>
      </div>

      <div className="topbar-section">
        <span className="topbar-label">IBKR</span>
        <span className="topbar-value">{lastStatus?.ibkr ?? "—"}</span>
      </div>

      <div className="topbar-section topbar-section-right">
        <button
          className="topbar-button"
          style={{ background: "var(--color-red)", color: "white", fontWeight: 700 }}
          onClick={async () => {
            if (!window.confirm("FLAT EVERYTHING?\n\n- Cancel all working orders\n- Disable auto-trade\n- Market-sell all open option positions\n- Disable scanner symbols\n\nIrreversible. Continue?")) return;
            try {
              const r = await fetch("http://127.0.0.1:8765/panic/flat", { method: "POST" });
              const data = await r.json();
              const sold = (data?.steps?.positions_flat ?? []).length;
              const cancelled = data?.steps?.cancel_orders?.cancelled ?? 0;
              window.alert(`FLAT done.\nOrders cancelled: ${cancelled}\nPositions sold: ${sold}\nAuto-trade & scanner disabled.`);
            } catch (e) {
              window.alert(`FLAT failed: ${(e as Error).message}`);
            }
          }}
          title="Cancel all orders + disable auto-trade + market-sell all positions + disable scanner"
        >
          🚨 FLAT
        </button>
        <button
          className="topbar-button"
          onClick={() => void popoutChart()}
          title="Open chart in standalone window (for second monitor)"
        >
          ⧉ Pop chart
        </button>
        {lastStatus?.quantdata?.startsWith("Mock") && lastStatus?.ibkr === "connected" && (
          <span className="all-mock-banner" title="Both providers are mock — signals are replay only">
            ⚠ ALL MOCK
          </span>
        )}
        <span
          className="connection-dot"
          style={{ background: connectionColor[connection] }}
          title={`WebSocket: ${connection}`}
        />
        <span className="topbar-label">{connection}</span>
      </div>
    </header>
  );
}

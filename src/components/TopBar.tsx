import { useEffect, useRef, useState } from "react";
import { useSidecar } from "../context/WebSocketContext";
import { popoutChart } from "../lib/popoutChart";

const connectionColor: Record<string, string> = {
  connecting: "var(--color-amber)",
  connected: "var(--color-green)",
  disconnected: "var(--color-red)",
  error: "var(--color-red)",
};

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
      <div className="topbar-section">
        <span className="topbar-label">SPX</span>
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

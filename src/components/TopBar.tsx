import { useSidecar } from "../context/WebSocketContext";

const connectionColor: Record<string, string> = {
  connecting: "var(--color-amber)",
  connected: "var(--color-green)",
  disconnected: "var(--color-red)",
  error: "var(--color-red)",
};

export function TopBar() {
  const { connection, lastTick, lastIter, lastStatus } = useSidecar();

  const spx = lastTick?.spx_price ?? lastIter?.spx_price;

  return (
    <header className="topbar">
      <div className="topbar-section">
        <span className="topbar-label">SPX</span>
        <span className="topbar-price">
          {spx != null ? spx.toFixed(2) : "—"}
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
        {lastStatus?.quantdata?.startsWith("Mock") && lastStatus?.ibkr === "connected" && (
          <span className="all-mock-banner" title="Both providers are mock — signals are replay only, not live data">
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

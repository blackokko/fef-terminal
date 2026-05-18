import { useEffect, useRef, useState } from "react";
import { useSidecar } from "../context/WebSocketContext";

// Live quote panel reads from the SHARED WS tick stream (lastTick) — the
// same source the chart's price updates come from. This guarantees zero
// desync between the panel's bid/ask and the chart's candle close.

export function LiveQuotePanel() {
  const { lastTick, activeSymbol, lastStatus } = useSidecar();
  const prevLastRef = useRef<number | null>(null);
  const [flash, setFlash] = useState<"up" | "down" | null>(null);

  useEffect(() => {
    if (!lastTick) return;
    const last = lastTick.spx_price;
    if (prevLastRef.current != null) {
      if (last > prevLastRef.current) setFlash("up");
      else if (last < prevLastRef.current) setFlash("down");
    }
    prevLastRef.current = last;
  }, [lastTick]);

  useEffect(() => {
    if (!flash) return;
    const t = window.setTimeout(() => setFlash(null), 400);
    return () => window.clearTimeout(t);
  }, [flash]);

  const symbol = lastTick?.symbol ?? activeSymbol ?? "SPX";
  const isLive = (lastStatus as any)?.ibkr === "connected"
    && lastStatus && !(/Mock/i.test(lastStatus.ibkr ?? ""));

  if (!lastTick) {
    return (
      <section className="sidebar-section">
        <h3 className="sidebar-section-title">Live quote · {symbol}</h3>
        <p className="muted small">waiting for first tick…</p>
      </section>
    );
  }

  const last = lastTick.spx_price;
  const bid = lastTick.bid ?? null;
  const ask = lastTick.ask ?? null;
  const spread = (bid != null && ask != null) ? (ask - bid) : null;
  const spreadPct = (spread != null && last) ? (spread / last) * 100 : null;
  // 0DTE liquidity benchmark — <0.1% tight, <0.5% acceptable, >1% wide
  const spreadColor = spreadPct == null ? "var(--color-text-muted)"
    : spreadPct < 0.1 ? "var(--color-green)"
    : spreadPct < 0.5 ? "var(--color-amber)"
    : "var(--color-red)";

  return (
    <section className="sidebar-section live-quote-panel">
      <h3 className="sidebar-section-title">
        Live quote · {symbol}
        <span className="live-quote-source"
              style={{ color: isLive ? "var(--color-green)" : "var(--color-amber)" }}>
          {isLive ? " ● LIVE" : " ◌ MOCK"}
        </span>
      </h3>

      <div className="live-quote-hero">
        <div className={`live-quote-last ${flash ? `flash-${flash}` : ""}`}>
          {last != null ? last.toFixed(2) : "—"}
        </div>
        <div className="muted small">updated {new Date(lastTick.timestamp_ms).toLocaleTimeString()}</div>
      </div>

      <div className="live-quote-book">
        <div className="live-quote-side live-quote-bid">
          <div className="muted small">BID</div>
          <div className="live-quote-price">{bid?.toFixed(2) ?? "—"}</div>
          <div className="muted small">×{lastTick.bid_size?.toFixed(0) ?? "—"}</div>
        </div>
        <div className="live-quote-spread" style={{ color: spreadColor }}>
          {spread != null ? (
            <>
              <div className="live-quote-spread-dollars">${spread.toFixed(2)}</div>
              <div className="muted small">{spreadPct!.toFixed(3)}%</div>
            </>
          ) : (
            <div className="muted small">no bid/ask</div>
          )}
        </div>
        <div className="live-quote-side live-quote-ask">
          <div className="muted small">ASK</div>
          <div className="live-quote-price">{ask?.toFixed(2) ?? "—"}</div>
          <div className="muted small">×{lastTick.ask_size?.toFixed(0) ?? "—"}</div>
        </div>
      </div>
    </section>
  );
}

import { useEffect, useRef, useState } from "react";
import { useSidecar } from "../context/WebSocketContext";

const API_BASE = "http://127.0.0.1:8765";
const POLL_MS = 1500;  // ~700ms latency floor from IBKR, poll every 1.5s

interface Quote {
  symbol: string;
  exchange: string;
  kind: string;
  last: number | null;
  bid: number | null;
  ask: number | null;
  bid_size: number | null;
  ask_size: number | null;
  spread: number | null;
  spread_pct: number | null;
  timestamp_ms: number;
  source: "live" | "mock";
  provider: string;
}

export function LiveQuotePanel() {
  const { activeSymbol } = useSidecar();
  const symbol = activeSymbol ?? "SPX";
  const [quote, setQuote] = useState<Quote | null>(null);
  const [error, setError] = useState<string | null>(null);
  const prevLastRef = useRef<number | null>(null);
  const [flash, setFlash] = useState<"up" | "down" | null>(null);

  useEffect(() => {
    let cancelled = false;
    const fetchQuote = async () => {
      try {
        const r = await fetch(`${API_BASE}/quote/${encodeURIComponent(symbol)}`);
        if (cancelled) return;
        if (!r.ok) {
          setError(`HTTP ${r.status}`);
          return;
        }
        const q: Quote = await r.json();
        setError(null);
        // Detect tick direction for flash
        if (prevLastRef.current != null && q.last != null) {
          if (q.last > prevLastRef.current) setFlash("up");
          else if (q.last < prevLastRef.current) setFlash("down");
          else setFlash(null);
        }
        prevLastRef.current = q.last;
        setQuote(q);
      } catch (e) {
        setError((e as Error).message);
      }
    };
    void fetchQuote();
    const id = window.setInterval(fetchQuote, POLL_MS);
    return () => { cancelled = true; window.clearInterval(id); };
  }, [symbol]);

  useEffect(() => {
    if (!flash) return;
    const t = window.setTimeout(() => setFlash(null), 400);
    return () => window.clearTimeout(t);
  }, [flash]);

  if (!quote) {
    return (
      <section className="sidebar-section">
        <h3 className="sidebar-section-title">Live quote · {symbol}</h3>
        <p className="muted small">{error ?? "loading..."}</p>
      </section>
    );
  }

  const isLive = quote.source === "live";
  const spreadPctNum = quote.spread_pct ?? 0;
  // 0DTE liquidity benchmark — <0.1% tight, <0.5% acceptable, >1% wide
  const spreadColor = spreadPctNum < 0.1 ? "var(--color-green)"
    : spreadPctNum < 0.5 ? "var(--color-amber)"
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
          {quote.last != null ? quote.last.toFixed(2) : "—"}
        </div>
        <div className="muted small">{quote.exchange} · {quote.kind}</div>
      </div>

      <div className="live-quote-book">
        <div className="live-quote-side live-quote-bid">
          <div className="muted small">BID</div>
          <div className="live-quote-price">{quote.bid?.toFixed(2) ?? "—"}</div>
          <div className="muted small">×{quote.bid_size?.toFixed(0) ?? "—"}</div>
        </div>
        <div className="live-quote-spread" style={{ color: spreadColor }}>
          {quote.spread != null ? (
            <>
              <div className="live-quote-spread-dollars">${quote.spread.toFixed(2)}</div>
              <div className="muted small">{spreadPctNum.toFixed(3)}%</div>
            </>
          ) : (
            <div className="muted">no quote</div>
          )}
        </div>
        <div className="live-quote-side live-quote-ask">
          <div className="muted small">ASK</div>
          <div className="live-quote-price">{quote.ask?.toFixed(2) ?? "—"}</div>
          <div className="muted small">×{quote.ask_size?.toFixed(0) ?? "—"}</div>
        </div>
      </div>

      <div className="muted small" style={{ marginTop: 6 }}>
        {quote.provider} · updated {new Date(quote.timestamp_ms).toLocaleTimeString()}
      </div>
    </section>
  );
}

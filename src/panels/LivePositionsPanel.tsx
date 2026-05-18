import { useEffect, useState } from "react";

const API_BASE = "http://127.0.0.1:8765";

interface LivePosition {
  id: number | null;
  snapshot_ts: string;
  symbol: string;
  strike: number;
  right: string;
  expiry: string;
  qty: number;
  avg_cost: number | null;
  market_price: number | null;
  unrealized_pnl: number | null;
  delta: number | null;
  gamma: number | null;
  theta: number | null;
  vega: number | null;
  iv: number | null;
}

function fmt(v: number | null | undefined, digits: number): string {
  if (v == null || !Number.isFinite(v)) return "—";
  return v.toFixed(digits);
}

export function LivePositionsPanel() {
  const [positions, setPositions] = useState<LivePosition[]>([]);

  useEffect(() => {
    const refresh = async () => {
      try {
        const r = await fetch(`${API_BASE}/live/positions?limit=20`);
        if (r.ok) setPositions(await r.json());
      } catch {}
    };
    void refresh();
    const id = window.setInterval(refresh, 5000);
    return () => window.clearInterval(id);
  }, []);

  // Take the most recent snapshot per (symbol/strike/right/expiry) since
  // they're appended over time
  const latest = new Map<string, LivePosition>();
  for (const p of positions) {
    const k = `${p.symbol}|${p.strike}|${p.right}|${p.expiry}`;
    if (!latest.has(k)) latest.set(k, p);
  }
  const rows = Array.from(latest.values());

  // Portfolio greeks total
  const portfolioDelta = rows.reduce((sum, p) => sum + (p.delta ?? 0) * p.qty, 0);
  const portfolioGamma = rows.reduce((sum, p) => sum + (p.gamma ?? 0) * p.qty, 0);
  const portfolioTheta = rows.reduce((sum, p) => sum + (p.theta ?? 0) * p.qty, 0);
  const totalUnrealized = rows.reduce((sum, p) => sum + (p.unrealized_pnl ?? 0), 0);

  return (
    <section className="sidebar-section">
      <h3 className="sidebar-section-title">Live positions · greeks</h3>
      {rows.length === 0 ? (
        <p className="muted small">no open positions (need live IBKR + open trades)</p>
      ) : (
        <>
          <div className="portfolio-greeks">
            <div>
              <div className="muted small">Unrealized</div>
              <div style={{ color: totalUnrealized >= 0 ? "var(--color-green)" : "var(--color-red)", fontWeight: 700, fontFamily: "var(--font-mono)" }}>
                {totalUnrealized >= 0 ? "+" : ""}${totalUnrealized.toFixed(0)}
              </div>
            </div>
            <div><div className="muted small">Δ</div><div className="portfolio-greek">{portfolioDelta.toFixed(2)}</div></div>
            <div><div className="muted small">Γ</div><div className="portfolio-greek">{portfolioGamma.toFixed(3)}</div></div>
            <div><div className="muted small">Θ</div><div className="portfolio-greek">{portfolioTheta.toFixed(0)}</div></div>
          </div>
          <table className="positions-table">
            <thead><tr><th>Pos</th><th>Cost</th><th>Mkt</th><th>P/L</th><th>Δ</th><th>IV</th></tr></thead>
            <tbody>
              {rows.map((p, i) => (
                <tr key={p.id ?? `${p.symbol}-${p.strike}-${p.right}-${i}`}>
                  <td>{p.qty}× {p.symbol} {p.strike}{p.right}</td>
                  <td className="muted">${fmt(p.avg_cost, 2)}</td>
                  <td>${fmt(p.market_price, 2)}</td>
                  <td style={{ color: (p.unrealized_pnl ?? 0) >= 0 ? "var(--color-green)" : "var(--color-red)" }}>
                    {p.unrealized_pnl == null ? "—" : (p.unrealized_pnl >= 0 ? "+" : "") + "$" + fmt(p.unrealized_pnl, 0)}
                  </td>
                  <td>{fmt(p.delta, 2)}</td>
                  <td className="muted">{p.iv != null ? (p.iv * 100).toFixed(1) + "%" : "—"}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </>
      )}
    </section>
  );
}

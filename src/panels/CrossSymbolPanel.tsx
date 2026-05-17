import { useEffect, useState } from "react";

const API_BASE = "http://127.0.0.1:8765";

interface CrossSymbolView {
  symbols_seen: string[];
  alignment_score: number;
  composite_direction: string;
  composite_confidence: string;
  per_symbol: Record<string, { direction: string; confidence: string; age_seconds: number; spx_price: number }>;
}

export function CrossSymbolPanel() {
  const [data, setData] = useState<CrossSymbolView | null>(null);

  useEffect(() => {
    let cancelled = false;
    const fetchView = async () => {
      try {
        const r = await fetch(`${API_BASE}/cross_symbol?window_seconds=120`);
        if (!r.ok || cancelled) return;
        setData(await r.json());
      } catch {
        // ignore
      }
    };
    void fetchView();
    const id = window.setInterval(fetchView, 5000);
    return () => { cancelled = true; window.clearInterval(id); };
  }, []);

  if (!data || data.symbols_seen.length === 0) {
    return (
      <section className="sidebar-section">
        <h3 className="sidebar-section-title">Cross-symbol alignment</h3>
        <p className="muted small">need ≥2 active symbols for cross-symbol view</p>
      </section>
    );
  }

  const a = data.alignment_score;
  const color = a > 0.5 ? "var(--color-green)"
    : a < -0.5 ? "var(--color-red)"
    : "var(--color-amber)";

  return (
    <section className="sidebar-section">
      <h3 className="sidebar-section-title">Cross-symbol · 120s</h3>
      <div className="cross-symbol-hero" style={{ borderLeft: `3px solid ${color}` }}>
        <div style={{ color, fontWeight: 700, fontFamily: "var(--font-mono)" }}>
          {data.composite_direction} · alignment {a.toFixed(2)}
        </div>
        <div className="muted small">{data.composite_confidence} confidence · {data.symbols_seen.length} symbols</div>
      </div>
      <table className="cross-symbol-table">
        <thead><tr><th>Sym</th><th>Dir</th><th>Conf</th><th>Age</th></tr></thead>
        <tbody>
          {Object.entries(data.per_symbol).map(([sym, v]) => (
            <tr key={sym}>
              <td>{sym}</td>
              <td style={{ color: v.direction === "LONG_CALLS" ? "var(--color-green)" : v.direction === "LONG_PUTS" ? "var(--color-red)" : undefined }}>
                {v.direction}
              </td>
              <td>{v.confidence}</td>
              <td className="muted">{v.age_seconds.toFixed(0)}s</td>
            </tr>
          ))}
        </tbody>
      </table>
    </section>
  );
}

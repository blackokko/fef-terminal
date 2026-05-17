import { useEffect, useState } from "react";

const API_BASE = "http://127.0.0.1:8765";

interface BucketStat {
  bucket: string;
  trades: number;
  wins: number;
  losses: number;
  total_pnl: number;
  avg_pnl: number;
  win_rate: number;
}

interface Attribution {
  by_direction: BucketStat[];
  by_confidence: BucketStat[];
  by_hour: BucketStat[];
}

export function AttributionPanel() {
  const [data, setData] = useState<Attribution | null>(null);

  useEffect(() => {
    let cancelled = false;
    void (async () => {
      try {
        const r = await fetch(`${API_BASE}/analytics/attribution`);
        if (!r.ok || cancelled) return;
        setData(await r.json());
      } catch {}
    })();
    return () => { cancelled = true; };
  }, []);

  if (!data) return (
    <section className="sidebar-section">
      <h3 className="sidebar-section-title">P/L Attribution</h3>
      <p className="muted small">loading...</p>
    </section>
  );

  const allEmpty = !data.by_direction.length && !data.by_confidence.length && !data.by_hour.length;
  if (allEmpty) {
    return (
      <section className="sidebar-section">
        <h3 className="sidebar-section-title">P/L Attribution</h3>
        <p className="muted small">no closed trades yet — close some tickets to see attribution</p>
      </section>
    );
  }

  return (
    <section className="sidebar-section">
      <h3 className="sidebar-section-title">P/L Attribution</h3>
      {[
        { title: "By direction", data: data.by_direction },
        { title: "By confidence", data: data.by_confidence },
        { title: "By hour (ET)", data: data.by_hour },
      ].map(({ title, data: rows }) =>
        rows.length > 0 ? (
          <div key={title} className="attribution-section">
            <div className="muted small" style={{ marginTop: 8, marginBottom: 4 }}>{title}</div>
            <table className="attribution-table">
              <thead><tr><th></th><th>Trades</th><th>WR</th><th>Total $</th><th>Avg $</th></tr></thead>
              <tbody>
                {rows.map(r => (
                  <tr key={r.bucket}>
                    <td><strong>{r.bucket}</strong></td>
                    <td className="muted">{r.trades}</td>
                    <td>{(r.win_rate * 100).toFixed(0)}%</td>
                    <td style={{ color: r.total_pnl >= 0 ? "var(--color-green)" : "var(--color-red)", fontWeight: 600 }}>
                      {r.total_pnl >= 0 ? "+" : ""}${r.total_pnl.toFixed(0)}
                    </td>
                    <td className="muted">${r.avg_pnl.toFixed(0)}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        ) : null
      )}
    </section>
  );
}

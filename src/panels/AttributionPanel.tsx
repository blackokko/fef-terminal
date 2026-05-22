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

function todayET(): string {
  const d = new Date();
  // ET is UTC-4 in summer; close enough for default
  const et = new Date(d.getTime() - 4 * 60 * 60 * 1000);
  return et.toISOString().slice(0, 10);
}

export function AttributionPanel() {
  const [data, setData] = useState<Attribution | null>(null);
  const [sessionDate, setSessionDate] = useState<string>(todayET());
  const [allTime, setAllTime] = useState(false);

  useEffect(() => {
    let cancelled = false;
    void (async () => {
      const url = allTime
        ? `${API_BASE}/analytics/attribution`
        : `${API_BASE}/analytics/attribution?session_date=${sessionDate}`;
      try {
        const r = await fetch(url);
        if (!r.ok || cancelled) return;
        setData(await r.json());
      } catch {}
    })();
    return () => { cancelled = true; };
  }, [sessionDate, allTime]);

  const allEmpty = !data || (!data.by_direction.length && !data.by_confidence.length && !data.by_hour.length);

  // Total P/L = sum of by_direction (every closed trade has exactly one direction bucket)
  const totalPnl = data?.by_direction.reduce((s, b) => s + b.total_pnl, 0) ?? 0;
  const totalTrades = data?.by_direction.reduce((s, b) => s + b.trades, 0) ?? 0;
  const totalWins = data?.by_direction.reduce((s, b) => s + b.wins, 0) ?? 0;
  const winRate = totalTrades > 0 ? (totalWins / totalTrades * 100) : 0;

  return (
    <section className="sidebar-section">
      <h3 className="sidebar-section-title">P/L Attribution</h3>

      <div style={{ display: "flex", gap: 6, marginBottom: 8, alignItems: "center" }}>
        <input
          type="date"
          value={sessionDate}
          onChange={e => { setSessionDate(e.target.value); setAllTime(false); }}
          disabled={allTime}
          style={{ flex: 1, padding: 4 }}
        />
        <label style={{ display: "flex", alignItems: "center", gap: 4 }}>
          <input type="checkbox" checked={allTime} onChange={e => setAllTime(e.target.checked)} />
          <span className="muted small">All time</span>
        </label>
      </div>

      {!data ? (
        <p className="muted small">loading...</p>
      ) : allEmpty ? (
        <p className="muted small">no closed trades for {allTime ? "any session" : sessionDate}</p>
      ) : (
        <>
          {/* Total row */}
          <div style={{
            display: "flex", justifyContent: "space-between",
            padding: "6px 8px", marginBottom: 8,
            background: "rgba(40,80,120,0.10)", borderRadius: 4,
            fontFamily: "var(--font-mono)", fontSize: 12,
          }}>
            <span><strong>Total</strong> ({totalTrades} trades, {winRate.toFixed(0)}% WR)</span>
            <strong style={{
              color: totalPnl >= 0 ? "var(--color-green)" : "var(--color-red)",
              fontSize: 14,
            }}>
              {totalPnl >= 0 ? "+" : ""}${totalPnl.toFixed(0)}
            </strong>
          </div>

          {[
            { title: "By direction", data: data.by_direction },
            { title: "By confidence", data: data.by_confidence },
            { title: "By hour (CT)", data: data.by_hour },
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
        </>
      )}
    </section>
  );
}

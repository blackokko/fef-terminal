import { useEffect, useState } from "react";

const API_BASE = "http://127.0.0.1:8765";

interface WfRun {
  id: number;
  run_at: string;
  window_days: number;
  drift_score: number;
  current: { wall_proximity_pts: number; wall_collapse_pct: number; wall_collapse_floor_m: number; absorption_fe_ratio_min: number };
  optimal: { wall_proximity_pts: number | null; wall_collapse_pct: number | null; wall_collapse_floor_m: number | null; absorption_fe_ratio_min: number | null; sharpe: number | null; total_pnl: number | null; win_rate_pct: number | null };
  notes: string;
}

export function WalkForwardPanel() {
  const [runs, setRuns] = useState<WfRun[]>([]);
  const [running, setRunning] = useState(false);

  const refresh = async () => {
    try {
      const r = await fetch(`${API_BASE}/walk_forward/runs?limit=10`);
      if (r.ok) setRuns(await r.json());
    } catch {}
  };

  useEffect(() => { void refresh(); }, []);

  const handleRun = async () => {
    setRunning(true);
    try {
      await fetch(`${API_BASE}/walk_forward/run_now`, { method: "POST" });
      await refresh();
    } catch {}
    setRunning(false);
  };

  const latest = runs[0];

  return (
    <section className="sidebar-section">
      <h3 className="sidebar-section-title">Auto walk-forward (nightly)</h3>

      <button onClick={() => void handleRun()} disabled={running} style={{ marginBottom: 8 }}>
        {running ? "Running optimization (may take 30s)..." : "Run now"}
      </button>

      {!latest ? (
        <p className="muted small">no runs yet — click 'Run now' or wait for nightly 22:00 UTC</p>
      ) : (
        <>
          <div className="walk-forward-hero">
            <div>
              <div className="muted small">Drift score</div>
              <div style={{
                fontSize: 20, fontWeight: 700, fontFamily: "var(--font-mono)",
                color: latest.drift_score > 0.5 ? "var(--color-red)" : latest.drift_score > 0.2 ? "var(--color-amber)" : "var(--color-green)",
              }}>
                {latest.drift_score.toFixed(2)}
              </div>
            </div>
            <div>
              <div className="muted small">Optimal Sharpe</div>
              <div style={{ fontSize: 16, fontWeight: 600 }}>{latest.optimal.sharpe?.toFixed(2) ?? "—"}</div>
            </div>
            <div>
              <div className="muted small">Optimal P/L</div>
              <div style={{ color: (latest.optimal.total_pnl ?? 0) >= 0 ? "var(--color-green)" : "var(--color-red)", fontWeight: 600 }}>
                ${latest.optimal.total_pnl?.toFixed(0) ?? "—"}
              </div>
            </div>
          </div>

          <table className="wf-table">
            <thead><tr><th>Param</th><th>Current</th><th>Optimal</th></tr></thead>
            <tbody>
              {([
                ["wall_proximity_pts", "wall_proximity_pts"],
                ["wall_collapse_pct", "wall_collapse_pct"],
                ["wall_collapse_floor_m", "wall_collapse_floor_m"],
                ["absorption_fe_ratio_min", "absorption_fe_ratio_min"],
              ] as Array<[keyof typeof latest.current, keyof typeof latest.optimal]>).map(([k, ok]) => {
                const c = (latest.current as any)[k];
                const o = (latest.optimal as any)[ok];
                const drift = o != null && Math.abs(o - c) / Math.max(c, 0.01) > 0.2;
                return (
                  <tr key={k as string}>
                    <td>{k as string}</td>
                    <td className="muted">{c}</td>
                    <td style={{ color: drift ? "var(--color-amber)" : undefined, fontWeight: drift ? 600 : 400 }}>
                      {o ?? "—"}
                    </td>
                  </tr>
                );
              })}
            </tbody>
          </table>

          <div className="muted small" style={{ marginTop: 8 }}>
            Last run: {new Date(latest.run_at).toLocaleString()} · {latest.window_days}d window
          </div>
        </>
      )}
    </section>
  );
}

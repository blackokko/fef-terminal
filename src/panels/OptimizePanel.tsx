import { useState } from "react";
import { useToast } from "../components/Toast";
import { fetchJson } from "../lib/api";
import { formatDollars } from "../lib/format";

// Walk-forward parameter optimization — grid sweep + train/test split.
// Find threshold params that maximized Sharpe on past sessions WITHOUT
// overfitting (test Sharpe is the real metric — train can always be tuned).

interface SweepResult {
  wall_proximity_pts: number;
  wall_collapse_pct: number;
  wall_collapse_floor_m: number;
  absorption_fe_ratio_min: number;
  train_sharpe: number | null;
  test_sharpe: number | null;
  train_trades: number;
  test_trades: number;
  train_win_rate_pct: number;
  test_win_rate_pct: number;
  train_total_pnl: number;
  test_total_pnl: number;
}

interface SweepResponse {
  elapsed_ms?: number;
  total_combos?: number;
  iters_in_window?: number;
  train_fraction?: number;
  top_results?: SweepResult[];
  error?: string;
}

export function OptimizePanel() {
  const toast = useToast();
  const [days, setDays] = useState(30);
  const [running, setRunning] = useState(false);
  const [result, setResult] = useState<SweepResponse | null>(null);

  const run = async () => {
    setRunning(true);
    setResult(null);
    try {
      const r = await fetchJson<SweepResponse>("/optimize/sweep", toast, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ days, lookahead_iters: 3, train_fraction: 0.6 }),
      });
      if (r) setResult(r);
    } finally {
      setRunning(false);
    }
  };

  return (
    <section className="sidebar-section">
      <h3 className="sidebar-section-title">Parameter sweep · walk-forward</h3>

      <div className="optimize-controls">
        <label className="small muted">Window</label>
        <input
          type="number"
          value={days}
          onChange={(e) => setDays(Number(e.target.value) || 30)}
          min={3}
          max={365}
          className="settings-input"
          style={{ width: 60 }}
        />
        <span className="small muted">days</span>
        <button onClick={() => void run()} disabled={running}>
          {running ? "Running…" : "Run sweep"}
        </button>
      </div>

      {result?.error && <p style={{ color: "var(--color-red)" }}>{result.error}</p>}

      {result && result.top_results && (
        <div className="optimize-summary small muted">
          {result.total_combos} combos · {result.iters_in_window} iters · {result.elapsed_ms}ms ·
          train {Math.round((result.train_fraction ?? 0.6) * 100)}% / test {Math.round((1 - (result.train_fraction ?? 0.6)) * 100)}%
        </div>
      )}

      {result?.top_results && result.top_results.length > 0 && (
        <div className="optimize-results">
          <div className="optimize-header">
            <span>Params (prox / col% / floor / fe)</span>
            <span>Test Sharpe</span>
            <span>Test win%</span>
            <span>Test P/L</span>
          </div>
          {result.top_results.slice(0, 10).map((r, i) => (
            <div key={i} className={`optimize-row ${i === 0 ? "optimize-best" : ""}`}>
              <span className="optimize-params">
                {r.wall_proximity_pts}/{(r.wall_collapse_pct * 100).toFixed(0)}%/{r.wall_collapse_floor_m}M/{r.absorption_fe_ratio_min}
              </span>
              <span style={{ color: (r.test_sharpe ?? 0) >= 1 ? "var(--color-green)" : (r.test_sharpe ?? 0) < 0 ? "var(--color-red)" : undefined }}>
                {r.test_sharpe ?? "n/a"}
              </span>
              <span>{r.test_win_rate_pct}% ({r.test_trades}T)</span>
              <span style={{ color: r.test_total_pnl >= 0 ? "var(--color-green)" : "var(--color-red)" }}>
                {formatDollars(r.test_total_pnl, { sign: true })}
              </span>
            </div>
          ))}
        </div>
      )}

      {result?.top_results && result.top_results.length === 0 && (
        <p className="muted small">No results — not enough data in window.</p>
      )}

      <p className="muted small" style={{ marginTop: 8 }}>
        Test Sharpe is the real metric (out-of-sample). Train Sharpe will
        always look good — it's the params we optimized for.
      </p>
    </section>
  );
}

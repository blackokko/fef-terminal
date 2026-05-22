import { useEffect, useState } from "react";
import { useToast } from "../components/Toast";

const API_BASE = "http://127.0.0.1:8765";

interface StrategyPreset {
  id: number;
  name: string;
  description: string;
  fef_loop_interval_sec: number;
  wall_proximity_pts: number;
  wall_collapse_pct: number;
  wall_collapse_floor_m: number;
  absorption_fe_ratio_min: number;
}

interface BacktestResult {
  preset_id: number;
  preset_name: string;
  trade_count: number;
  iters_scanned?: number;
  skipped?: { no_dir?: number; no_opt?: number; no_targets?: number; exception?: number };
  wins: number;
  losses: number;
  win_rate_pct: number;
  expectancy_R: number;
  sharpe_ratio: number | null;
  max_drawdown_R: number;
  profit_factor: number | null;
  cumulative_R: number;
  error?: string;
}

interface HistoryRow {
  id: number;
  run_at: string;
  session_date_from: string;
  session_date_to: string;
  apply_be_ladder: boolean;
  preset_count: number;
  notes: string;
  winner_preset_name: string | null;
  winner_cumulative_r: number | null;
  winner_expectancy_r: number | null;
  winner_sharpe: number | null;
  regime_distribution?: Record<string, number>;
  dominant_regime?: string;
}

/**
 * Preset Tournament — runs the full-pipeline backtester across every saved
 * StrategyPreset on a chosen date range and presents a leaderboard.
 *
 * Lets you compare "which preset would have done best on last week?" without
 * actually swapping configs live. Calls /backtest/full_pipeline for each
 * preset (currently uses the live engine's thresholds; preset-specific
 * threshold backtest is a follow-up — for now this just runs the same
 * backtest N times so you see consistency).
 */
export function PresetTournamentPanel() {
  const [presets, setPresets] = useState<StrategyPreset[]>([]);
  const [from, setFrom] = useState<string>(() => {
    const d = new Date();
    d.setDate(d.getDate() - 7);
    return d.toISOString().slice(0, 10);
  });
  const [to, setTo] = useState<string>(() => new Date().toISOString().slice(0, 10));
  const [applyBE, setApplyBE] = useState(true);
  const [results, setResults] = useState<BacktestResult[]>([]);
  const [running, setRunning] = useState(false);
  const [history, setHistory] = useState<HistoryRow[]>([]);
  const [showHistory, setShowHistory] = useState(false);
  const [notes, setNotes] = useState("");
  const toast = useToast();

  useEffect(() => {
    void (async () => {
      try {
        const r = await fetch(`${API_BASE}/presets`);
        if (r.ok) setPresets(await r.json());
      } catch {}
    })();
    void refreshHistory();
  }, []);

  const refreshHistory = async () => {
    try {
      const r = await fetch(`${API_BASE}/backtest/tournament_runs?limit=30`);
      if (r.ok) setHistory(await r.json());
    } catch {}
  };

  const saveTournament = async (resultsToSave: BacktestResult[]) => {
    try {
      await fetch(`${API_BASE}/backtest/save_tournament`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          session_date_from: from,
          session_date_to: to,
          apply_be_ladder: applyBE,
          notes: notes,
          results: resultsToSave,
        }),
      });
      await refreshHistory();
    } catch {}
  };

  const runTournament = async () => {
    if (presets.length === 0) {
      toast.show("No strategy presets defined yet — create one first", { variant: "warn" });
      return;
    }
    setRunning(true);
    setResults([]);
    try {
      const out: BacktestResult[] = [];
      for (const p of presets) {
        try {
          const r = await fetch(
            `${API_BASE}/backtest/full_pipeline?session_date_from=${from}&session_date_to=${to}&apply_be_ladder=${applyBE}&preset_id=${p.id}`
          );
          // Push the result even on 0 trades — user wants to see WHY.
          const data = r.ok ? await r.json() : { error: `HTTP ${r.status}` };
          out.push({
            preset_id: p.id,
            preset_name: p.name,
            trade_count: data.trade_count ?? 0,
            iters_scanned: data.iters_scanned,
            skipped: data.skipped,
            wins: data.wins ?? 0,
            losses: data.losses ?? 0,
            win_rate_pct: data.win_rate_pct ?? 0,
            expectancy_R: data.expectancy_R ?? 0,
            sharpe_ratio: data.sharpe_ratio ?? null,
            max_drawdown_R: data.max_drawdown_R ?? 0,
            profit_factor: data.profit_factor ?? null,
            cumulative_R: data.cumulative_R ?? 0,
            error: data.error,
          });
        } catch (e) {
          out.push({
            preset_id: p.id, preset_name: p.name,
            trade_count: 0, wins: 0, losses: 0, win_rate_pct: 0,
            expectancy_R: 0, sharpe_ratio: null, max_drawdown_R: 0,
            profit_factor: null, cumulative_R: 0,
            error: `request failed: ${(e as Error).message}`,
          });
        }
      }
      out.sort((a, b) => (b.cumulative_R ?? -99) - (a.cumulative_R ?? -99));
      setResults(out);
      const tradeable = out.filter(r => r.trade_count > 0).length;
      const errored = out.filter(r => r.error).length;
      toast.show(
        `Tournament: ${tradeable}/${out.length} presets produced trades${errored ? ` — ${errored} errored` : ""}`,
        { variant: tradeable > 0 ? "success" : "warn" }
      );
      // Auto-save the run so it shows up in history.
      if (tradeable > 0) void saveTournament(out);
    } finally {
      setRunning(false);
    }
  };

  return (
    <section className="research-section">
      <h2 className="research-section-title">Preset Tournament</h2>
      <p className="muted small">
        Backtest every saved strategy preset against historical session data.
        Ranked by Sharpe ratio. Picks the configuration that historically
        delivered the best risk-adjusted return.
      </p>

      <div className="research-controls">
        <label>
          <span className="muted small">From</span>
          <input type="date" value={from} onChange={(e) => setFrom(e.target.value)} />
        </label>
        <label>
          <span className="muted small">To</span>
          <input type="date" value={to} onChange={(e) => setTo(e.target.value)} />
        </label>
        <label style={{ display: "flex", alignItems: "center", gap: 4 }}>
          <input type="checkbox" checked={applyBE} onChange={(e) => setApplyBE(e.target.checked)} />
          <span className="muted small">Simulate BE ladder</span>
        </label>
        <label style={{ flex: 1, minWidth: 180 }}>
          <span className="muted small">Notes (optional)</span>
          <input type="text" value={notes} onChange={(e) => setNotes(e.target.value)}
                 placeholder="e.g. high-vol day, post-Fed test"
                 style={{ width: "100%" }} />
        </label>
        <button onClick={() => void runTournament()} disabled={running}
                className="research-run-button">
          {running ? `Running… (${presets.length} presets)` : "Run Tournament"}
        </button>
        <button onClick={() => setShowHistory(s => !s)}
                style={{ background: "transparent", border: "1px solid var(--color-border)",
                         padding: "6px 12px", borderRadius: 3, color: "var(--color-text-muted)",
                         cursor: "pointer", fontSize: 11 }}>
          {showHistory ? "Hide history" : `History (${history.length})`}
        </button>
      </div>

      {showHistory && (
        <div className="research-history" style={{ marginBottom: 12, padding: 10,
             background: "rgba(40,80,120,0.10)", borderRadius: 4 }}>
          <div className="muted small" style={{ marginBottom: 6 }}>
            Past tournament runs (newest first)
          </div>
          {history.length === 0 ? (
            <p className="muted small">No saved runs yet. Run the tournament to populate.</p>
          ) : (
            <table style={{ width: "100%", fontSize: 11, fontFamily: "var(--font-mono)" }}>
              <thead>
                <tr style={{ color: "var(--color-text-muted)" }}>
                  <th style={{ textAlign: "left" }}>Run at</th>
                  <th style={{ textAlign: "left" }}>Range</th>
                  <th style={{ textAlign: "left" }}>Regime</th>
                  <th style={{ textAlign: "left" }}>Winner</th>
                  <th style={{ textAlign: "right" }}>Cum R</th>
                  <th style={{ textAlign: "right" }}>Exp R</th>
                  <th style={{ textAlign: "right" }}>Sharpe</th>
                  <th style={{ textAlign: "left" }}>Notes</th>
                </tr>
              </thead>
              <tbody>
                {history.map(h => {
                  const regime = h.dominant_regime ?? "UNKNOWN";
                  const dist = h.regime_distribution ?? {};
                  const regimeFmt = (s: string) => s.replace(/_/g, " ").toLowerCase().replace(/\b\w/g, c => c.toUpperCase());
                  const regimeTip = Object.entries(dist).sort((a,b) => b[1]-a[1])
                    .map(([k,v]) => `${regimeFmt(k)} ${(v*100).toFixed(0)}%`).join(", ");
                  return (
                    <tr key={h.id}>
                      <td>{new Date(h.run_at).toLocaleString("en-US", {
                        timeZone: "America/Chicago",
                        month: "short", day: "numeric",
                        hour: "numeric", minute: "2-digit", hour12: true,
                      }).toLowerCase().replace(" am"," am").replace(" pm"," pm")}</td>
                      <td>{h.session_date_from}{h.session_date_to !== h.session_date_from ? `…${h.session_date_to}` : ""}</td>
                      <td title={regimeTip}>{regimeFmt(regime)}</td>
                      <td><strong>{h.winner_preset_name ?? "—"}</strong></td>
                      <td style={{ textAlign: "right",
                        color: (h.winner_cumulative_r ?? 0) >= 0 ? "var(--color-green)" : "var(--color-red)" }}>
                        {h.winner_cumulative_r != null ? `${h.winner_cumulative_r >= 0 ? "+" : ""}${h.winner_cumulative_r.toFixed(2)}` : "—"}
                      </td>
                      <td style={{ textAlign: "right" }}>
                        {h.winner_expectancy_r != null ? `${h.winner_expectancy_r >= 0 ? "+" : ""}${h.winner_expectancy_r.toFixed(3)}` : "—"}
                      </td>
                      <td style={{ textAlign: "right" }}>
                        {h.winner_sharpe != null ? h.winner_sharpe.toFixed(3) : "—"}
                      </td>
                      <td className="muted">{h.notes || "—"}</td>
                    </tr>
                  );
                })}
              </tbody>
            </table>
          )}
        </div>
      )}

      <div className="research-results">
        {results.length === 0 ? (
          <p className="muted small">
            {presets.length === 0
              ? "No saved presets yet. Create some in the Strategy Presets section below."
              : `Ready: ${presets.length} preset(s) loaded. Click Run Tournament to backtest.`}
          </p>
        ) : (
          <table className="research-leaderboard">
            <thead>
              <tr>
                <th>Rank</th>
                <th>Preset</th>
                <th>Trades</th>
                <th>Win rate</th>
                <th>Expectancy R</th>
                <th>Sharpe</th>
                <th>Max DD R</th>
                <th>Profit factor</th>
                <th>Cumulative R</th>
                <th>Notes</th>
              </tr>
            </thead>
            <tbody>
              {results.map((r, i) => {
                const hasData = r.trade_count > 0;
                const note = r.error
                  ? `error: ${r.error}`
                  : !hasData && r.iters_scanned !== undefined
                    ? `0 trades from ${r.iters_scanned} iters — ${
                        Object.entries(r.skipped ?? {})
                          .filter(([, n]) => (n ?? 0) > 0)
                          .map(([k, n]) => `${k}: ${n}`)
                          .join(", ") || "no matching iters"
                      }`
                    : "";
                return (
                  <tr key={r.preset_id} className={hasData && i === 0 ? "winner-row" : ""}
                      style={{ opacity: hasData ? 1 : 0.6 }}>
                    <td><strong>{i + 1}</strong></td>
                    <td>{r.preset_name}</td>
                    <td>{r.trade_count}</td>
                    <td>{hasData ? `${r.win_rate_pct.toFixed(1)}%` : "—"}</td>
                    <td style={{ color: hasData ? (r.expectancy_R >= 0 ? "var(--color-green)" : "var(--color-red)") : undefined }}>
                      {hasData ? `${r.expectancy_R >= 0 ? "+" : ""}${r.expectancy_R.toFixed(3)}` : "—"}
                    </td>
                    <td>{r.sharpe_ratio != null ? r.sharpe_ratio.toFixed(3) : "—"}</td>
                    <td>{hasData ? r.max_drawdown_R.toFixed(2) : "—"}</td>
                    <td>{r.profit_factor != null ? r.profit_factor.toFixed(2) : "—"}</td>
                    <td style={{ color: hasData ? (r.cumulative_R >= 0 ? "var(--color-green)" : "var(--color-red)") : undefined }}>
                      {hasData ? `${r.cumulative_R >= 0 ? "+" : ""}${r.cumulative_R.toFixed(2)}` : "—"}
                    </td>
                    <td style={{ fontSize: 10, color: r.error ? "var(--color-red)" : "var(--color-text-muted)" }}>
                      {note}
                    </td>
                  </tr>
                );
              })}
            </tbody>
          </table>
        )}
      </div>

      <div className="muted small" style={{ marginTop: 8, fontStyle: "italic" }}>
        Each preset's thresholds are injected into a fresh FEFEngine and every
        historical iter is re-evaluated through it. Different presets produce
        different verdicts → different trades → different leaderboard rows.
      </div>
    </section>
  );
}

import { useEffect, useState } from "react";

const API_BASE = "http://127.0.0.1:8765";

interface FrictionScenarios {
  best_live: number;
  realistic_live: number;
  worst_live: number;
  friction_breakdown: Record<string, number>;
}

interface DedupSection {
  window_min: number;
  total_stacked: number;
  unique_setups: number;
  dropped_dupes: number;
  actual_paper_pnl: number;
  counterfactual_paper_pnl: number;
  actual_wins: number;
  counterfactual_wins: number;
  actual_win_rate_pct: number;
  counterfactual_win_rate_pct: number;
  actual_scenarios?: FrictionScenarios;
  counterfactual_scenarios?: FrictionScenarios;
}

interface BECounterfactualResult {
  session_date: string;
  parents_analyzed: number;
  trades_improved_by_cf: number;
  trades_unchanged: number;
  total_actual_pnl: number;
  total_counterfactual_pnl: number;
  incremental_upside_dollars: number;
  actual_scenarios?: FrictionScenarios;
  counterfactual_scenarios?: FrictionScenarios;
  n_trades?: number;
  avg_qty_per_trade?: number;
  dedup?: DedupSection;
  parents: Array<{
    parent_id: number;
    contract: string;
    qty: number;
    entry_option_price: number;
    be_step_reached_R: number;
    status: string;
    actual_pnl_dollars: number;
    counterfactual_pnl_dollars: number;
    delta_dollars: number;
  }>;
}

interface StrategyOutcome {
  n_simulated: number;
  wins: number;
  losses: number;
  win_rate_pct: number;
  expectancy_r: number;
  total_R: number;
}

interface PollingCFResult {
  target_interval_sec: number;
  actual_loop_interval_sec: number;
  flow_snapshots_total: number;
  flow_snapshots_sampled: number;
  actual_actionable_signals: number;
  counterfactual_actionable_signals: number;
  incremental_signals: number;
  unique_setups_after_dedup: number;
  stacked_dupes_within_kept: number;
  outcomes_current_be: StrategyOutcome;
  outcomes_bcd: StrategyOutcome;
}

interface PollingCFResponse {
  session_date: string;
  comparisons: PollingCFResult[];
}

interface CooldownCFResult {
  cooldown_sec: number;
  actual_signals_fired: number;
  counterfactual_signals_kept: number;
  signals_blocked_by_cooldown: number;
  unique_setups_after_dedup?: number;
  stacked_dupes_within_kept?: number;
  outcomes_current_be?: StrategyOutcome;
  outcomes_bcd?: StrategyOutcome;
}

const EMPTY_OUTCOME: StrategyOutcome = {
  n_simulated: 0, wins: 0, losses: 0,
  win_rate_pct: 0, expectancy_r: 0, total_R: 0,
};
const safeOutcome = (o?: StrategyOutcome): StrategyOutcome => o ?? EMPTY_OUTCOME;

interface CooldownCFResponse {
  session_date: string;
  comparisons: CooldownCFResult[];
}

function todayET(): string {
  const d = new Date();
  const et = new Date(d.getTime() - 4 * 60 * 60 * 1000);
  return et.toISOString().slice(0, 10);
}

export function CounterfactualPanel() {
  const [sessionDate, setSessionDate] = useState(todayET());
  const [beResult, setBeResult] = useState<BECounterfactualResult | null>(null);
  const [pollingResult, setPollingResult] = useState<PollingCFResponse | null>(null);
  const [cooldownResult, setCooldownResult] = useState<CooldownCFResponse | null>(null);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const runBoth = async () => {
    setLoading(true);
    setError(null);
    try {
      const [be, polling, cooldown] = await Promise.all([
        fetch(`${API_BASE}/diag/counterfactual_signal_strength?session_date=${sessionDate}`)
          .then(r => r.ok ? r.json() : Promise.reject(`BE: ${r.status}`)),
        fetch(`${API_BASE}/diag/counterfactual_polling_interval?session_date=${sessionDate}&intervals=30,60,90`)
          .then(r => r.ok ? r.json() : Promise.reject(`Polling: ${r.status}`)),
        fetch(`${API_BASE}/diag/counterfactual_cooldown?session_date=${sessionDate}&cooldowns=60,180,300,600`)
          .then(r => r.ok ? r.json() : Promise.reject(`Cooldown: ${r.status}`)),
      ]);
      setBeResult(be);
      setPollingResult(polling);
      setCooldownResult(cooldown);
    } catch (e) {
      setError(String(e));
    }
    setLoading(false);
  };

  useEffect(() => { void runBoth(); }, []);  // initial load

  return (
    <section className="sidebar-section">
      <h3 className="sidebar-section-title">What-If Analysis</h3>
      <p className="muted small" style={{ marginBottom: 8 }}>
        "What if" comparisons — show how proposed changes would affect WR% and P/L,
        without actually shipping the change. Freeze-safe (pure analysis).
      </p>

      <div style={{ display: "flex", gap: 6, marginBottom: 10 }}>
        <input
          type="date"
          value={sessionDate}
          onChange={e => setSessionDate(e.target.value)}
          style={{ flex: 1, padding: 4 }}
        />
        <button onClick={() => void runBoth()} disabled={loading}>
          {loading ? "Running..." : "Run both"}
        </button>
      </div>

      {error && (
        <div className="muted small" style={{ color: "var(--color-red)", marginBottom: 8 }}>
          Error: {error}
        </div>
      )}

      {/* BE Ladder + Take-Profit counterfactual */}
      <div style={{ marginBottom: 14 }}>
        <strong className="small">BE Ladder + B+C+D Exits</strong>
        <div className="muted small" style={{ marginBottom: 4 }}>
          Option-price take-profits, trailing peak, time-decay scaling
        </div>
        {!beResult ? (
          <p className="muted small">no data yet — run the analysis</p>
        ) : beResult.parents_analyzed === 0 ? (
          <p className="muted small">no closed trades for this date</p>
        ) : (
          <>
            {/* Three-scenario comparison: paper / realistic / worst */}
            <table style={{ width: "100%", fontSize: 12, borderCollapse: "collapse" }}>
              <thead>
                <tr style={{ borderBottom: "1px solid var(--color-border)" }}>
                  <th style={{ textAlign: "left", padding: "4px 6px" }}>Scenario</th>
                  <th style={{ textAlign: "right", padding: "4px 6px" }}>Current BE</th>
                  <th style={{ textAlign: "right", padding: "4px 6px" }}>B+C+D</th>
                  <th style={{ textAlign: "right", padding: "4px 6px" }}>Δ</th>
                </tr>
              </thead>
              <tbody>
                <tr>
                  <td style={{ padding: "4px 6px" }}>Paper</td>
                  <td style={{
                    textAlign: "right", padding: "4px 6px",
                    color: beResult.total_actual_pnl >= 0 ? "var(--color-green)" : "var(--color-red)",
                  }}>
                    ${beResult.total_actual_pnl.toFixed(0)}
                  </td>
                  <td style={{ textAlign: "right", padding: "4px 6px", color: "var(--color-blue)" }}>
                    ${beResult.total_counterfactual_pnl.toFixed(0)}
                  </td>
                  <td style={{
                    textAlign: "right", padding: "4px 6px",
                    color: beResult.incremental_upside_dollars >= 0 ? "var(--color-green)" : "var(--color-red)",
                  }}>
                    +${beResult.incremental_upside_dollars.toFixed(0)}
                  </td>
                </tr>
                {beResult.actual_scenarios && beResult.counterfactual_scenarios && (
                  <>
                    <tr>
                      <td style={{ padding: "4px 6px" }}>Best live</td>
                      <td style={{
                        textAlign: "right", padding: "4px 6px",
                        color: beResult.actual_scenarios.best_live >= 0 ? "var(--color-green)" : "var(--color-red)",
                      }}>
                        ${beResult.actual_scenarios.best_live.toFixed(0)}
                      </td>
                      <td style={{
                        textAlign: "right", padding: "4px 6px",
                        color: beResult.counterfactual_scenarios.best_live >= 0 ? "var(--color-green)" : "var(--color-red)",
                      }}>
                        ${beResult.counterfactual_scenarios.best_live.toFixed(0)}
                      </td>
                      <td style={{ textAlign: "right", padding: "4px 6px" }}>
                        ${(beResult.counterfactual_scenarios.best_live - beResult.actual_scenarios.best_live).toFixed(0)}
                      </td>
                    </tr>
                    <tr>
                      <td style={{ padding: "4px 6px" }}>Realistic live</td>
                      <td style={{
                        textAlign: "right", padding: "4px 6px",
                        color: beResult.actual_scenarios.realistic_live >= 0 ? "var(--color-green)" : "var(--color-red)",
                      }}>
                        ${beResult.actual_scenarios.realistic_live.toFixed(0)}
                      </td>
                      <td style={{
                        textAlign: "right", padding: "4px 6px",
                        color: beResult.counterfactual_scenarios.realistic_live >= 0 ? "var(--color-green)" : "var(--color-red)",
                      }}>
                        ${beResult.counterfactual_scenarios.realistic_live.toFixed(0)}
                      </td>
                      <td style={{ textAlign: "right", padding: "4px 6px" }}>
                        ${(beResult.counterfactual_scenarios.realistic_live - beResult.actual_scenarios.realistic_live).toFixed(0)}
                      </td>
                    </tr>
                    <tr>
                      <td style={{ padding: "4px 6px" }}>Worst live</td>
                      <td style={{
                        textAlign: "right", padding: "4px 6px", fontWeight: 700,
                        color: beResult.actual_scenarios.worst_live >= 0 ? "var(--color-green)" : "var(--color-red)",
                      }}>
                        ${beResult.actual_scenarios.worst_live.toFixed(0)}
                      </td>
                      <td style={{
                        textAlign: "right", padding: "4px 6px", fontWeight: 700,
                        color: beResult.counterfactual_scenarios.worst_live >= 0 ? "var(--color-green)" : "var(--color-red)",
                      }}>
                        ${beResult.counterfactual_scenarios.worst_live.toFixed(0)}
                      </td>
                      <td style={{ textAlign: "right", padding: "4px 6px" }}>
                        ${(beResult.counterfactual_scenarios.worst_live - beResult.actual_scenarios.worst_live).toFixed(0)}
                      </td>
                    </tr>
                  </>
                )}
              </tbody>
            </table>
            <div className="muted small" style={{ marginTop: 6 }}>
              {beResult.trades_improved_by_cf}/{beResult.parents_analyzed} trades improved by CF
              {beResult.n_trades && ` · ${beResult.n_trades} entries × ${beResult.avg_qty_per_trade}avg qty`}
            </div>

            {beResult.dedup && beResult.dedup.actual_scenarios && beResult.dedup.counterfactual_scenarios && (
              <div style={{ marginTop: 10, paddingTop: 8, borderTop: "1px solid var(--color-border)" }}>
                <strong className="small">Dedup view ({beResult.dedup.window_min}-min window, direction|strike)</strong>
                <div className="muted small" style={{ marginBottom: 4 }}>
                  {beResult.dedup.total_stacked} stacked → {beResult.dedup.unique_setups} unique setups
                  &nbsp;({beResult.dedup.dropped_dupes} dupes dropped) ·
                  Per-setup WR: actual {beResult.dedup.actual_win_rate_pct.toFixed(0)}% ·
                  CF {beResult.dedup.counterfactual_win_rate_pct.toFixed(0)}%
                </div>
                <table style={{ width: "100%", fontSize: 12, borderCollapse: "collapse" }}>
                  <thead>
                    <tr style={{ borderBottom: "1px solid var(--color-border)" }}>
                      <th style={{ textAlign: "left", padding: "4px 6px" }}>Scenario</th>
                      <th style={{ textAlign: "right", padding: "4px 6px" }}>Current BE</th>
                      <th style={{ textAlign: "right", padding: "4px 6px" }}>B+C+D</th>
                      <th style={{ textAlign: "right", padding: "4px 6px" }}>Δ</th>
                    </tr>
                  </thead>
                  <tbody>
                    <tr>
                      <td style={{ padding: "4px 6px" }}>Paper</td>
                      <td style={{
                        textAlign: "right", padding: "4px 6px",
                        color: beResult.dedup.actual_paper_pnl >= 0 ? "var(--color-green)" : "var(--color-red)",
                      }}>${beResult.dedup.actual_paper_pnl.toFixed(0)}</td>
                      <td style={{
                        textAlign: "right", padding: "4px 6px",
                        color: beResult.dedup.counterfactual_paper_pnl >= 0 ? "var(--color-green)" : "var(--color-red)",
                      }}>${beResult.dedup.counterfactual_paper_pnl.toFixed(0)}</td>
                      <td style={{ textAlign: "right", padding: "4px 6px" }}>
                        ${(beResult.dedup.counterfactual_paper_pnl - beResult.dedup.actual_paper_pnl).toFixed(0)}
                      </td>
                    </tr>
                    <tr>
                      <td style={{ padding: "4px 6px" }}>Realistic live</td>
                      <td style={{
                        textAlign: "right", padding: "4px 6px",
                        color: beResult.dedup.actual_scenarios.realistic_live >= 0 ? "var(--color-green)" : "var(--color-red)",
                      }}>${beResult.dedup.actual_scenarios.realistic_live.toFixed(0)}</td>
                      <td style={{
                        textAlign: "right", padding: "4px 6px",
                        color: beResult.dedup.counterfactual_scenarios.realistic_live >= 0 ? "var(--color-green)" : "var(--color-red)",
                      }}>${beResult.dedup.counterfactual_scenarios.realistic_live.toFixed(0)}</td>
                      <td style={{ textAlign: "right", padding: "4px 6px" }}>
                        ${(beResult.dedup.counterfactual_scenarios.realistic_live - beResult.dedup.actual_scenarios.realistic_live).toFixed(0)}
                      </td>
                    </tr>
                    <tr>
                      <td style={{ padding: "4px 6px" }}>Worst live</td>
                      <td style={{
                        textAlign: "right", padding: "4px 6px", fontWeight: 700,
                        color: beResult.dedup.actual_scenarios.worst_live >= 0 ? "var(--color-green)" : "var(--color-red)",
                      }}>${beResult.dedup.actual_scenarios.worst_live.toFixed(0)}</td>
                      <td style={{
                        textAlign: "right", padding: "4px 6px", fontWeight: 700,
                        color: beResult.dedup.counterfactual_scenarios.worst_live >= 0 ? "var(--color-green)" : "var(--color-red)",
                      }}>${beResult.dedup.counterfactual_scenarios.worst_live.toFixed(0)}</td>
                      <td style={{ textAlign: "right", padding: "4px 6px" }}>
                        ${(beResult.dedup.counterfactual_scenarios.worst_live - beResult.dedup.actual_scenarios.worst_live).toFixed(0)}
                      </td>
                    </tr>
                  </tbody>
                </table>
                <div className="muted small" style={{ marginTop: 4, fontStyle: "italic" }}>
                  Signal quality only — answers "what if I fired ONCE per setup?"
                </div>
              </div>
            )}
          </>
        )}
      </div>

      {/* Polling interval counterfactual */}
      <div>
        <strong className="small">Polling Interval (Option A)</strong>
        <div className="muted small" style={{ marginBottom: 4 }}>
          Replay FEF at finer cadence. Current: 120s.
        </div>
        {!pollingResult || !pollingResult.comparisons ? (
          <p className="muted small">no data yet</p>
        ) : (
          <>
            <table style={{ width: "100%", fontSize: 11, borderCollapse: "collapse" }}>
              <thead>
                <tr style={{ borderBottom: "1px solid var(--color-border)" }}>
                  <th style={{ textAlign: "left", padding: "4px 6px" }}>Interval</th>
                  <th style={{ textAlign: "right", padding: "4px 6px" }}>Setups</th>
                  <th style={{ textAlign: "right", padding: "4px 6px" }}>BE WR%</th>
                  <th style={{ textAlign: "right", padding: "4px 6px" }}>BE Tot R</th>
                  <th style={{ textAlign: "right", padding: "4px 6px" }}>B+C+D WR%</th>
                  <th style={{ textAlign: "right", padding: "4px 6px" }}>B+C+D Tot R</th>
                </tr>
              </thead>
              <tbody>
                {pollingResult.comparisons.map(c => {
                  const be = safeOutcome(c.outcomes_current_be);
                  const bcd = safeOutcome(c.outcomes_bcd);
                  return (
                  <tr key={c.target_interval_sec}>
                    <td style={{ padding: "4px 6px" }}>{c.target_interval_sec}s</td>
                    <td style={{ textAlign: "right", padding: "4px 6px", fontWeight: 600 }}>
                      {c.unique_setups_after_dedup ?? 0}
                    </td>
                    <td style={{ textAlign: "right", padding: "4px 6px" }}>
                      {be.win_rate_pct.toFixed(0)}%
                    </td>
                    <td style={{
                      textAlign: "right", padding: "4px 6px",
                      color: be.total_R > 0 ? "var(--color-green)" : "var(--color-red)",
                    }}>
                      {be.total_R > 0 ? "+" : ""}{be.total_R.toFixed(1)}
                    </td>
                    <td style={{ textAlign: "right", padding: "4px 6px" }}>
                      {bcd.win_rate_pct.toFixed(0)}%
                    </td>
                    <td style={{
                      textAlign: "right", padding: "4px 6px", fontWeight: 600,
                      color: bcd.total_R > 0 ? "var(--color-green)" : "var(--color-red)",
                    }}>
                      {bcd.total_R > 0 ? "+" : ""}{bcd.total_R.toFixed(1)}
                    </td>
                  </tr>
                  );
                })}
              </tbody>
            </table>
            <div className="muted small" style={{ marginTop: 4, fontStyle: "italic" }}>
              Per dedup setup. BE = current ladder (+0.5R → stop-to-entry).
              B+C+D = option-T1 + trail 15% from peak.
            </div>
          </>
        )}
      </div>

      {/* Cooldown counterfactual */}
      <div style={{ marginTop: 14, paddingTop: 10, borderTop: "1px solid var(--color-border)" }}>
        <strong className="small">Per-symbol cooldown</strong>
        <div className="muted small" style={{ marginBottom: 4 }}>
          Filter today's signals through alternative cooldown rules. Current: 60s.
        </div>
        {!cooldownResult || !cooldownResult.comparisons ? (
          <p className="muted small">no data yet</p>
        ) : (
          <>
            <table style={{ width: "100%", fontSize: 11, borderCollapse: "collapse" }}>
              <thead>
                <tr style={{ borderBottom: "1px solid var(--color-border)" }}>
                  <th style={{ textAlign: "left", padding: "4px 6px" }}>Cooldown</th>
                  <th style={{ textAlign: "right", padding: "4px 6px" }}>Setups</th>
                  <th style={{ textAlign: "right", padding: "4px 6px" }}>Blocked</th>
                  <th style={{ textAlign: "right", padding: "4px 6px" }}>BE WR%</th>
                  <th style={{ textAlign: "right", padding: "4px 6px" }}>BE Tot R</th>
                  <th style={{ textAlign: "right", padding: "4px 6px" }}>B+C+D WR%</th>
                  <th style={{ textAlign: "right", padding: "4px 6px" }}>B+C+D Tot R</th>
                </tr>
              </thead>
              <tbody>
                {cooldownResult.comparisons.map(c => {
                  const be = safeOutcome(c.outcomes_current_be);
                  const bcd = safeOutcome(c.outcomes_bcd);
                  return (
                  <tr key={c.cooldown_sec}>
                    <td style={{ padding: "4px 6px" }}>{c.cooldown_sec}s</td>
                    <td style={{ textAlign: "right", padding: "4px 6px", fontWeight: 600 }}>
                      {c.unique_setups_after_dedup ?? 0}
                    </td>
                    <td style={{
                      textAlign: "right", padding: "4px 6px",
                      color: c.signals_blocked_by_cooldown > 0 ? "var(--color-amber)" : undefined,
                    }}>
                      {c.signals_blocked_by_cooldown}
                    </td>
                    <td style={{ textAlign: "right", padding: "4px 6px" }}>
                      {be.win_rate_pct.toFixed(0)}%
                    </td>
                    <td style={{
                      textAlign: "right", padding: "4px 6px",
                      color: be.total_R > 0 ? "var(--color-green)" : "var(--color-red)",
                    }}>
                      {be.total_R > 0 ? "+" : ""}{be.total_R.toFixed(1)}
                    </td>
                    <td style={{ textAlign: "right", padding: "4px 6px" }}>
                      {bcd.win_rate_pct.toFixed(0)}%
                    </td>
                    <td style={{
                      textAlign: "right", padding: "4px 6px", fontWeight: 600,
                      color: bcd.total_R > 0 ? "var(--color-green)" : "var(--color-red)",
                    }}>
                      {bcd.total_R > 0 ? "+" : ""}{bcd.total_R.toFixed(1)}
                    </td>
                  </tr>
                  );
                })}
              </tbody>
            </table>
            <div className="muted small" style={{ marginTop: 4, fontStyle: "italic" }}>
              Per dedup setup. BE = current ladder. B+C+D = upgraded exits.
              Pick best (cooldown × strategy) combo by Total R.
            </div>
          </>
        )}
      </div>
    </section>
  );
}

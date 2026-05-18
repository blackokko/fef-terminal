import { useEffect, useState } from "react";

const API_BASE = "http://127.0.0.1:8765";

interface ExitConfig {
  long: {
    strategy: "spx_points" | "fixed_dollar" | "option_price";
    dollar_t1: number;
    dollar_t2: number;
    dollar_t3: number;
    dollar_stop: number;
  };
  spread: {
    stop_multiplier: number;
    tp_close_debit: number;
  };
}

export function ExitStrategyPanel() {
  const [cfg, setCfg] = useState<ExitConfig | null>(null);
  const [busy, setBusy] = useState(false);

  const refresh = async () => {
    try {
      const r = await fetch(`${API_BASE}/exit_strategy`);
      if (r.ok) setCfg(await r.json());
    } catch {}
  };

  useEffect(() => { void refresh(); }, []);

  const update = async (body: Record<string, any>) => {
    setBusy(true);
    try {
      await fetch(`${API_BASE}/exit_strategy`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(body),
      });
      await refresh();
    } catch {} finally { setBusy(false); }
  };

  if (!cfg) return (
    <section className="sidebar-section">
      <h3 className="sidebar-section-title">Exit strategy</h3>
      <p className="muted small">loading...</p>
    </section>
  );

  return (
    <section className="sidebar-section">
      <h3 className="sidebar-section-title">Exit strategy</h3>

      <div className="muted small" style={{ marginBottom: 4, fontWeight: 600, color: "var(--color-text)" }}>
        Long calls / puts
      </div>
      <div style={{ display: "flex", flexDirection: "column", gap: 4 }}>
        <label className="exit-strat-radio">
          <input type="radio" name="long_strategy" value="spx_points"
                 checked={cfg.long.strategy === "spx_points"}
                 onChange={() => void update({ long_strategy: "spx_points" })}
                 disabled={busy} />
          <span>
            <strong>SPX-points (Option C)</strong>
            <div className="muted small">Watcher fires market sells when SPX crosses T1 / T2 / T3 / stop. Exact, no gamma/theta approximation.</div>
          </span>
        </label>
        <label className="exit-strat-radio">
          <input type="radio" name="long_strategy" value="fixed_dollar"
                 checked={cfg.long.strategy === "fixed_dollar"}
                 onChange={() => void update({ long_strategy: "fixed_dollar" })}
                 disabled={busy} />
          <span>
            <strong>Fixed dollar (Option A)</strong>
            <div className="muted small">$ profit/loss targets translated to option-price bracket via current delta. Standard IBKR bracket.</div>
          </span>
        </label>
      </div>

      {cfg.long.strategy === "fixed_dollar" && (
        <table className="exit-strat-table">
          <tbody>
            <tr>
              <td>T1 target</td>
              <td>$ <input type="number" step="100" value={cfg.long.dollar_t1}
                          onChange={(e) => void update({ dollar_t1: Number(e.target.value) })}
                          className="settings-input" disabled={busy} /></td>
            </tr>
            <tr>
              <td>T2 target</td>
              <td>$ <input type="number" step="100" value={cfg.long.dollar_t2}
                          onChange={(e) => void update({ dollar_t2: Number(e.target.value) })}
                          className="settings-input" disabled={busy} /></td>
            </tr>
            <tr>
              <td>T3 target</td>
              <td>$ <input type="number" step="100" value={cfg.long.dollar_t3}
                          onChange={(e) => void update({ dollar_t3: Number(e.target.value) })}
                          className="settings-input" disabled={busy} /></td>
            </tr>
            <tr>
              <td>Stop (loss)</td>
              <td>$ <input type="number" step="100" value={cfg.long.dollar_stop}
                          onChange={(e) => void update({ dollar_stop: Number(e.target.value) })}
                          className="settings-input" disabled={busy} /></td>
            </tr>
          </tbody>
        </table>
      )}

      <div className="muted small" style={{ marginTop: 10, marginBottom: 4, fontWeight: 600, color: "var(--color-text)" }}>
        Credit spreads (CCS / PCS)
      </div>
      <table className="exit-strat-table">
        <tbody>
          <tr>
            <td>TP close debit</td>
            <td>$ <input type="number" step="0.01" value={cfg.spread.tp_close_debit}
                        onChange={(e) => void update({ spread_tp_close_debit: Number(e.target.value) })}
                        className="settings-input" disabled={busy} /></td>
          </tr>
          <tr>
            <td>Stop multiplier</td>
            <td><input type="number" step="0.5" value={cfg.spread.stop_multiplier}
                       onChange={(e) => void update({ spread_stop_multiplier: Number(e.target.value) })}
                       className="settings-input" disabled={busy} />× initial credit</td>
          </tr>
        </tbody>
      </table>
      <div className="muted small" style={{ marginTop: 4 }}>
        Spread watcher closes the position when: current debit ≤ TP ($0.05 = expire worthless), OR
        current debit ≥ stop_multiplier × initial credit ($X credit → close at $X×{cfg.spread.stop_multiplier} loss).
      </div>
    </section>
  );
}

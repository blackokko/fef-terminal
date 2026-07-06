import { useEffect, useState } from "react";
import { NumberSetting } from "../components/NumberSetting";

const API_BASE = "http://127.0.0.1:8765";

interface ExitConfig {
  long: {
    strategy: "spx_points" | "fixed_dollar" | "option_price";
    dollar_t1: number;
    dollar_t2: number;
    dollar_t3: number;
    dollar_stop: number;
    dollar_semantics: "cumulative_realized" | "whole_unrealized";
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
        Long calls / puts (auto-trade picks based on size)
      </div>
      <div style={{ display: "flex", flexDirection: "column", gap: 4 }}>
        <label className="exit-strat-radio">
          <input type="radio" name="long_strategy" value="spx_points"
                 checked={cfg.long.strategy === "spx_points"}
                 onChange={() => void update({ long_strategy: "spx_points" })}
                 disabled={busy} />
          <span>
            <strong>SPX-points</strong>
            <div className="muted small">Watcher fires market sells when SPX crosses T1 / T2 / T3 / stop. Exact, no gamma/theta approximation. Auto-trade uses this for qty &lt; 15.</div>
          </span>
        </label>
        <label className="exit-strat-radio">
          <input type="radio" name="long_strategy" value="fixed_dollar"
                 checked={cfg.long.strategy === "fixed_dollar"}
                 onChange={() => void update({ long_strategy: "fixed_dollar" })}
                 disabled={busy} />
          <span>
            <strong>Fixed dollar</strong>
            <div className="muted small">$ profit/loss targets translated to option-price bracket via current delta. Standard IBKR bracket. Auto-trade uses this only for qty ≥ 15.</div>
          </span>
        </label>
      </div>

      {cfg.long.strategy === "fixed_dollar" && (
        <>
        <div style={{ margin: "6px 0 4px" }}>
          <select
            value={cfg.long.dollar_semantics}
            disabled={busy}
            onChange={(e) => void update({ dollar_semantics: e.target.value })}
            style={{ fontSize: 11, width: "100%" }}
          >
            <option value="cumulative_realized">Targets = cumulative banked ($1000 TP ≈ $1000 booked)</option>
            <option value="whole_unrealized">Targets = full-position unrealized (legacy)</option>
          </select>
          <div className="muted small" style={{ marginTop: 2 }}>
            {cfg.long.dollar_semantics === "cumulative_realized"
              ? "Each tier's slice earns the increment between targets, so hitting all 3 banks ≈ the top number. Tiers fire later."
              : "Tier fires when the whole position's unrealized P&L crosses the target; each sells only its slice, so 3 TPs bank far less than the top number (the 7510C case)."}
          </div>
        </div>
        <table className="exit-strat-table">
          <tbody>
            <tr>
              <td>T1 target</td>
              <td>$ <NumberSetting step="100" value={cfg.long.dollar_t1}
                          onCommit={(n) => void update({ dollar_t1: n })} /></td>
            </tr>
            <tr>
              <td>T2 target</td>
              <td>$ <NumberSetting step="100" value={cfg.long.dollar_t2}
                          onCommit={(n) => void update({ dollar_t2: n })} /></td>
            </tr>
            <tr>
              <td>T3 target</td>
              <td>$ <NumberSetting step="100" value={cfg.long.dollar_t3}
                          onCommit={(n) => void update({ dollar_t3: n })} /></td>
            </tr>
            <tr>
              <td>Stop (loss)</td>
              <td>$ <NumberSetting step="100" value={cfg.long.dollar_stop}
                          onCommit={(n) => void update({ dollar_stop: n })} /></td>
            </tr>
          </tbody>
        </table>
        </>
      )}

      <div className="muted small" style={{ marginTop: 10, marginBottom: 4, fontWeight: 600, color: "var(--color-text)" }}>
        Credit spreads (CCS / PCS)
      </div>
      <table className="exit-strat-table">
        <tbody>
          <tr>
            <td>TP close debit</td>
            <td>$ <NumberSetting step="0.01" value={cfg.spread.tp_close_debit}
                        onCommit={(n) => void update({ spread_tp_close_debit: n })} /></td>
          </tr>
          <tr>
            <td>Stop multiplier</td>
            <td><NumberSetting step="0.5" value={cfg.spread.stop_multiplier}
                       onCommit={(n) => void update({ spread_stop_multiplier: n })} />× initial credit</td>
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

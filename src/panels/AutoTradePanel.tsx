import { useEffect, useState } from "react";
import { useToast } from "../components/Toast";

const API_BASE = "http://127.0.0.1:8765";

interface AutoTradeStatus {
  enabled: boolean;
  live_trading_env: boolean;
  confidence_floor: string;
  cool_down_sec: number;
  cool_down_remaining_sec: number;
  max_per_session: number;
  count_session: number;
  last_at_ms: number;
  last_block_reason: string;
  session_cap_reached: boolean;
  runner_protect_enabled: boolean;
}

export function AutoTradePanel() {
  const [status, setStatus] = useState<AutoTradeStatus | null>(null);
  const [busy, setBusy] = useState(false);
  const toast = useToast();

  const refresh = async () => {
    try {
      const r = await fetch(`${API_BASE}/auto_trade/status`);
      if (r.ok) setStatus(await r.json());
    } catch {}
  };

  useEffect(() => {
    void refresh();
    const id = window.setInterval(refresh, 3000);
    return () => window.clearInterval(id);
  }, []);

  const handleToggle = async () => {
    if (!status) return;
    const turningOn = !status.enabled;
    if (turningOn) {
      if (!status.live_trading_env) {
        toast.show("FEF_LIVE_TRADING env not set — restart sidecar in live mode first", { variant: "error" });
        return;
      }
      const ok = window.confirm(
        `ENABLE AUTO-TRADE?\n\n` +
        `When ON, every FEF verdict that meets these gates will fire a REAL bracket order via IBKR:\n` +
        `  • direction != WAIT\n` +
        `  • confidence >= ${status.confidence_floor}\n` +
        `  • not inside a calendar pause window\n` +
        `  • >= ${status.cool_down_sec}s since last auto-trade\n` +
        `  • session count < ${status.max_per_session}\n` +
        `  • verdict has option pricing\n` +
        `  • qty within max_contracts_per_trade\n\n` +
        `Real orders. Real money. Are you sure?`
      );
      if (!ok) return;
    }
    setBusy(true);
    try {
      const r = await fetch(`${API_BASE}/auto_trade/toggle`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ enabled: turningOn, confirm: true }),
      });
      const data = await r.json();
      if (r.ok) {
        toast.show(turningOn ? "AUTO-TRADE ENABLED" : "Auto-trade disabled",
                   { variant: turningOn ? "warn" : "success" });
        await refresh();
      } else {
        toast.show(`Toggle failed: ${data.detail || "unknown"}`, { variant: "error" });
      }
    } catch (e) {
      toast.show(`Toggle failed: ${(e as Error).message}`, { variant: "error" });
    } finally {
      setBusy(false);
    }
  };

  const handleResetCounter = async () => {
    if (!window.confirm("Reset the per-session auto-trade counter? You'll be able to fire more orders.")) return;
    setBusy(true);
    try {
      await fetch(`${API_BASE}/auto_trade/reset_session`, { method: "POST" });
      await refresh();
      toast.show("Counter reset", { variant: "success" });
    } catch {} finally { setBusy(false); }
  };

  const updateSetting = async (key: string, value: any) => {
    setBusy(true);
    try {
      await fetch(`${API_BASE}/auto_trade/toggle`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ enabled: status?.enabled ?? false, confirm: true, [key]: value }),
      });
      await refresh();
    } catch {} finally { setBusy(false); }
  };

  if (!status) return (
    <section className="sidebar-section">
      <h3 className="sidebar-section-title">Auto-trade</h3>
      <p className="muted small">loading...</p>
    </section>
  );

  const heroColor = status.enabled
    ? "var(--color-red)"
    : (status.live_trading_env ? "var(--color-amber)" : "var(--color-text-muted)");

  return (
    <section className="sidebar-section auto-trade-panel">
      <h3 className="sidebar-section-title">Auto-trade</h3>

      <div className="auto-trade-hero" style={{ borderLeft: `4px solid ${heroColor}` }}>
        <div style={{ color: heroColor, fontWeight: 700, fontSize: 15, fontFamily: "var(--font-mono)" }}>
          {status.enabled ? "🔴 LIVE — AUTO ORDERS ON"
           : status.live_trading_env ? "⚠ OFF — LIVE ENV READY"
           : "◌ OFF — LIVE ENV NOT SET"}
        </div>
        <div className="muted small">
          {status.enabled
            ? "Every qualifying verdict fires a REAL bracket order"
            : status.live_trading_env
              ? "Toggle ON to start auto-firing orders on HIGH-conviction signals"
              : "Restart sidecar via FEF-Paper-AutoTrade.bat (or FEF-Live-AutoTrade.bat) to enable"}
        </div>
      </div>

      {!status.live_trading_env && !status.enabled && (
        <div style={{
          background: "var(--color-bg-elevated)",
          borderLeft: "3px solid var(--color-red)",
          padding: "6px 10px", marginBottom: 8, fontSize: 11,
        }}>
          <strong style={{ color: "var(--color-red)" }}>Button disabled.</strong>{" "}
          The sidecar wasn't launched with FEF_LIVE_TRADING=true. Close the app
          and relaunch via <code>FEF-Paper-AutoTrade.bat</code> (paper account,
          safe) or <code>FEF-Live-AutoTrade.bat</code> (real money).
        </div>
      )}

      <button
        onClick={() => void handleToggle()}
        disabled={busy || (!status.live_trading_env && !status.enabled)}
        className={status.enabled ? "kill-switch-button" : ""}
        style={{
          width: "100%", marginBottom: 8,
          background: !status.live_trading_env && !status.enabled
            ? "var(--color-bg-elevated)"
            : status.enabled ? "var(--color-red)" : "var(--color-green)",
          color: !status.live_trading_env && !status.enabled
            ? "var(--color-text-muted)" : "white",
          fontWeight: 700, padding: 10, fontSize: 13,
          cursor: !status.live_trading_env && !status.enabled ? "not-allowed" : "pointer",
        }}
      >
        {busy ? "…" :
         !status.live_trading_env && !status.enabled ? "ENABLE AUTO-TRADE (live env required)" :
         status.enabled ? "🛑 DISABLE AUTO-TRADE" : "▶ ENABLE AUTO-TRADE"}
      </button>

      <table className="auto-trade-settings">
        <tbody>
          <tr>
            <td>Confidence floor</td>
            <td>
              <select
                value={status.confidence_floor}
                onChange={(e) => void updateSetting("confidence_floor", e.target.value)}
                disabled={busy}
                className="settings-input"
              >
                <option>HIGH</option>
                <option>MEDIUM</option>
                <option>LOW</option>
              </select>
            </td>
          </tr>
          <tr>
            <td>Cool-down (sec)</td>
            <td>
              <input type="number" min={60} max={3600} step={30}
                     value={status.cool_down_sec}
                     onChange={(e) => void updateSetting("cool_down_sec", Number(e.target.value))}
                     disabled={busy} className="settings-input" style={{ width: 80 }} />
            </td>
          </tr>
          <tr>
            <td>Max per session</td>
            <td>
              <input type="number" min={1} max={50}
                     value={status.max_per_session}
                     onChange={(e) => void updateSetting("max_per_session", Number(e.target.value))}
                     disabled={busy} className="settings-input" style={{ width: 80 }} />
            </td>
          </tr>
        </tbody>
      </table>

      <div className="auto-trade-counters">
        <div>
          <div className="muted small">Session</div>
          <div style={{
            fontFamily: "var(--font-mono)", fontWeight: 600,
            color: status.session_cap_reached ? "var(--color-red)" : "var(--color-text)",
          }}>
            {status.count_session} / {status.max_per_session}
          </div>
        </div>
        <div>
          <div className="muted small">Cool-down</div>
          <div style={{ fontFamily: "var(--font-mono)", fontWeight: 600 }}>
            {status.cool_down_remaining_sec > 0 ? `${status.cool_down_remaining_sec}s` : "ready"}
          </div>
        </div>
        {status.session_cap_reached && (
          <button onClick={() => void handleResetCounter()} disabled={busy} style={{ alignSelf: "center" }}>
            Reset
          </button>
        )}
      </div>

      <div className="muted small" style={{ marginTop: 8, borderTop: "1px solid var(--color-border)", paddingTop: 6 }}>
        <label style={{ display: "flex", alignItems: "center", gap: 6, cursor: "pointer" }}>
          <input
            type="checkbox"
            checked={status.runner_protect_enabled}
            onChange={async (e) => {
              setBusy(true);
              try {
                await fetch(`${API_BASE}/auto_trade/runner_protect`, {
                  method: "POST",
                  headers: { "Content-Type": "application/json" },
                  body: JSON.stringify({ enabled: e.target.checked }),
                });
                await refresh();
              } catch {} finally { setBusy(false); }
            }}
            disabled={busy}
          />
          <span>
            <strong>Runner protect</strong> — after T1 fills, exit remaining
            runners at market if premium retraces to T1 fill (85/10/5 split)
          </span>
        </label>
      </div>

      {status.last_block_reason && (
        <div className="muted small" style={{ marginTop: 6 }}>
          Last block: <span style={{ color: "var(--color-amber)" }}>{status.last_block_reason}</span>
        </div>
      )}
    </section>
  );
}

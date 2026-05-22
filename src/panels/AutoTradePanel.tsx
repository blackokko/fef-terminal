import { useEffect, useState } from "react";
import { useToast } from "../components/Toast";

const API_BASE = "http://127.0.0.1:8765";

interface AutoTradeStatus {
  enabled: boolean;
  live_trading_env: boolean;
  confidence_floor: string;
  cool_down_sec: number;
  cool_down_remaining_sec: number;
  count_session: number;
  last_at_ms: number;
  last_block_reason: string;
  runner_protect_enabled: boolean;
  qty_override: number;
  trigger_types: string[];
}

interface AutoTradePreset {
  id: number;
  name: string;
  description: string;
  confidence_floor: string;
  cool_down_sec: number;
  trigger_types: string[];
  qty_override: number;
  runner_protect_enabled: boolean;
}

const TRIGGER_TYPES = [
  { value: "confluence", label: "Confluence (wall collapse + aligned)",
    hint: "Today: 41% win, +0.37R — the only profitable subset so far" },
  { value: "aligned_only", label: "Aligned only",
    hint: "Today: 22% win, −0.26R — keep ON to grow sample for validation" },
];

export function AutoTradePanel() {
  const [status, setStatus] = useState<AutoTradeStatus | null>(null);
  const [busy, setBusy] = useState(false);
  const [presets, setPresets] = useState<AutoTradePreset[]>([]);
  const [selectedPresetId, setSelectedPresetId] = useState<number | "">("");
  const toast = useToast();

  const refresh = async () => {
    try {
      const r = await fetch(`${API_BASE}/auto_trade/status`);
      if (r.ok) setStatus(await r.json());
    } catch {}
  };

  const refreshPresets = async () => {
    try {
      const r = await fetch(`${API_BASE}/auto_trade/presets`);
      if (r.ok) setPresets(await r.json());
    } catch {}
  };

  useEffect(() => {
    void refresh();
    void refreshPresets();
    const id = window.setInterval(refresh, 3000);
    return () => window.clearInterval(id);
  }, []);

  const handleApplyPreset = async (presetId: number) => {
    setBusy(true);
    try {
      const r = await fetch(`${API_BASE}/auto_trade/presets/${presetId}/apply`, { method: "POST" });
      if (r.ok) {
        const data = await r.json();
        toast.show(`Preset applied: ${data.applied}`, { variant: "success" });
        await refresh();
      } else {
        toast.show("Failed to apply preset", { variant: "error" });
      }
    } catch (e) {
      toast.show(`Apply failed: ${(e as Error).message}`, { variant: "error" });
    } finally {
      setBusy(false);
    }
  };

  const handleSavePreset = async () => {
    if (!status) return;
    const name = window.prompt(
      "Save current auto-trade settings as a preset.\n\nName:",
      ""
    )?.trim();
    if (!name) return;
    const description = window.prompt("Description (optional):", "") ?? "";

    setBusy(true);
    try {
      const r = await fetch(`${API_BASE}/auto_trade/presets`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          name, description,
          confidence_floor: status.confidence_floor,
          cool_down_sec: status.cool_down_sec,
          trigger_types: status.trigger_types,
          qty_override: status.qty_override,
          runner_protect_enabled: status.runner_protect_enabled,
        }),
      });
      const data = await r.json();
      if (r.ok) {
        toast.show(`Preset '${data.name}' saved`, { variant: "success" });
        await refreshPresets();
        setSelectedPresetId(data.preset_id);
      } else {
        toast.show(`Save failed: ${data.detail || "unknown"}`, { variant: "error" });
      }
    } catch (e) {
      toast.show(`Save failed: ${(e as Error).message}`, { variant: "error" });
    } finally {
      setBusy(false);
    }
  };

  const handleDeletePreset = async () => {
    if (!selectedPresetId) return;
    const preset = presets.find(p => p.id === selectedPresetId);
    if (!preset) return;
    if (!window.confirm(`Delete preset '${preset.name}'? This can't be undone.`)) return;
    setBusy(true);
    try {
      const r = await fetch(`${API_BASE}/auto_trade/presets/${selectedPresetId}`, { method: "DELETE" });
      if (r.ok) {
        toast.show(`Preset '${preset.name}' deleted`, { variant: "success" });
        setSelectedPresetId("");
        await refreshPresets();
      } else {
        toast.show("Delete failed", { variant: "error" });
      }
    } catch (e) {
      toast.show(`Delete failed: ${(e as Error).message}`, { variant: "error" });
    } finally {
      setBusy(false);
    }
  };

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
        `  • >= ${status.cool_down_sec}s since last auto-trade (per symbol)\n` +
        `  • symbol budget not yet reached (set per-symbol cap in Scanner panel)\n` +
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
    // Clear preset selection — manual edit means current state no longer matches a preset.
    if (selectedPresetId !== "") setSelectedPresetId("");
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
            ? `Every qualifying verdict fires a REAL bracket order (>= ${status.confidence_floor} conviction)`
            : status.live_trading_env
              ? `Toggle ON to start auto-firing orders on ${status.confidence_floor}-conviction signals`
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

      <div style={{
        display: "flex", gap: 6, alignItems: "center", marginBottom: 8,
        padding: "6px 8px", background: "var(--color-bg-elevated)",
        borderRadius: 4, fontSize: 11,
      }}>
        <span className="muted small" style={{ fontWeight: 600 }}>Preset:</span>
        <select
          value={selectedPresetId}
          onChange={(e) => {
            const val = e.target.value;
            if (val === "") { setSelectedPresetId(""); return; }
            const id = Number(val);
            setSelectedPresetId(id);
            void handleApplyPreset(id);
          }}
          disabled={busy || presets.length === 0}
          className="settings-input"
          style={{ flex: 1, minWidth: 0 }}
          title={
            selectedPresetId !== ""
              ? presets.find(p => p.id === selectedPresetId)?.description || ""
              : "Select a saved preset to apply its settings"
          }
        >
          <option value="">— select preset —</option>
          {presets.map(p => (
            <option key={p.id} value={p.id}>{p.name}</option>
          ))}
        </select>
        <button
          onClick={() => void handleSavePreset()}
          disabled={busy}
          title="Save the current settings as a new preset"
          style={{ fontSize: 11, padding: "2px 8px" }}
        >
          Save as…
        </button>
        {selectedPresetId !== "" && (
          <button
            onClick={() => void handleDeletePreset()}
            disabled={busy}
            title="Delete the currently selected preset"
            style={{ fontSize: 11, padding: "2px 8px", color: "var(--color-red)" }}
          >
            ✕
          </button>
        )}
      </div>

      <table className="auto-trade-settings">
        <tbody>
          <tr>
            <td>Confidence floor <span className="muted small">(label only)</span></td>
            <td>
              <select
                value={status.confidence_floor}
                onChange={(e) => void updateSetting("confidence_floor", e.target.value)}
                disabled={busy}
                className="settings-input"
                title="Demoted to informational. The Trigger Types selection above is the actual auto-trade volume knob. Set FEF_USE_CONFIDENCE_FLOOR=true env to re-enable as a gate."
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
            <td>Trigger types</td>
            <td>
              <div style={{ display: "flex", flexDirection: "column", gap: 2 }}>
                {TRIGGER_TYPES.map(t => (
                  <label key={t.value} style={{ display: "flex", gap: 4, alignItems: "center", fontSize: 11 }}>
                    <input
                      type="checkbox"
                      checked={(status.trigger_types ?? []).includes(t.value)}
                      onChange={(e) => {
                        const cur = status.trigger_types ?? [];
                        const next = e.target.checked
                          ? [...cur, t.value]
                          : cur.filter(x => x !== t.value);
                        void updateSetting("trigger_types", next.length ? next : ["confluence"]);
                      }}
                      disabled={busy}
                    />
                    <span title={t.hint}>{t.label}</span>
                  </label>
                ))}
              </div>
            </td>
          </tr>
          <tr>
            <td>Contracts per fire</td>
            <td>
              <input type="number" min={0} max={100}
                     value={status.qty_override}
                     onChange={(e) => void updateSetting("qty_override", Number(e.target.value))}
                     disabled={busy} className="settings-input" style={{ width: 80 }}
                     title="Fixed contract size for every auto-trade fire. 0 = use FEF's confidence-tier default (HIGH=15, MEDIUM=10, LOW=5)" />
            </td>
          </tr>
        </tbody>
      </table>

      <div className="auto-trade-counters">
        <div>
          <div className="muted small">Session activity</div>
          <div style={{ fontFamily: "var(--font-mono)", fontWeight: 600 }}>
            {status.count_session} {status.count_session === 1 ? "fire" : "fires"}
          </div>
        </div>
        <div>
          <div className="muted small">Cool-down</div>
          <div style={{ fontFamily: "var(--font-mono)", fontWeight: 600 }}>
            {status.cool_down_remaining_sec > 0 ? `${status.cool_down_remaining_sec}s` : "ready"}
          </div>
        </div>
        <button onClick={() => void handleResetCounter()} disabled={busy}
                style={{ alignSelf: "center" }} title="Resets per-symbol counters (use if you hit a symbol's cap and want a fresh budget mid-session)">
          Reset counters
        </button>
      </div>

      <div className="muted small" style={{ marginTop: 4, fontSize: 10, fontStyle: "italic" }}>
        Per-symbol trade budgets live in the Scanner panel ↓
      </div>

      <div className="muted small" style={{ marginTop: 8, borderTop: "1px solid var(--color-border)", paddingTop: 6 }}>
        <label style={{ display: "flex", alignItems: "center", gap: 6, cursor: "pointer" }}>
          <input
            type="checkbox"
            checked={status.runner_protect_enabled}
            onChange={async (e) => {
              setBusy(true);
              if (selectedPresetId !== "") setSelectedPresetId("");
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

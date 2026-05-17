import { useEffect, useState } from "react";

// IMPROVEMENTS #24: UI-tunable settings backed by ~/.fef-terminal/config.json.
// Some take effect immediately (wall thresholds), others need a sidecar restart
// (loop intervals).

interface Config {
  fef_loop_interval_sec: number;
  quantdata_poll_interval_sec: number;
  wall_proximity_pts: number;
  wall_collapse_pct: number;
  wall_collapse_floor_m: number;
  default_qty: number;
  audio_alerts: boolean;
}

const API_BASE = "http://127.0.0.1:8765";

export function SettingsPanel() {
  const [config, setConfig] = useState<Config | null>(null);
  const [saveStatus, setSaveStatus] = useState<string | null>(null);

  useEffect(() => {
    void fetch(`${API_BASE}/config`)
      .then((r) => r.json())
      .then((d) => setConfig(d as Config))
      .catch(() => setConfig(null));
  }, []);

  if (!config) {
    return (
      <details className="sidebar-section">
        <summary className="sidebar-section-title">Settings</summary>
        <p className="muted">loading…</p>
      </details>
    );
  }

  const save = async (updates: Partial<Config>) => {
    try {
      const r = await fetch(`${API_BASE}/config`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(updates),
      });
      const data = await r.json();
      setConfig(data.config);
      const requiresRestart = data.requires_restart || [];
      const changed = Object.keys(updates);
      const needsRestart = changed.some((k) => requiresRestart.includes(k));
      setSaveStatus(needsRestart ? "Saved — restart sidecar for full effect" : "✓ Saved");
      window.setTimeout(() => setSaveStatus(null), 3000);
    } catch (e) {
      setSaveStatus(`Failed: ${(e as Error).message}`);
    }
  };

  return (
    <details className="sidebar-section">
      <summary className="sidebar-section-title">Settings</summary>
      <div className="settings-body">
        <Field
          label="FEF interval (s)"
          hint="Restart required"
          value={config.fef_loop_interval_sec}
          onChange={(v) => save({ fef_loop_interval_sec: v })}
          min={10}
          max={600}
        />
        <Field
          label="QuantData poll (s)"
          hint="Restart required"
          value={config.quantdata_poll_interval_sec}
          onChange={(v) => save({ quantdata_poll_interval_sec: v })}
          min={5}
          max={120}
        />
        <Field
          label="Wall proximity (pts)"
          hint="Hot-reload"
          value={config.wall_proximity_pts}
          onChange={(v) => save({ wall_proximity_pts: v })}
          min={1}
          max={30}
          step={0.5}
        />
        <Field
          label="Wall collapse %"
          hint="Hot-reload (0.20 = 20%)"
          value={config.wall_collapse_pct}
          onChange={(v) => save({ wall_collapse_pct: v })}
          min={0.05}
          max={0.80}
          step={0.05}
        />
        <Field
          label="Wall collapse floor ($M)"
          hint="Hot-reload"
          value={config.wall_collapse_floor_m}
          onChange={(v) => save({ wall_collapse_floor_m: v })}
          min={0}
          max={1000}
          step={1}
        />
        <Field
          label="Default qty"
          hint="Override per ticket"
          value={config.default_qty}
          onChange={(v) => save({ default_qty: v })}
          min={1}
          max={100}
        />
        {saveStatus && <div className="settings-status small">{saveStatus}</div>}
      </div>
    </details>
  );
}

function Field({
  label,
  hint,
  value,
  onChange,
  min,
  max,
  step = 1,
}: {
  label: string;
  hint: string;
  value: number;
  onChange: (v: number) => void;
  min: number;
  max: number;
  step?: number;
}) {
  return (
    <div className="settings-row">
      <div className="settings-label">
        <span>{label}</span>
        <span className="muted small">{hint}</span>
      </div>
      <input
        type="number"
        value={value}
        min={min}
        max={max}
        step={step}
        onChange={(e) => onChange(Number(e.target.value))}
        className="settings-input"
      />
    </div>
  );
}

import { useEffect, useState } from "react";
import { NumberSetting } from "../components/NumberSetting";

const API_BASE = "http://127.0.0.1:8765";

type Mode = "off" | "advisory" | "active";

interface PhaseConfig {
  mode: Mode;
  window_sec: number;
  vel_sec: number;
  vel_ignite: number;
  vel_stall: number;
  min_move: number;
  bounce_pt: number;
  iv_delta: number;
  mp_overshoot: number;
  fade_min: number;
  ride_min: number;
}

interface PhaseVerdict {
  state: string;
  direction: string | null;
  score: number;
  threshold: number;
  reasons: string[];
  actionable: boolean;
  mode?: string;
  ts_ms?: number;
  spx?: number;
}

interface PhaseResponse {
  config: PhaseConfig;
  last_verdict: PhaseVerdict | null;
  presets: Record<string, Partial<PhaseConfig>>;
}

const STATE_COLOR: Record<string, string> = {
  RIDE: "#e0b341",
  FADE: "#4bb3fd",
  WAIT: "var(--color-text-muted, #888)",
};

// (state field, label, step) — order shown in the table.
const KNOBS: [keyof PhaseConfig, string, string][] = [
  ["min_move", "Min move (pt)", "1"],
  ["bounce_pt", "Bounce/reject (pt)", "0.5"],
  ["vel_ignite", "Ignite vel (pt/min)", "1"],
  ["vel_stall", "Stall vel (pt/min)", "0.5"],
  ["vel_sec", "Velocity lookback (s)", "5"],
  ["window_sec", "Context window (s)", "30"],
  ["mp_overshoot", "Max-pain overshoot (pt)", "1"],
  ["iv_delta", "IV climax delta", "0.001"],
  ["ride_min", "RIDE confluence", "1"],
  ["fade_min", "FADE confluence", "1"],
];

export function PhaseDetectorPanel() {
  const [cfg, setCfg] = useState<PhaseConfig | null>(null);
  const [verdict, setVerdict] = useState<PhaseVerdict | null>(null);
  const [presets, setPresets] = useState<Record<string, Partial<PhaseConfig>>>({});
  const [busy, setBusy] = useState(false);

  const refresh = async () => {
    try {
      const r = await fetch(`${API_BASE}/phase_detector`);
      if (r.ok) {
        const j: PhaseResponse = await r.json();
        setCfg(j.config);
        setVerdict(j.last_verdict);
        setPresets(j.presets || {});
      }
    } catch {}
  };

  useEffect(() => {
    void refresh();
    // Poll so the live verdict + advisory state stay fresh.
    const id = setInterval(() => void refresh(), 5000);
    return () => clearInterval(id);
  }, []);

  const update = async (body: Record<string, unknown>) => {
    setBusy(true);
    try {
      await fetch(`${API_BASE}/phase_detector`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(body),
      });
      await refresh();
    } catch {} finally {
      setBusy(false);
    }
  };

  if (!cfg) {
    return (
      <section className="sidebar-section">
        <h3 className="sidebar-section-title">Phase detector</h3>
        <p className="muted small">loading...</p>
      </section>
    );
  }

  const v = verdict;
  const stale = v?.ts_ms ? Date.now() - v.ts_ms > 90_000 : true;

  return (
    <section className="sidebar-section">
      <h3 className="sidebar-section-title">Phase detector — direction refiner</h3>

      {/* Live verdict */}
      <div
        style={{
          display: "flex",
          alignItems: "center",
          gap: 8,
          padding: "6px 8px",
          borderRadius: 4,
          background: "var(--color-surface-2, #1b1b1f)",
          marginBottom: 8,
        }}
      >
        <span
          style={{
            fontWeight: 700,
            color: v ? STATE_COLOR[v.state] ?? STATE_COLOR.WAIT : STATE_COLOR.WAIT,
          }}
        >
          {v ? v.state : "—"}
        </span>
        <span className="muted small">
          {v?.direction ? v.direction.replace("LONG_", "") : ""}
          {v ? ` ${v.score}/${v.threshold}` : cfg.mode === "off" ? " (mode off)" : " (warming up)"}
          {v && stale ? " · stale" : ""}
        </span>
      </div>
      {v?.reasons?.length ? (
        <div className="muted small" style={{ marginBottom: 8 }}>
          {v.reasons.slice(0, 4).join(" · ")}
        </div>
      ) : null}

      {/* Mode */}
      <div style={{ display: "flex", flexDirection: "column", gap: 4 }}>
        {(
          [
            ["off", "Off", "Not evaluated."],
            ["advisory", "Advisory", "Computes + logs RIDE/FADE/WAIT every poll. Zero trade impact — watch it here first."],
            ["active", "Active (veto)", "Vetoes an auto-trade when a fresh phase verdict DISAGREES with the engine direction (stops wrong-way put entries). Does not yet flip into the fade."],
          ] as [Mode, string, string][]
        ).map(([m, label, desc]) => (
          <label className="exit-strat-radio" key={m}>
            <input
              type="radio"
              name="phase_mode"
              value={m}
              checked={cfg.mode === m}
              onChange={() => void update({ mode: m })}
              disabled={busy}
            />
            <span>
              <strong>{label}</strong>
              <div className="muted small">{desc}</div>
            </span>
          </label>
        ))}
      </div>

      {/* Presets */}
      <div style={{ display: "flex", gap: 6, alignItems: "center", margin: "8px 0 4px" }}>
        <span className="muted small">Preset:</span>
        {Object.keys(presets).map((name) => (
          <button
            key={name}
            disabled={busy}
            onClick={() => void update({ preset: name })}
            title={`Load the ${name} threshold profile (then tune below)`}
            style={{ fontSize: 11, padding: "2px 8px" }}
          >
            {name === "big_move" ? "Big move" : name === "balanced" ? "Balanced" : name}
          </button>
        ))}
      </div>
      <div className="muted small" style={{ marginBottom: 4 }}>
        Presets are starting points (pending backtest calibration) — every value below is editable.
      </div>

      {/* Thresholds */}
      <table className="exit-strat-table">
        <tbody>
          {KNOBS.map(([key, label, step]) => (
            <tr key={key}>
              <td>{label}</td>
              <td>
                <NumberSetting
                  step={step}
                  value={cfg[key] as number}
                  onCommit={(n) => void update({ [key]: n })}
                />
              </td>
            </tr>
          ))}
        </tbody>
      </table>
      <div className="muted small" style={{ marginTop: 4 }}>
        Fed on SPX every 15s. RIDE needs an active velocity thrust; FADE (tighter) needs {cfg.fade_min}+
        pillars to confirm a reversal. See engine/phase_detector.py.
      </div>
    </section>
  );
}

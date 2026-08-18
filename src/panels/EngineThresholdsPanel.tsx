import { useEffect, useState } from "react";

const API_BASE = "http://127.0.0.1:8765";

/**
 * Engine thresholds — plain editor. Replaces StrategyPresetsPanel (retired
 * with the Research-tab teardown, 2026-07-30).
 *
 * WHY PLAIN, AND NOT PRESETS. The preset panel offered named bundles (Pin /
 * Trend / Volatile / Chop / Power Hour) with one-click Apply, alongside a
 * tournament that ranked which bundle "would have won". The ranking was not
 * evidence: the auto walk-forward sweep that produced these bundles returned
 * an optimal `wall_proximity` of 3, then 5, then 8 on consecutive sessions
 * from the same four-knob search. That is noise presented as a
 * recommendation, and one-click application of it to a live engine is the
 * hazard. Editing thresholds is legitimate; being told which values won a
 * backtest is not.
 *
 * So: current values, explicit Save, no bundles, no ranking, no winner row.
 *
 * WHAT THIS WRITES. `POST /config` is the canonical change path. It validates
 * against the UIConfig model server-side (out-of-range values are rejected
 * before persistence), writes one audit row per effective change in
 * `key: old -> new` form, persists to ~/.fef-terminal/config.json, and
 * hot-reloads the engine attributes that can change without a restart. We
 * send ONLY the keys that actually changed, so the audit diff stays precise.
 *
 * `absorption_fe_ratio_min` is included. It had NO config path before
 * 2026-07-30 — the only way to change it was the retired preset panel, whose
 * apply handler wrote `engine._absorption_fe_min` directly. It was added to
 * UIConfig and to update_config's hot-reload list in the same deploy that
 * retired presets, so this panel can actually honour it.
 *
 * The restart warning after a save is read from the endpoint's own
 * `hot_reloaded` response, never hardcoded here — so if the sidecar's list
 * changes, this panel stays correct without an edit.
 */

interface FieldSpec {
  key: string;
  label: string;
  min: number;
  max: number;
  step: number;
  hot: boolean;
  help: string;
}

/** Bounds mirror UIConfig (sidecar/src/fef_sidecar/config_store.py). They are
 *  duplicated here only to catch bad input before the round-trip — the server
 *  remains the authority and re-validates every write. */
const FIELDS: FieldSpec[] = [
  {
    key: "wall_proximity_pts",
    label: "Wall proximity (pts)",
    min: 1.0, max: 30.0, step: 0.5, hot: true,
    help: "Distance from a GEX wall inside which the engine treats price as wall-bound.",
  },
  {
    key: "wall_collapse_pct",
    label: "Wall collapse (fraction)",
    min: 0.05, max: 0.80, step: 0.01, hot: true,
    help: "Fractional drop in wall size that counts as a collapse. 0.20 = 20%.",
  },
  {
    key: "wall_collapse_floor_m",
    label: "Wall collapse floor ($M)",
    min: 0.0, max: 1000.0, step: 1.0, hot: true,
    help: "Minimum wall size (millions) for a collapse to register at all.",
  },
  {
    key: "absorption_fe_ratio_min",
    label: "Absorption FE minimum",
    min: 0.5, max: 50.0, step: 0.5, hot: true,
    help: "Flow-efficiency ratio at or above which a move is flagged as absorption.",
  },
  {
    key: "fef_loop_interval_sec",
    label: "FEF loop interval (sec)",
    min: 10, max: 600, step: 5, hot: false,
    help: "Seconds between engine evaluations. Restart required.",
  },
  {
    key: "quantdata_poll_interval_sec",
    label: "QuantData poll interval (sec)",
    min: 5, max: 120, step: 1, hot: false,
    help: "Seconds between QuantData fetches. Restart required.",
  },
];

type Cfg = Record<string, any>;

export function EngineThresholdsPanel() {
  const [saved, setSaved] = useState<Cfg | null>(null);
  const [draft, setDraft] = useState<Record<string, string>>({});
  const [busy, setBusy] = useState(false);
  const [status, setStatus] = useState<string | null>(null);
  const [err, setErr] = useState<string | null>(null);

  const load = async () => {
    try {
      const r = await fetch(`${API_BASE}/config`);
      if (!r.ok) { setErr(`sidecar returned ${r.status}`); return; }
      const cfg: Cfg = await r.json();
      setSaved(cfg);
      const d: Record<string, string> = {};
      for (const f of FIELDS) d[f.key] = String(cfg[f.key] ?? "");
      setDraft(d);
      setErr(null);
    } catch {
      setErr("sidecar unreachable");
    }
  };

  useEffect(() => { void load(); }, []);

  const problems: Record<string, string> = {};
  for (const f of FIELDS) {
    const raw = draft[f.key];
    if (raw === undefined || raw === "") { problems[f.key] = "required"; continue; }
    const n = Number(raw);
    if (!Number.isFinite(n)) problems[f.key] = "not a number";
    else if (n < f.min || n > f.max) problems[f.key] = `must be ${f.min}–${f.max}`;
  }

  // Only fields that actually differ from the saved value get sent, so the
  // server-side audit row lists exactly what the operator changed.
  const changedKeys = saved
    ? FIELDS.filter((f) => !problems[f.key] && Number(draft[f.key]) !== Number(saved[f.key]))
        .map((f) => f.key)
    : [];
  const hasProblems = Object.keys(problems).length > 0;

  const save = async () => {
    if (!changedKeys.length || hasProblems) return;
    setBusy(true);
    setStatus(null);
    try {
      const updates: Cfg = {};
      for (const k of changedKeys) updates[k] = Number(draft[k]);
      const r = await fetch(`${API_BASE}/config`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(updates),
      });
      if (!r.ok) {
        setStatus(`REJECTED by sidecar (${r.status}) — nothing was changed.`);
        return;
      }
      const res = await r.json();
      setSaved(res.config);
      const touchedHot = changedKeys.filter((k) =>
        (res.hot_reloaded ?? []).includes(k));
      const touchedRestart = changedKeys.filter((k) =>
        !(res.hot_reloaded ?? []).includes(k));
      const bits: string[] = [];
      if (touchedHot.length) bits.push(`live now: ${touchedHot.join(", ")}`);
      if (touchedRestart.length) {
        bits.push(`NEEDS SIDECAR RESTART: ${touchedRestart.join(", ")}`);
      }
      setStatus(`Saved. ${bits.join(" · ")}`);
    } catch {
      setStatus("Save failed — sidecar unreachable. Nothing was changed.");
    } finally {
      setBusy(false);
    }
  };

  const revert = () => {
    if (!saved) return;
    const d: Record<string, string> = {};
    for (const f of FIELDS) d[f.key] = String(saved[f.key] ?? "");
    setDraft(d);
    setStatus(null);
  };

  return (
    <section className="sidebar-section">
      <h3 className="sidebar-section-title">Engine thresholds</h3>

      {err && <p className="muted small">{err}</p>}
      {!saved && !err && <p className="muted small">Loading…</p>}

      {saved && (
        <>
          <p className="muted small" style={{ marginBottom: 8 }}>
            Live engine values. Edits are not applied until you press Save.
          </p>

          {FIELDS.map((f) => {
            const dirty = Number(draft[f.key]) !== Number(saved[f.key]);
            const bad = problems[f.key];
            return (
              <div key={f.key} style={{ marginBottom: 8 }}>
                <label
                  className="muted small"
                  style={{ display: "flex", justifyContent: "space-between", gap: 8 }}
                >
                  <span style={{ color: dirty ? "var(--color-amber)" : undefined }}>
                    {f.label}
                    {dirty ? " *" : ""}
                  </span>
                  <input
                    type="number"
                    className="settings-input"
                    style={{
                      width: 110,
                      borderColor: bad ? "var(--color-red)" : undefined,
                    }}
                    min={f.min}
                    max={f.max}
                    step={f.step}
                    value={draft[f.key] ?? ""}
                    onChange={(e) =>
                      setDraft((d) => ({ ...d, [f.key]: e.target.value }))
                    }
                  />
                </label>
                <div className="muted small" style={{ fontSize: 10 }}>
                  {f.help}
                  {!f.hot && " "}
                  {bad && (
                    <span style={{ color: "var(--color-red)" }}> — {bad}</span>
                  )}
                </div>
              </div>
            );
          })}

          <div style={{ display: "flex", gap: 6, marginTop: 10 }}>
            <button
              onClick={() => void save()}
              disabled={busy || hasProblems || changedKeys.length === 0}
            >
              {busy
                ? "Saving…"
                : changedKeys.length
                  ? `Save ${changedKeys.length} change${changedKeys.length > 1 ? "s" : ""}`
                  : "No changes"}
            </button>
            <button onClick={revert} disabled={busy || changedKeys.length === 0}>
              Revert
            </button>
          </div>

          {status && (
            <p className="muted small" style={{ marginTop: 8 }}>{status}</p>
          )}

          <p className="muted small" style={{ marginTop: 10, fontSize: 10 }}>
            Every effective change writes an audit row (<code>key: old &rarr; new</code>),
            visible in the Audit Log panel. Values are re-validated by the
            sidecar; an out-of-range write is rejected outright rather than
            clamped.
          </p>

          <p className="muted small" style={{ marginTop: 6, fontSize: 10 }}>
            <strong>No value here is known to be optimal.</strong> The nightly
            sweep that used to recommend these returned wall-proximity 3, then
            5, then 8 on consecutive sessions searching the same four knobs.
            Change them for a reason you can state; a backtest ranking is not
            one.
          </p>
        </>
      )}
    </section>
  );
}

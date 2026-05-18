import { useEffect, useRef, useState } from "react";
import { useToast } from "../components/Toast";
import { useSidecar } from "../context/WebSocketContext";

// Market regime banner — what kind of day is this?
// Sits at the TOP of the sidebar so it's always in view.

const REGIME_COLORS: Record<string, string> = {
  PIN_DAY: "#d29922",
  TREND_DAY: "#3fb950",
  CHOP: "#7d8590",
  ABSORPTION_HEAVY: "#a371f7",
  WALL_TEST: "#d29922",
  OPEN_DRIVE: "#58a6ff",
  POWER_HOUR: "#d29922",
  HIGH_VOL: "#f85149",
  UNCLEAR: "#7d8590",
};

const BIAS_LABEL: Record<string, string> = {
  FOLLOW_DRIFT: "Trade with the drift",
  REVERSAL: "Hunt reversals",
  TOWARD_MP: "Lean toward NTM MP",
  NEUTRAL: "No directional edge",
  SKIP: "Stand down",
};

const SIZING_LABEL: Record<string, string> = {
  FULL: "Full size (15)",
  STANDARD: "Standard (10)",
  REDUCED: "Reduced (5-8)",
  TOE_DIP: "Toe-dip (3-5)",
  STAND_DOWN: "Don't take it",
};

const API_BASE = "http://127.0.0.1:8765";

export function RegimePanel() {
  const { lastRegime } = useSidecar();
  const toast = useToast();
  const lastRegimeRef = useRef<string | null>(null);

  // Toast on regime change
  useEffect(() => {
    if (!lastRegime) return;
    if (lastRegimeRef.current && lastRegimeRef.current !== lastRegime.regime) {
      toast.show(
        `Regime → ${lastRegime.label} (${(lastRegime.confidence * 100).toFixed(0)}% conf)`,
        { variant: "info", duration: 5000 },
      );
    }
    lastRegimeRef.current = lastRegime.regime;
  }, [lastRegime?.regime, lastRegime?.confidence, toast]);

  const applyRecommendedPreset = async () => {
    if (!lastRegime?.recommended_preset_hint) return;
    // Find a preset whose name contains the hint (case-insensitive)
    const r = await fetch(`${API_BASE}/presets`);
    if (!r.ok) return;
    const presets = (await r.json()) as { id: number; name: string }[];
    const match = presets.find((p) =>
      p.name.toLowerCase().includes(lastRegime.recommended_preset_hint.toLowerCase()),
    );
    if (!match) {
      toast.show(
        `No preset matching '${lastRegime.recommended_preset_hint}'. Create one in Strategy Presets.`,
        { variant: "warn" },
      );
      return;
    }
    const ar = await fetch(`${API_BASE}/presets/${match.id}/apply`, { method: "POST" });
    if (ar.ok) toast.show(`Applied preset: ${match.name}`, { variant: "success" });
  };

  if (!lastRegime) {
    return (
      <section className="sidebar-section regime-panel">
        <h3 className="sidebar-section-title">Market regime</h3>
        <p className="muted small">warming up…</p>
        <LookbackControl />
      </section>
    );
  }

  const color = REGIME_COLORS[lastRegime.regime] ?? REGIME_COLORS.UNCLEAR;

  return (
    <section className="sidebar-section regime-panel" style={{ borderLeft: `3px solid ${color}` }}>
      <h3 className="sidebar-section-title">Market regime</h3>
      <div className="regime-body">
        <div className="regime-header">
          <span className="regime-label" style={{ color }}>{lastRegime.label}</span>
          <span className="muted small">{(lastRegime.confidence * 100).toFixed(0)}% conf</span>
        </div>
        <div className="regime-rationale small">{lastRegime.rationale}</div>

        <div className="regime-grid">
          <div>
            <div className="regime-grid-label">Bias</div>
            <div className="regime-grid-value">{BIAS_LABEL[lastRegime.bias] ?? lastRegime.bias}</div>
          </div>
          <div>
            <div className="regime-grid-label">Sizing</div>
            <div className="regime-grid-value">{SIZING_LABEL[lastRegime.sizing] ?? lastRegime.sizing}</div>
          </div>
        </div>

        {lastRegime.recommended_patterns.length > 0 && (
          <div className="regime-patterns">
            <div className="regime-grid-label">Watch for</div>
            <div className="regime-patterns-list">
              {lastRegime.recommended_patterns.map((p) => (
                <span key={p} className="pattern-condition">{p}</span>
              ))}
            </div>
          </div>
        )}

        {lastRegime.recommended_preset_hint && (
          <button onClick={() => void applyRecommendedPreset()} style={{ marginTop: 8 }}>
            Apply '{lastRegime.recommended_preset_hint}' preset
          </button>
        )}
        <LookbackControl />
      </div>
    </section>
  );
}

const API_BASE = "http://127.0.0.1:8765";

function LookbackControl() {
  const [info, setInfo] = useState<{
    window_size: number; loop_interval_sec: number;
    minutes_of_context: number; samples_currently: number;
  } | null>(null);
  const [busy, setBusy] = useState(false);

  const refresh = async () => {
    try {
      const r = await fetch(`${API_BASE}/regime/lookback`);
      if (r.ok) setInfo(await r.json());
    } catch {}
  };
  useEffect(() => {
    void refresh();
    const id = window.setInterval(refresh, 5000);
    return () => window.clearInterval(id);
  }, []);

  const setWindow = async (n: number) => {
    setBusy(true);
    try {
      await fetch(`${API_BASE}/regime/lookback`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ window_size: n }),
      });
      await refresh();
    } catch {} finally { setBusy(false); }
  };

  if (!info) return null;
  const presets = [
    { label: "30m", n: Math.max(3, Math.round(30 * 60 / info.loop_interval_sec)) },
    { label: "1h",  n: Math.max(3, Math.round(60 * 60 / info.loop_interval_sec)) },
    { label: "2h",  n: Math.max(3, Math.round(120 * 60 / info.loop_interval_sec)) },
    { label: "4h",  n: Math.max(3, Math.round(240 * 60 / info.loop_interval_sec)) },
    { label: "Full session", n: Math.max(3, Math.round(390 * 60 / info.loop_interval_sec)) },
  ];

  return (
    <div className="regime-lookback" style={{
      marginTop: 8, paddingTop: 8, borderTop: "1px solid var(--color-border)",
    }}>
      <div className="muted small" style={{ marginBottom: 4 }}>
        Lookback: <strong style={{ color: "var(--color-text)" }}>{info.minutes_of_context.toFixed(0)} min</strong>
        ({info.samples_currently}/{info.window_size} samples, {info.loop_interval_sec}s loop)
      </div>
      <div style={{ display: "flex", gap: 4, flexWrap: "wrap" }}>
        {presets.map((p) => (
          <button key={p.label} onClick={() => void setWindow(p.n)}
                  disabled={busy}
                  className={`chart-toolbar-btn ${info.window_size === p.n ? "active" : ""}`}
                  style={{ fontSize: 10, padding: "2px 6px" }}>
            {p.label}
          </button>
        ))}
      </div>
    </div>
  );
}

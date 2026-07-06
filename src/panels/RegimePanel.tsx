import { useEffect, useState } from "react";

// Market regime — VALIDATED live measurements (GET /regime/live).
//
// Replaces the retired session-"vibe" classifier display (PIN_DAY/TREND_DAY/
// CHOP labels — unvalidated, documented mislabeling, consumed by nothing in
// the trading path). Each tile shows a measured reading plus a plain-language
// line describing its effect on the bot. The old /regime/lookback control is
// gone — the tiles have no tunable window.

const API_BASE = "http://127.0.0.1:8765";

interface Tile {
  reading: string;
  label: string;
  effect_on_bot: string;
  updated_at: number | null;
  [key: string]: unknown;
}

interface RegimeLive {
  generated_at_ms: number;
  session_date: string;
  active_symbol: string;
  bar_symbol: string;
  tiles: {
    range_regime: Tile;
    expected_move: Tile;
    flow_bias: Tile;
    events: Tile;
    dealer_gamma: Tile;
  };
}

const TILE_ORDER: { key: keyof RegimeLive["tiles"]; title: string }[] = [
  { key: "range_regime", title: "Range" },
  { key: "expected_move", title: "Expected move" },
  { key: "flow_bias", title: "Flow bias" },
  { key: "events", title: "Events" },
  { key: "dealer_gamma", title: "Dealer gamma" },
];

function tileColor(key: string, t: Tile): string {
  const label = t.label ?? "";
  const effect = t.effect_on_bot ?? "";
  if (effect.startsWith("SKIPPING") || label.startsWith("post-cap")) {
    return "var(--color-red)";
  }
  switch (key) {
    case "range_regime":
      if (label === "compressed") return "var(--color-amber)";
      if (label === "expanded") return "var(--color-green)";
      return "var(--color-text-muted)";
    case "expected_move":
      if (label.startsWith("~2-3x")) return "var(--color-amber)";
      if (label.startsWith("~0.5x")) return "var(--color-text-muted)";
      return "var(--color-text)";
    case "flow_bias":
      if (label === "BULLISH") return "var(--color-green)";
      if (label === "BEARISH") return "var(--color-red)";
      return "var(--color-text-muted)";
    case "events":
      if (label === "FOMC") return "var(--color-red)";
      if (label === "CPI" || label === "NFP") return "var(--color-amber)";
      if (label === "clear") return "var(--color-text-muted)";
      return "#58a6ff";
    case "dealer_gamma":
      if (label === "long") return "var(--color-green)";
      if (label === "short") return "var(--color-red)";
      if (label === "transition") return "var(--color-amber)";
      return "var(--color-text-muted)";
    default:
      return "var(--color-text-muted)";
  }
}

function ageLabel(updatedAt: number | null, nowMs: number): string | null {
  if (!updatedAt) return null;
  const sec = Math.max(0, Math.round((nowMs - updatedAt) / 1000));
  if (sec < 90) return `${sec}s ago`;
  return `${Math.round(sec / 60)}m ago`;
}

export function RegimePanel() {
  const [data, setData] = useState<RegimeLive | null>(null);
  const [error, setError] = useState(false);
  const [nowMs, setNowMs] = useState(Date.now());

  useEffect(() => {
    let cancelled = false;
    const refresh = async () => {
      try {
        const r = await fetch(`${API_BASE}/regime/live`);
        if (!cancelled && r.ok) {
          setData(await r.json());
          setError(false);
        } else if (!cancelled && !r.ok) {
          setError(true);
        }
      } catch {
        if (!cancelled) setError(true);
      }
      if (!cancelled) setNowMs(Date.now());
    };
    void refresh();
    const id = window.setInterval(refresh, 15000);
    return () => {
      cancelled = true;
      window.clearInterval(id);
    };
  }, []);

  if (!data) {
    return (
      <section className="sidebar-section regime-panel">
        <h3 className="sidebar-section-title">Market regime</h3>
        <p className="muted small">{error ? "sidecar unreachable" : "loading…"}</p>
      </section>
    );
  }

  return (
    <section className="sidebar-section regime-panel">
      <h3 className="sidebar-section-title">
        Market regime
        <span className="muted small" style={{ fontWeight: 400, marginLeft: 6 }}>
          live measurements · {data.bar_symbol}
        </span>
      </h3>
      <div className="regime-body">
        {TILE_ORDER.map(({ key, title }) => {
          const t = data.tiles[key];
          if (!t) return null;
          const color = tileColor(key, t);
          const age = ageLabel(t.updated_at, nowMs);
          return (
            <div
              key={key}
              style={{
                borderLeft: `3px solid ${color}`,
                padding: "4px 0 4px 8px",
                marginBottom: 6,
              }}
            >
              <div style={{ display: "flex", justifyContent: "space-between", alignItems: "baseline" }}>
                <span className="regime-grid-label">{title}</span>
                <span style={{ color, fontSize: 11, fontWeight: 600 }}>{t.label}</span>
              </div>
              <div className="small" style={{ marginTop: 2 }}>{t.reading}</div>
              <div className="muted small" style={{ marginTop: 2 }}>
                {t.effect_on_bot}
                {age && <span style={{ marginLeft: 6, opacity: 0.7 }}>· {age}</span>}
              </div>
            </div>
          );
        })}
      </div>
    </section>
  );
}

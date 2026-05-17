import { useSidecar } from "../context/WebSocketContext";

// Composite HFT signal panel — vol + stuffing + L2 depth + composite direction.
// Event-driven: fires on threshold crossings (vol_regime flip, stuffing
// severity change, book direction flip) OR poll cadence.

const COMPOSITE_COLOR: Record<string, string> = {
  BULLISH: "var(--color-green)",
  BEARISH: "var(--color-red)",
  VOLATILE: "var(--color-amber)",
  FADE: "var(--color-purple)",
  NEUTRAL: "var(--color-text-muted)",
};

const REGIME_COLOR: Record<string, string> = {
  EXPANSION: "var(--color-red)",
  CONTRACTION: "var(--color-blue)",
  NORMAL: "var(--color-text-muted)",
};

const SEVERITY_COLOR: Record<string, string> = {
  STUFFING: "var(--color-red)",
  ELEVATED: "var(--color-amber)",
  NORMAL: "var(--color-text-muted)",
};

const BOOK_COLOR: Record<string, string> = {
  BID_HEAVY: "var(--color-green)",
  ASK_HEAVY: "var(--color-red)",
  BALANCED: "var(--color-text-muted)",
};

export function HFTPanel() {
  const { lastHFT } = useSidecar();
  if (!lastHFT) {
    return (
      <section className="sidebar-section">
        <h3 className="sidebar-section-title">HFT signals (4Hz)</h3>
        <p className="muted small">collecting depth/vol/stuffing...</p>
      </section>
    );
  }

  const h = lastHFT;
  const compColor = COMPOSITE_COLOR[h.composite_direction] ?? "var(--color-text-muted)";

  return (
    <section className="sidebar-section hft-panel">
      <h3 className="sidebar-section-title">
        HFT signals · {h.fired_on === "poll" ? "poll" : "flip!"}
      </h3>

      <div className="hft-composite" style={{ borderLeft: `3px solid ${compColor}` }}>
        <div className="hft-composite-dir" style={{ color: compColor }}>{h.composite_direction}</div>
        <div className="muted small">fired on: {h.fired_on}</div>
      </div>

      <div className="hft-subsystems">
        <div className="hft-sub">
          <div className="hft-sub-label">Realized Vol</div>
          <div className="hft-sub-value" style={{ color: REGIME_COLOR[h.vol_regime] ?? "var(--color-text-muted)" }}>
            {h.vol_regime}
          </div>
          <div className="muted small">1m {h.short_vol_pts.toFixed(2)}pt · 5m {h.long_vol_pts.toFixed(2)}pt · ratio {h.vol_ratio.toFixed(2)}</div>
        </div>

        <div className="hft-sub">
          <div className="hft-sub-label">Quote Stuffing</div>
          <div className="hft-sub-value" style={{ color: SEVERITY_COLOR[h.stuffing_severity] ?? "var(--color-text-muted)" }}>
            {h.stuffing_severity}
          </div>
          <div className="muted small">C/T {h.cancel_to_trade_ratio.toFixed(1)}x · {h.quote_intensity_per_sec.toFixed(0)}/s</div>
        </div>

        <div className="hft-sub">
          <div className="hft-sub-label">L2 Depth</div>
          <div className="hft-sub-value" style={{ color: BOOK_COLOR[h.book_direction] ?? "var(--color-text-muted)" }}>
            {h.book_direction.replace("_", " ")}
          </div>
          <div className="muted small">imb {h.book_imbalance.toFixed(2)} · top5 {h.book_imbalance_5lvl.toFixed(2)} · spread ${h.book_spread?.toFixed(2) ?? "—"}</div>
        </div>
      </div>
    </section>
  );
}

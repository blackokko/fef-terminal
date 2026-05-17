import { useEffect, useRef } from "react";
import { useSidecar } from "../context/WebSocketContext";
import { flashTitle, playBearishChime, playBullishChime } from "../lib/audioAlert";

const CONFIDENCE_COLOR: Record<string, string> = {
  HIGH: "var(--color-green)",
  MEDIUM: "var(--color-amber)",
  LOW: "var(--color-text-muted)",
  NONE: "var(--color-text-muted)",
};

export function FEFVerdictPanel() {
  const { lastIter } = useSidecar();
  const lastAlertedIter = useRef<number | null>(null);

  // IMPROVEMENTS #17: chime + title flash on HIGH-confidence fires
  useEffect(() => {
    if (!lastIter) return;
    if (lastIter.iter_num === lastAlertedIter.current) return;
    const v = lastIter.verdict;
    if (v.confidence === "HIGH" && (v.direction === "LONG_CALLS" || v.direction === "LONG_PUTS")) {
      lastAlertedIter.current = lastIter.iter_num;
      if (v.direction === "LONG_CALLS") {
        playBullishChime();
      } else {
        playBearishChime();
      }
      flashTitle(`🔔 ${v.direction} HIGH @ $${lastIter.spx_price.toFixed(2)}`);
    }
  }, [lastIter]);

  if (!lastIter) {
    return (
      <section className="sidebar-section">
        <h3 className="sidebar-section-title">FEF Verdict</h3>
        <p className="muted">waiting for first iter…</p>
      </section>
    );
  }

  const v = lastIter.verdict;

  return (
    <section className="sidebar-section">
      <h3 className="sidebar-section-title">
        FEF Verdict · iter #{lastIter.iter_num}
      </h3>

      <div className="verdict-block">
        <div
          className={`verdict-direction verdict-${v.direction.toLowerCase()}`}
        >
          {v.direction}
        </div>
        <div
          className="verdict-confidence"
          style={{ color: CONFIDENCE_COLOR[v.confidence] }}
        >
          confidence: {v.confidence}
        </div>
        <div className="verdict-reasoning">{v.reasoning}</div>

        <div className="verdict-metrics">
          <Metric label="ΔPrice" value={v.delta_price} format="signed" suffix="pts" />
          <Metric label="ΔDrift" value={v.delta_drift_m} format="signed" suffix="M" />
          <Metric label="FE" value={v.fe_ratio} format="fixed" />
        </div>

        <div className="verdict-flags">
          {v.aligned && <Flag label="aligned" color="var(--color-green)" />}
          {v.absorption && <Flag label="absorption" color="var(--color-amber)" />}
          {v.wall_proximity_skip && (
            <Flag label="wall-skip" color="var(--color-red)" />
          )}
          {v.wall_collapse_trigger && (
            <Flag label="collapse" color="var(--color-purple)" />
          )}
        </div>

        {v.secondary_scores && Object.keys(v.secondary_scores).length > 0 && (
          <details className="verdict-scores">
            <summary>6-factor scorer</summary>
            <ul>
              {Object.entries(v.secondary_scores).map(([k, val]) => (
                <li key={k}>
                  <span>{k}</span>
                  <span>{val.toFixed(2)}</span>
                </li>
              ))}
            </ul>
          </details>
        )}
      </div>
    </section>
  );
}

function Metric({
  label,
  value,
  format = "fixed",
  suffix = "",
}: {
  label: string;
  value: number | null;
  format?: "signed" | "fixed";
  suffix?: string;
}) {
  if (value == null) {
    return (
      <div className="metric">
        <span className="metric-label">{label}</span>
        <span className="metric-value muted">—</span>
      </div>
    );
  }
  const sign = format === "signed" && value > 0 ? "+" : "";
  return (
    <div className="metric">
      <span className="metric-label">{label}</span>
      <span className="metric-value">
        {sign}
        {value.toFixed(2)}
        {suffix}
      </span>
    </div>
  );
}

function Flag({ label, color }: { label: string; color: string }) {
  return (
    <span className="flag" style={{ borderColor: color, color }}>
      {label}
    </span>
  );
}

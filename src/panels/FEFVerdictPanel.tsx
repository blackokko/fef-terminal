import { useEffect, useRef, useState } from "react";
import { useSidecar } from "../context/WebSocketContext";
import { flashTitle, playBearishChime, playBullishChime } from "../lib/audioAlert";
import { formatDrift } from "../lib/format";

// Verdicts panel — repurposed 2026-07-06.
//
// PRIMARY: the multi-symbol SCANNER's latest per-symbol verdicts + the last
// gate outcome (chop gate / bias veto / auto-trader block reasons). The
// scanner IS the trading path — this is what actually fires.
//
// SECONDARY: the main engine's latest advisory verdict, clearly labeled
// retired — it trades nothing and is shown only for context/attention.

const API_BASE = "http://127.0.0.1:8765";

const CONFIDENCE_COLOR: Record<string, string> = {
  HIGH: "var(--color-green)",
  MEDIUM: "var(--color-amber)",
  LOW: "var(--color-text-muted)",
  NONE: "var(--color-text-muted)",
};

const DIRECTION_COLOR: Record<string, string> = {
  LONG_CALLS: "var(--color-green)",
  LONG_PUTS: "var(--color-red)",
  WAIT: "var(--color-text-muted)",
};

interface ScannerVerdict {
  direction: string;
  confidence: string;
  reasoning: string;
  spx_price: number;
  strike_rec: number | null;
  timestamp_ms: number;
}

interface AutoTradeStatus {
  enabled: boolean;
  last_block_reason: string;
  scanner: {
    enabled_symbols: string[];
    max_per_symbol: number;
    count_by_symbol: Record<string, number>;
    last_fire_at_ms_by_symbol: Record<string, number>;
    last_verdict_by_symbol: Record<string, ScannerVerdict>;
  };
}

function age(tsMs: number, nowMs: number): string {
  const sec = Math.max(0, Math.round((nowMs - tsMs) / 1000));
  if (sec < 90) return `${sec}s`;
  if (sec < 5400) return `${Math.round(sec / 60)}m`;
  return `${Math.round(sec / 3600)}h`;
}

export function FEFVerdictPanel() {
  const { lastIter } = useSidecar();
  const lastAlertedIter = useRef<number | null>(null);
  const [status, setStatus] = useState<AutoTradeStatus | null>(null);
  const [nowMs, setNowMs] = useState(Date.now());

  useEffect(() => {
    let cancelled = false;
    const refresh = async () => {
      try {
        const r = await fetch(`${API_BASE}/auto_trade/status`);
        if (!cancelled && r.ok) setStatus(await r.json());
      } catch {}
      if (!cancelled) setNowMs(Date.now());
    };
    void refresh();
    const id = window.setInterval(refresh, 3000);
    return () => {
      cancelled = true;
      window.clearInterval(id);
    };
  }, []);

  // Chime + title flash on HIGH-confidence advisory fires (attention only —
  // the advisory engine trades nothing).
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

  const scanner = status?.scanner;
  const verdictSymbols = scanner
    ? Object.keys(scanner.last_verdict_by_symbol).sort()
    : [];

  return (
    <section className="sidebar-section">
      <h3 className="sidebar-section-title">Scanner verdicts · trading path</h3>

      {!scanner ? (
        <p className="muted small">loading…</p>
      ) : verdictSymbols.length === 0 ? (
        <p className="muted small">
          no scanner verdicts yet
          {scanner.enabled_symbols.length === 0 && " — no symbols enabled"}
        </p>
      ) : (
        <table style={{ width: "100%", fontSize: 11, fontFamily: "var(--font-mono)" }}>
          <tbody>
            {verdictSymbols.map((sym) => {
              const v = scanner.last_verdict_by_symbol[sym];
              const fires = scanner.count_by_symbol[sym] ?? 0;
              return (
                <tr key={sym} style={{ borderTop: "1px solid var(--color-border)" }}>
                  <td style={{ padding: "3px 0", fontWeight: 600, width: 42 }}>{sym}</td>
                  <td>
                    <span style={{ color: DIRECTION_COLOR[v.direction] ?? "var(--color-text)" }}>
                      {v.direction}
                    </span>{" "}
                    <span style={{ color: CONFIDENCE_COLOR[v.confidence] ?? "var(--color-text-muted)" }}>
                      {v.confidence}
                    </span>
                    {v.strike_rec != null && v.direction !== "WAIT" && (
                      <span className="muted small"> · {v.strike_rec.toFixed(0)}</span>
                    )}
                  </td>
                  <td className="muted small" style={{ textAlign: "right", width: 70 }}>
                    {fires > 0 && <span>{fires} fired · </span>}
                    {age(v.timestamp_ms, nowMs)}
                  </td>
                </tr>
              );
            })}
          </tbody>
        </table>
      )}

      {status && status.last_block_reason && (
        <div className="muted small" style={{ marginTop: 6 }}>
          <span className="regime-grid-label">Last gate outcome</span>
          <div style={{ marginTop: 2 }}>{status.last_block_reason}</div>
        </div>
      )}
      {status && !status.enabled && (
        <div className="small" style={{ marginTop: 4, color: "var(--color-amber)" }}>
          auto-trade OFF — scanner verdicts are signals only
        </div>
      )}

      <AdvisoryVerdict />
    </section>
  );
}

// SECONDARY — the main engine's advisory verdict. Retired from the trading
// path; shown for context only.
function AdvisoryVerdict() {
  const { lastIter } = useSidecar();

  return (
    <details style={{ marginTop: 10, borderTop: "1px solid var(--color-border)", paddingTop: 6 }}>
      <summary className="muted small" style={{ cursor: "pointer" }}>
        Advisory engine (retired — does not trade)
        {lastIter && <span> · iter #{lastIter.iter_num}</span>}
      </summary>
      {!lastIter ? (
        <p className="muted small">waiting for first iter…</p>
      ) : (
        <AdvisoryBody />
      )}
    </details>
  );
}

function AdvisoryBody() {
  const { lastIter } = useSidecar();
  if (!lastIter) return null;
  const v = lastIter.verdict;

  return (
    <div className="verdict-block" style={{ marginTop: 6 }}>
      <div className={`verdict-direction verdict-${v.direction.toLowerCase()}`}>
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
        <div className="metric">
          <span className="metric-label">ΔDrift</span>
          <span className="metric-value">{formatDrift(v.delta_drift_m)}</span>
        </div>
        <Metric label="FE" value={v.fe_ratio} format="fixed" />
      </div>

      <div className="verdict-flags">
        {v.aligned && <Flag label="aligned" color="var(--color-green)" />}
        {v.absorption && <Flag label="absorption" color="var(--color-amber)" />}
        {v.wall_proximity_skip && <Flag label="wall-skip" color="var(--color-red)" />}
        {v.wall_collapse_trigger && <Flag label="collapse" color="var(--color-purple)" />}
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

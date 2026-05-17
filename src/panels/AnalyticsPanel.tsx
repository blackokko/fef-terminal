import { useEffect, useState } from "react";
import { useToast } from "../components/Toast";
import { fetchJson } from "../lib/api";
import { formatDollars } from "../lib/format";

interface WinRateBucket {
  wins: number;
  losses: number;
  total: number;
  pnl: number;
  pct: number;
}

interface AnalyticsSummary {
  window_days: number;
  win_rate_by_confidence: Record<string, WinRateBucket>;
  win_rate_by_direction: Record<string, WinRateBucket>;
  win_rate_by_pattern: Record<string, WinRateBucket>;
  pnl_by_hour_et: Record<string, number>;
  pattern_frequency: Record<string, number>;
  recent_sessions: {
    date: string;
    total_iters: number;
    actionable: number;
    trades: number;
    net_pnl: number;
    wins: number;
    losses: number;
  }[];
}

export function AnalyticsPanel() {
  const toast = useToast();
  const [summary, setSummary] = useState<AnalyticsSummary | null>(null);
  const [windowDays, setWindowDays] = useState(30);

  useEffect(() => {
    void fetchJson<AnalyticsSummary>(`/analytics/summary?days=${windowDays}`, toast).then(setSummary);
    const id = window.setInterval(
      () => void fetchJson<AnalyticsSummary>(`/analytics/summary?days=${windowDays}`, toast).then(setSummary),
      30_000,
    );
    return () => window.clearInterval(id);
  }, [windowDays, toast]);

  if (!summary) {
    return (
      <section className="sidebar-section">
        <h3 className="sidebar-section-title">Analytics</h3>
        <p className="muted small">loading…</p>
      </section>
    );
  }

  return (
    <section className="sidebar-section">
      <h3 className="sidebar-section-title">
        Analytics · last {summary.window_days}d
      </h3>
      <div className="analytics-body">
        <div className="analytics-window-picker">
          {[7, 30, 90].map((d) => (
            <button
              key={d}
              className={`chart-toolbar-btn ${windowDays === d ? "active" : ""}`}
              onClick={() => setWindowDays(d)}
            >
              {d}d
            </button>
          ))}
        </div>

        <WinRateSection title="By confidence" buckets={summary.win_rate_by_confidence} />
        <WinRateSection title="By direction" buckets={summary.win_rate_by_direction} />
        <WinRateSection title="By pattern" buckets={summary.win_rate_by_pattern} />

        <SubBlock title="P/L by hour ET">
          {Object.keys(summary.pnl_by_hour_et).length === 0 ? (
            <p className="muted small">no trades yet</p>
          ) : (
            <PnlHourChart data={summary.pnl_by_hour_et} />
          )}
        </SubBlock>

        <SubBlock title="Pattern fires">
          {Object.keys(summary.pattern_frequency).length === 0 ? (
            <p className="muted small">no pattern matches</p>
          ) : (
            <ul className="freq-list">
              {Object.entries(summary.pattern_frequency)
                .sort(([, a], [, b]) => b - a)
                .map(([name, count]) => (
                  <li key={name}>
                    <span>{name}</span>
                    <span className="muted">{count}×</span>
                  </li>
                ))}
            </ul>
          )}
        </SubBlock>

        <SubBlock title="Recent sessions">
          {summary.recent_sessions.length === 0 ? (
            <p className="muted small">no sessions yet</p>
          ) : (
            <ul className="session-list">
              {summary.recent_sessions.map((s) => (
                <li key={s.date}>
                  <span>{s.date}</span>
                  <span className="muted">
                    {s.actionable}/{s.total_iters} act · {s.trades}T
                  </span>
                  <span style={{ color: s.net_pnl >= 0 ? "var(--color-green)" : "var(--color-red)" }}>
                    {formatDollars(s.net_pnl, { sign: true })}
                  </span>
                </li>
              ))}
            </ul>
          )}
        </SubBlock>
      </div>
    </section>
  );
}

function WinRateSection({ title, buckets }: { title: string; buckets: Record<string, WinRateBucket> }) {
  const keys = Object.keys(buckets);
  return (
    <SubBlock title={title}>
      {keys.length === 0 ? (
        <p className="muted small">no closed trades</p>
      ) : (
        <ul className="winrate-list">
          {keys.map((k) => {
            const b = buckets[k];
            return (
              <li key={k} className="winrate-row">
                <span className="winrate-label">{k}</span>
                <div className="winrate-bar-wrap">
                  <div className="winrate-bar" style={{ width: `${b.pct}%` }} />
                </div>
                <span className="winrate-stats muted small">
                  {b.pct}% ({b.wins}/{b.total})
                </span>
                <span
                  className="winrate-pnl"
                  style={{ color: b.pnl >= 0 ? "var(--color-green)" : "var(--color-red)" }}
                >
                  {formatDollars(b.pnl, { sign: true })}
                </span>
              </li>
            );
          })}
        </ul>
      )}
    </SubBlock>
  );
}

function PnlHourChart({ data }: { data: Record<string, number> }) {
  const entries = Object.entries(data).map(([h, v]) => ({ hour: Number(h), pnl: v }));
  const maxAbs = Math.max(1, ...entries.map((e) => Math.abs(e.pnl)));
  return (
    <ul className="pnl-hour-list">
      {entries.map(({ hour, pnl }) => {
        const widthPct = Math.abs(pnl) / maxAbs * 50;
        return (
          <li key={hour}>
            <span className="pnl-hour-label">{hour}:00</span>
            <div className="pnl-hour-bars">
              {pnl < 0 && (
                <div
                  className="pnl-hour-bar neg"
                  style={{ width: `${widthPct}%`, marginLeft: `${50 - widthPct}%` }}
                />
              )}
              {pnl >= 0 && (
                <div className="pnl-hour-bar pos" style={{ width: `${widthPct}%`, marginLeft: "50%" }} />
              )}
            </div>
            <span className="pnl-hour-val muted small">{formatDollars(pnl, { sign: true })}</span>
          </li>
        );
      })}
    </ul>
  );
}

function SubBlock({ title, children }: { title: string; children: React.ReactNode }) {
  return (
    <div className="analytics-block">
      <div className="subsection-title">{title}</div>
      {children}
    </div>
  );
}

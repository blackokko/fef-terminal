import { useSidecar } from "../context/WebSocketContext";
import { formatDollars } from "../lib/format";

// Live IBKR account scorecard — realized + unrealized P/L straight from
// the broker. Sits at the top of the Trade tab so it's the first thing
// you see during a session.

export function AccountScorecardPanel() {
  const { lastAccount } = useSidecar();

  if (!lastAccount) {
    return (
      <section className="sidebar-section scorecard-panel">
        <h3 className="sidebar-section-title">Today's P/L</h3>
        <p className="muted small">connecting to broker…</p>
      </section>
    );
  }

  const a = lastAccount;
  const daily = a.daily_pnl ?? 0;
  const isUp = daily >= 0;
  const color = isUp ? "var(--color-green)" : "var(--color-red)";

  return (
    <section className="sidebar-section scorecard-panel">
      <h3 className="sidebar-section-title">
        Today's P/L <span className="muted small">· {a.account}</span>
      </h3>

      <div className="scorecard-hero" style={{ color }}>
        {formatDollars(daily, { sign: true })}
      </div>

      <div className="scorecard-grid">
        <Stat label="Realized" value={a.realized_pnl} color sign />
        <Stat label="Unrealized" value={a.unrealized_pnl} color sign />
        <Stat label="Net Liq" value={a.net_liquidation} />
        <Stat label="Buying Power" value={a.buying_power} />
        <Stat label="Available" value={a.available_funds} />
        <Stat label="Positions" rawValue={`${a.open_positions_count} (${a.open_options_count} opt)`} />
      </div>
    </section>
  );
}

function Stat({
  label,
  value,
  rawValue,
  color = false,
  sign = false,
}: {
  label: string;
  value?: number | null;
  rawValue?: string;
  color?: boolean;
  sign?: boolean;
}) {
  let display = rawValue ?? formatDollars(value, { sign });
  let style: React.CSSProperties | undefined;
  if (color && value != null) {
    style = { color: value >= 0 ? "var(--color-green)" : "var(--color-red)" };
  }
  return (
    <div className="scorecard-stat">
      <span className="scorecard-stat-label">{label}</span>
      <span className="scorecard-stat-value" style={style}>{display}</span>
    </div>
  );
}

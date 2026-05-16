import { useSidecar } from "../context/WebSocketContext";
import type { WallRow } from "../lib/events";

export function QuantDataPanel() {
  const { lastIter } = useSidecar();

  if (!lastIter) {
    return (
      <section className="sidebar-section">
        <h3 className="sidebar-section-title">QuantData</h3>
        <p className="muted">no snapshot yet</p>
      </section>
    );
  }

  const s = lastIter.snapshot;

  const ageMs = Date.now() - lastIter.timestamp_ms;
  const ageStr =
    ageMs < 60_000 ? `${Math.round(ageMs / 1000)}s` : `${Math.round(ageMs / 60_000)}m`;

  return (
    <section className="sidebar-section">
      <h3 className="sidebar-section-title">
        QuantData · {ageStr} ago
      </h3>

      <SubSection title="Drift">
        <div>
          {s.drift.bias} (cum net{" "}
          {s.drift.cum_net_m > 0 ? "+" : ""}
          {s.drift.cum_net_m.toFixed(2)}M)
        </div>
      </SubSection>

      <SubSection title="Max Pain">
        <div>
          NTM <strong>${s.ntm_max_pain.toFixed(0)}</strong>
          <span className="muted small">
            {" "}
            (raw chain ${s.raw_max_pain.toFixed(0)})
          </span>
        </div>
      </SubSection>

      <SubSection title="Top GEX walls">
        <WallList walls={s.top_gex} />
      </SubSection>

      <SubSection title="Top DEX walls">
        <WallList walls={s.top_dex} />
      </SubSection>

      {s.aggression && (
        <SubSection title="Aggression">
          <div className="aggression-row">
            <span>call</span>
            <span style={{ color: "var(--color-green)" }}>
              {s.aggression.call_pct.toFixed(1)}%
            </span>
          </div>
          <div className="aggression-row">
            <span>put</span>
            <span style={{ color: "var(--color-red)" }}>
              {s.aggression.put_pct.toFixed(1)}%
            </span>
          </div>
        </SubSection>
      )}
    </section>
  );
}

function SubSection({ title, children }: { title: string; children: React.ReactNode }) {
  return (
    <div className="subsection">
      <div className="subsection-title">{title}</div>
      <div className="subsection-body">{children}</div>
    </div>
  );
}

function WallList({ walls }: { walls: WallRow[] }) {
  if (!walls || walls.length === 0) {
    return <p className="muted small">—</p>;
  }
  return (
    <ul className="wall-list">
      {walls.slice(0, 5).map((w) => (
        <li key={`${w.strike}-${w.side}`} className={`wall-${w.side.toLowerCase()}`}>
          <span className="wall-strike">${w.strike.toFixed(0)}</span>
          <span className="wall-net">
            {w.net_m > 0 ? "+" : ""}
            {w.net_m >= 1000 || w.net_m <= -1000
              ? `${(w.net_m / 1000).toFixed(1)}B`
              : `${w.net_m.toFixed(1)}M`}
          </span>
        </li>
      ))}
    </ul>
  );
}

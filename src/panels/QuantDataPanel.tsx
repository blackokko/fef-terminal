import { useSidecar } from "../context/WebSocketContext";
import type { WallRow } from "../lib/events";

// Prefer SnapshotEvent (fires every ~15s) over IterEvent's snapshot (~2min)
// so walls/drift/MP refresh ahead of the FEF cadence.
export function QuantDataPanel() {
  const { lastSnapshot, lastIter } = useSidecar();

  // SnapshotEvent has the same shape as IterEvent.snapshot but without verdict.
  const source = lastSnapshot
    ? {
        ts: lastSnapshot.timestamp_ms,
        gex: lastSnapshot.top_gex,
        dex: lastSnapshot.top_dex,
        drift: lastSnapshot.drift,
        ntm_max_pain: lastSnapshot.ntm_max_pain,
        raw_max_pain: lastSnapshot.raw_max_pain,
        aggression: lastSnapshot.aggression,
      }
    : lastIter
      ? {
          ts: lastIter.timestamp_ms,
          gex: lastIter.snapshot.top_gex,
          dex: lastIter.snapshot.top_dex,
          drift: lastIter.snapshot.drift,
          ntm_max_pain: lastIter.snapshot.ntm_max_pain,
          raw_max_pain: lastIter.snapshot.raw_max_pain,
          aggression: lastIter.snapshot.aggression,
        }
      : null;

  if (!source) {
    return (
      <section className="sidebar-section">
        <h3 className="sidebar-section-title">QuantData</h3>
        <p className="muted">no snapshot yet</p>
      </section>
    );
  }

  const ageMs = Date.now() - source.ts;
  const ageStr =
    ageMs < 60_000 ? `${Math.round(ageMs / 1000)}s` : `${Math.round(ageMs / 60_000)}m`;

  return (
    <section className="sidebar-section">
      <h3 className="sidebar-section-title">QuantData · {ageStr} ago</h3>

      <SubSection title="Drift">
        <div>
          {source.drift.bias} (cum net{" "}
          {source.drift.cum_net_m > 0 ? "+" : ""}
          {source.drift.cum_net_m.toFixed(2)}M)
        </div>
      </SubSection>

      <SubSection title="Max Pain">
        <div>
          NTM <strong>${source.ntm_max_pain.toFixed(0)}</strong>
          <span className="muted small">
            {" "}
            (raw chain ${source.raw_max_pain.toFixed(0)})
          </span>
        </div>
      </SubSection>

      <SubSection title="Top GEX walls">
        <WallList walls={source.gex} />
      </SubSection>

      <SubSection title="Top DEX walls">
        <WallList walls={source.dex} />
      </SubSection>

      {source.aggression && (
        <SubSection title="Aggression">
          <div className="aggression-row">
            <span>call</span>
            <span style={{ color: "var(--color-green)" }}>
              {source.aggression.call_pct.toFixed(1)}%
            </span>
          </div>
          <div className="aggression-row">
            <span>put</span>
            <span style={{ color: "var(--color-red)" }}>
              {source.aggression.put_pct.toFixed(1)}%
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

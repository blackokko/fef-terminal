import { useSidecar } from "../context/WebSocketContext";

// Sidebar — minimal v1 layout. Tasks #10 (QuantData panel), #11 (FEF
// verdict panel), #12 (order ticket) will populate the sections.

export function Sidebar() {
  const { lastIter } = useSidecar();

  return (
    <aside className="sidebar">
      <Section title="FEF Verdict">
        {lastIter ? (
          <div className="verdict-block">
            <div className={`verdict-direction verdict-${lastIter.verdict.direction.toLowerCase()}`}>
              {lastIter.verdict.direction}
            </div>
            <div className="verdict-confidence">
              confidence: {lastIter.verdict.confidence}
            </div>
            <div className="verdict-reasoning">{lastIter.verdict.reasoning}</div>
          </div>
        ) : (
          <p className="muted">waiting for first iter…</p>
        )}
      </Section>

      <Section title="Walls (top GEX)">
        {lastIter ? (
          <ul className="wall-list">
            {lastIter.snapshot.top_gex.slice(0, 5).map((w) => (
              <li key={`gex-${w.strike}`} className={`wall-${w.side.toLowerCase()}`}>
                <span className="wall-strike">${w.strike.toFixed(0)}</span>
                <span className="wall-net">
                  {w.net_m > 0 ? "+" : ""}
                  {w.net_m.toFixed(1)}M
                </span>
              </li>
            ))}
          </ul>
        ) : (
          <p className="muted">—</p>
        )}
      </Section>

      <Section title="Drift">
        {lastIter ? (
          <div>
            <div>
              {lastIter.snapshot.drift.bias} (cum net{" "}
              {lastIter.snapshot.drift.cum_net_m > 0 ? "+" : ""}
              {lastIter.snapshot.drift.cum_net_m.toFixed(2)}M)
            </div>
          </div>
        ) : (
          <p className="muted">—</p>
        )}
      </Section>

      <Section title="Max Pain">
        {lastIter ? (
          <div>
            <div>NTM: ${lastIter.snapshot.ntm_max_pain.toFixed(0)}</div>
            <div className="muted small">
              raw chain: ${lastIter.snapshot.raw_max_pain.toFixed(0)}
            </div>
          </div>
        ) : (
          <p className="muted">—</p>
        )}
      </Section>

      <Section title="Order Ticket">
        {lastIter && lastIter.verdict.strike_rec ? (
          <div className="ticket-block">
            <div>
              {lastIter.verdict.direction === "LONG_CALLS" ? "BUY" : "BUY"} ×15{" "}
              {lastIter.verdict.strike_rec.toFixed(0)}
              {lastIter.verdict.direction === "LONG_CALLS" ? "C" : "P"}
            </div>
            <div className="muted small">
              T1 {lastIter.verdict.t1?.toFixed(0)} / T2{" "}
              {lastIter.verdict.t2?.toFixed(0)} / T3{" "}
              {lastIter.verdict.t3?.toFixed(0)} — stop{" "}
              {lastIter.verdict.stop?.toFixed(0)}
            </div>
          </div>
        ) : (
          <p className="muted">no actionable signal</p>
        )}
      </Section>
    </aside>
  );
}

function Section({ title, children }: { title: string; children: React.ReactNode }) {
  return (
    <section className="sidebar-section">
      <h3 className="sidebar-section-title">{title}</h3>
      {children}
    </section>
  );
}

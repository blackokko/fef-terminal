import { useEffect, useState } from "react";

const API_BASE = "http://127.0.0.1:8765";

interface SplitRow {
  session_date: string;
  dedup_actionable: number;
  dedup_wins: number;
  dedup_win_rate_pct: number;
  dedup_expectancy_r: number;
  paper_realized_dollars: number;
  live_realized_dollars: number;
}

interface EngineChange {
  applied_at: string;
  name: string;
  category: string;
}

/**
 * Two stacked graphs:
 *   1. Paper — dedup expectancy R (signal quality) + paper realized $ (real outcomes)
 *   2. Live  — same metrics, scoped to non-paper (live) IBKR accounts
 *
 * Dedup expectancy comes from DailyReview.dedup_expectancy_r — collapses
 * stacked entries to one signal per setup, then walks each through 1-min
 * SPX bars to simulate outcome. Realized dollars come from IBKRExecution
 * filtered by account-ID prefix (DU* = paper, else = live).
 */
export function ModelEvolutionPanel() {
  const [rows, setRows] = useState<SplitRow[]>([]);
  const [changes, setChanges] = useState<EngineChange[]>([]);

  useEffect(() => {
    const refresh = async () => {
      try {
        const r1 = await fetch(`${API_BASE}/history/reviews_split?limit=60`);
        if (r1.ok) {
          const data: SplitRow[] = await r1.json();
          // Backend returns newest first; chart reads left-to-right oldest → newest
          setRows(data.slice().reverse());
        }
        const r2 = await fetch(`${API_BASE}/engine/changes?limit=50`);
        if (r2.ok) setChanges(await r2.json());
      } catch {}
    };
    void refresh();
    const id = window.setInterval(refresh, 60000);
    return () => window.clearInterval(id);
  }, []);

  if (rows.length === 0) {
    return (
      <section className="sidebar-section">
        <h3 className="sidebar-section-title">Model evolution</h3>
        <p className="muted small">No DailyReview rows yet. Run /admin/daily_review or wait for nightly compute.</p>
      </section>
    );
  }

  return (
    <section className="sidebar-section">
      <h3 className="sidebar-section-title">Model evolution</h3>
      <div className="muted small" style={{ marginBottom: 8 }}>
        {rows.length} sessions · {changes.length} engine changes ·
        Dedup expectancy (R, left axis) + realized $ (right axis)
      </div>

      <SessionChart
        title="Paper"
        rows={rows}
        getRealized={r => r.paper_realized_dollars}
        changes={changes}
      />
      <SessionChart
        title="Live"
        rows={rows}
        getRealized={r => r.live_realized_dollars}
        changes={changes}
      />
    </section>
  );
}

interface ChartProps {
  title: string;
  rows: SplitRow[];
  getRealized: (r: SplitRow) => number;
  changes: EngineChange[];
}

function SessionChart({ title, rows, getRealized, changes }: ChartProps) {
  const W = 320, H = 130;
  const PAD = 24;

  const realized = rows.map(getRealized);
  const totalAbs = realized.reduce((s, v) => s + Math.abs(v), 0);
  const hasRealized = totalAbs > 0.01;

  // Left axis: dedup expectancy R
  const exps = rows.map(r => r.dedup_expectancy_r);
  const minE = Math.min(-0.3, ...exps);
  const maxE = Math.max(0.3, ...exps);
  const yScaleR = (e: number) =>
    H - PAD - ((e - minE) / (maxE - minE || 1)) * (H - 2 * PAD);
  const xScale = (i: number) =>
    PAD + (i / Math.max(1, rows.length - 1)) * (W - 2 * PAD);

  // Right axis: realized $
  const minD = Math.min(0, ...realized);
  const maxD = Math.max(0, ...realized);
  const dollarSpan = maxD - minD || 1;
  const yScaleD = (d: number) =>
    H - PAD - ((d - minD) / dollarSpan) * (H - 2 * PAD);

  const yZeroR = yScaleR(0);

  // Dedup expectancy line (blue)
  const expPts = rows.map((r, i) => `${xScale(i)},${yScaleR(r.dedup_expectancy_r)}`).join(" ");
  // Realized $ line (green/red shaded)
  const realizedPts = rows.map((r, i) => `${xScale(i)},${yScaleD(getRealized(r))}`).join(" ");

  // Engine change markers
  const changeMarkers: { x: number }[] = [];
  for (const c of changes) {
    const d = c.applied_at.slice(0, 10);
    let idx = -1;
    for (let i = 0; i < rows.length; i++) {
      if (rows[i].session_date >= d) { idx = i; break; }
    }
    if (idx >= 0) changeMarkers.push({ x: xScale(idx) });
  }

  const lastReal = realized[realized.length - 1] ?? 0;
  const lastExp = exps[exps.length - 1] ?? 0;

  return (
    <div style={{ marginBottom: 14 }}>
      <div style={{
        display: "flex", justifyContent: "space-between",
        fontFamily: "var(--font-mono)", fontSize: 11, marginBottom: 4,
      }}>
        <strong>{title}</strong>
        <span>
          last:
          <span style={{ color: lastExp >= 0 ? "var(--color-green)" : "var(--color-red)", marginLeft: 4 }}>
            {lastExp >= 0 ? "+" : ""}{lastExp.toFixed(3)}R
          </span>
          {hasRealized && (
            <>
              <span className="muted" style={{ margin: "0 4px" }}>·</span>
              <span style={{ color: lastReal >= 0 ? "var(--color-green)" : "var(--color-red)" }}>
                {lastReal >= 0 ? "+" : ""}${lastReal.toFixed(0)}
              </span>
            </>
          )}
        </span>
      </div>

      {!hasRealized && (
        <div className="muted small" style={{ fontStyle: "italic", marginBottom: 4 }}>
          No {title.toLowerCase()} fills yet — only dedup expectancy plotted.
        </div>
      )}

      <svg width={W} height={H} style={{ background: "var(--color-bg-elevated)", borderRadius: 3 }}>
        {/* Zero lines */}
        <line x1={PAD} y1={yZeroR} x2={W - PAD} y2={yZeroR}
              stroke="var(--color-text-muted)" strokeDasharray="2,2" strokeWidth={1} />

        {/* Realized $ — only if there's data */}
        {hasRealized && (
          <>
            <polyline points={realizedPts} fill="none"
                      stroke={lastReal >= 0 ? "var(--color-green)" : "var(--color-red)"}
                      strokeWidth={2} strokeOpacity={0.85} />
            {rows.map((r, i) => {
              const v = getRealized(r);
              if (Math.abs(v) < 0.01) return null;
              return (
                <circle key={`r-${i}`} cx={xScale(i)} cy={yScaleD(v)} r={2.5}
                        fill={v >= 0 ? "var(--color-green)" : "var(--color-red)"}>
                  <title>{`${r.session_date}: realized $${v.toFixed(0)}`}</title>
                </circle>
              );
            })}
          </>
        )}

        {/* Dedup expectancy — always plotted (blue) */}
        <polyline points={expPts} fill="none" stroke="var(--color-blue)" strokeWidth={1.5} strokeOpacity={0.85} />
        {rows.map((r, i) => (
          <circle key={`e-${i}`} cx={xScale(i)} cy={yScaleR(r.dedup_expectancy_r)} r={2}
                  fill="var(--color-blue)">
            <title>{`${r.session_date}: dedup ${r.dedup_expectancy_r.toFixed(3)}R, WR ${r.dedup_win_rate_pct.toFixed(0)}% (${r.dedup_wins}/${r.dedup_actionable})`}</title>
          </circle>
        ))}

        {/* Engine change markers */}
        {changeMarkers.map((c, i) => (
          <line key={`c-${i}`} x1={c.x} y1={PAD} x2={c.x} y2={H - PAD}
                stroke="var(--color-amber)" strokeWidth={0.7} strokeDasharray="2,3" />
        ))}

        {/* Axis labels */}
        <text x={4} y={PAD + 4} fontSize="9" fill="var(--color-blue)" opacity="0.7">R</text>
        {hasRealized && (
          <text x={W - 4} y={PAD + 4} fontSize="9" fill="var(--color-text-muted)" textAnchor="end" opacity="0.7">$</text>
        )}
      </svg>
    </div>
  );
}

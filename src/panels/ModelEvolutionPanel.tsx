import { useEffect, useState } from "react";

const API_BASE = "http://127.0.0.1:8765";

interface Review {
  session_date: string;
  expectancy_r: number;
  win_rate_pct: number;
  actionable_signals: number;
  actual_trades_taken: number;
  actual_total_R: number;
}

interface EngineChange {
  applied_at: string;
  name: string;
  category: string;
}

export function ModelEvolutionPanel() {
  const [reviews, setReviews] = useState<Review[]>([]);
  const [changes, setChanges] = useState<EngineChange[]>([]);

  useEffect(() => {
    const refresh = async () => {
      try {
        const r1 = await fetch(`${API_BASE}/history/reviews?limit=60`);
        if (r1.ok) {
          const data: Review[] = await r1.json();
          // backend returns desc; we want asc for the chart
          setReviews(data.slice().reverse());
        }
        const r2 = await fetch(`${API_BASE}/engine/changes?limit=50`);
        if (r2.ok) setChanges(await r2.json());
      } catch {}
    };
    void refresh();
    const id = window.setInterval(refresh, 60000);
    return () => window.clearInterval(id);
  }, []);

  if (reviews.length === 0) {
    return (
      <section className="sidebar-section">
        <h3 className="sidebar-section-title">Model evolution</h3>
        <p className="muted small">No DailyReview rows yet. Run /admin/daily_review or wait for nightly compute.</p>
      </section>
    );
  }

  const W = 320, H = 120;
  const PAD = 20;
  const exps = reviews.map(r => r.expectancy_r);
  const minE = Math.min(-0.5, ...exps);
  const maxE = Math.max(0.5, ...exps);
  const yScale = (e: number) => H - PAD - ((e - minE) / (maxE - minE)) * (H - 2 * PAD);
  const xScale = (i: number) => PAD + (i / Math.max(1, reviews.length - 1)) * (W - 2 * PAD);
  const yZero = yScale(0);

  const pts = reviews.map((r, i) => `${xScale(i)},${yScale(r.expectancy_r)}`).join(" ");
  const area = `${PAD},${yZero} ${pts} ${xScale(reviews.length - 1)},${yZero}`;

  // Map engine change dates to chart x-positions (only if there's a review for that date or before/after)
  const changeMarkers: { x: number; name: string }[] = [];
  for (const c of changes) {
    const dateStr = c.applied_at.slice(0, 10);
    // find nearest review index
    let idx = -1;
    for (let i = 0; i < reviews.length; i++) {
      if (reviews[i].session_date >= dateStr) { idx = i; break; }
    }
    if (idx >= 0) changeMarkers.push({ x: xScale(idx), name: c.name });
  }

  return (
    <section className="sidebar-section">
      <h3 className="sidebar-section-title">Model evolution</h3>

      <div style={{ fontFamily: "var(--font-mono)", fontSize: 11, marginBottom: 6 }}>
        Last expectancy: <strong style={{ color: exps[exps.length - 1] >= 0 ? "var(--color-green)" : "var(--color-red)" }}>
          {exps[exps.length - 1].toFixed(3)}R
        </strong> · sessions: {reviews.length} · engine changes: {changes.length}
      </div>

      <svg width={W} height={H} style={{ background: "var(--color-bg-elevated)", borderRadius: 3 }}>
        <line x1={PAD} y1={yZero} x2={W - PAD} y2={yZero}
              stroke="var(--color-text-muted)" strokeDasharray="2,2" strokeWidth={1} />
        <polygon points={area} fill="var(--color-blue)" fillOpacity="0.2" />
        <polyline points={pts} fill="none" stroke="var(--color-blue)" strokeWidth={1.5} />
        {reviews.map((r, i) => (
          <circle key={i} cx={xScale(i)} cy={yScale(r.expectancy_r)} r={2}
                  fill={r.expectancy_r >= 0 ? "var(--color-green)" : "var(--color-red)"}>
            <title>{`${r.session_date}: ${r.expectancy_r.toFixed(3)}R, ${r.win_rate_pct}% win, ${r.actionable_signals} signals`}</title>
          </circle>
        ))}
        {changeMarkers.map((c, i) => (
          <line key={i} x1={c.x} y1={PAD} x2={c.x} y2={H - PAD}
                stroke="var(--color-amber)" strokeWidth={0.7} strokeDasharray="2,3">
            <title>{c.name}</title>
          </line>
        ))}
      </svg>

      <div className="muted small" style={{ marginTop: 4 }}>
        Hover dots for daily stats. Amber lines = engine changes.
      </div>
    </section>
  );
}

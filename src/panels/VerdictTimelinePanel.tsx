import { useEffect, useState } from "react";

const API_BASE = "http://127.0.0.1:8765";

interface Row {
  id: number;
  iter_num: number;
  timestamp: string;
  session_date: string;
  direction: string;
  confidence: string;
  spx_price: number;
  reasoning: string;
  extras: Record<string, any> | null;
}

const DIRECTION_COLOR: Record<string, string> = {
  LONG_CALLS: "var(--color-green)",
  LONG_PUTS: "var(--color-red)",
  WAIT: "var(--color-text-muted)",
};

const CONF_COLOR: Record<string, string> = {
  HIGH: "var(--color-green)",
  MEDIUM: "var(--color-amber)",
  LOW: "var(--color-text-muted)",
  NONE: "var(--color-text-muted)",
};

const fmtTime = (iso: string): string => {
  const date = new Date(iso + (iso.endsWith("Z") ? "" : "Z"));
  return date.toLocaleTimeString("en-US", {
    timeZone: "America/Chicago",
    hour12: false,
    hour: "2-digit", minute: "2-digit", second: "2-digit",
  });
};

export function VerdictTimelinePanel() {
  const [rows, setRows] = useState<Row[]>([]);
  const [filterDir, setFilterDir] = useState<string>("ALL");
  const [filterConf, setFilterConf] = useState<string>("ALL");
  const [hideWait, setHideWait] = useState(true);
  const [filterTrigger, setFilterTrigger] = useState<string>("CONFLUENCE_OR_ALIGNED");

  const triggerOf = (r: Row): string => {
    const ex = r.extras || {};
    const aligned = !!ex.aligned;
    const wall = !!ex.wall_collapse_trigger;
    if (wall && aligned) return "confluence";
    if (aligned) return "aligned_only";
    if (wall) return "wall_only";
    return "other";
  };

  const refresh = async () => {
    try {
      const r = await fetch(`${API_BASE}/history/signals?limit=200`);
      if (r.ok) setRows(await r.json());
    } catch {}
  };

  useEffect(() => {
    void refresh();
    const id = window.setInterval(refresh, 30000);
    return () => window.clearInterval(id);
  }, []);

  const today = new Date().toISOString().slice(0, 10);
  let filtered = rows.filter(r => r.session_date === today);
  if (filterDir !== "ALL") filtered = filtered.filter(r => r.direction === filterDir);
  if (filterConf !== "ALL") filtered = filtered.filter(r => r.confidence === filterConf);
  if (hideWait) filtered = filtered.filter(r => r.direction !== "WAIT");
  if (filterTrigger === "CONFLUENCE_ONLY") {
    filtered = filtered.filter(r => triggerOf(r) === "confluence");
  } else if (filterTrigger === "CONFLUENCE_OR_ALIGNED") {
    filtered = filtered.filter(r => {
      const t = triggerOf(r);
      return t === "confluence" || t === "aligned_only" || r.direction === "WAIT";
    });
  }
  filtered = filtered.slice(0, 80);

  return (
    <section className="sidebar-section">
      <h3 className="sidebar-section-title">Verdict timeline · today</h3>

      <div className="muted small" style={{ marginBottom: 6, display: "flex", gap: 6, flexWrap: "wrap" }}>
        <select value={filterDir} onChange={e => setFilterDir(e.target.value)} className="settings-input">
          <option value="ALL">All directions</option>
          <option value="LONG_CALLS">LONG_CALLS</option>
          <option value="LONG_PUTS">LONG_PUTS</option>
          <option value="WAIT">WAIT</option>
        </select>
        <select value={filterConf} onChange={e => setFilterConf(e.target.value)} className="settings-input">
          <option value="ALL">All confidence</option>
          <option value="HIGH">HIGH</option>
          <option value="MEDIUM">MEDIUM</option>
          <option value="LOW">LOW</option>
        </select>
        <label style={{ display: "flex", alignItems: "center", gap: 4 }}>
          <input type="checkbox" checked={hideWait} onChange={e => setHideWait(e.target.checked)} />
          Hide WAIT
        </label>
        <select value={filterTrigger} onChange={e => setFilterTrigger(e.target.value)} className="settings-input"
                title="Filter by trigger type. Default hides wall-only and other non-trigger verdicts to reduce noise.">
          <option value="CONFLUENCE_OR_ALIGNED">Confluence + Aligned (default)</option>
          <option value="CONFLUENCE_ONLY">Confluence only</option>
          <option value="ALL">All trigger types</option>
        </select>
      </div>

      {filtered.length === 0 ? (
        <p className="muted small">No matching iters today.</p>
      ) : (
        <table style={{ width: "100%", fontSize: 11, fontFamily: "var(--font-mono)" }}>
          <thead>
            <tr style={{ color: "var(--color-text-muted)", textAlign: "left" }}>
              <th>Time</th><th>#</th><th>SPX</th><th>Direction</th><th>Conf</th><th>Trigger</th><th>Strike</th>
            </tr>
          </thead>
          <tbody>
            {filtered.map(r => {
              const ex = r.extras || {};
              return (
                <tr key={r.id} style={{ borderTop: "1px solid var(--color-border)" }}>
                  <td>{fmtTime(r.timestamp)}</td>
                  <td className="muted">#{r.iter_num}</td>
                  <td>{r.spx_price.toFixed(2)}</td>
                  <td style={{ color: DIRECTION_COLOR[r.direction], fontWeight: 600 }}>
                    {r.direction}
                  </td>
                  <td style={{ color: CONF_COLOR[r.confidence] }}>{r.confidence}</td>
                  <td className="muted small" style={{
                    color: triggerOf(r) === "confluence" ? "var(--color-green)" : undefined,
                  }}>{triggerOf(r)}</td>
                  <td className="muted">{ex.strike_rec ?? "—"}</td>
                </tr>
              );
            })}
          </tbody>
        </table>
      )}
    </section>
  );
}

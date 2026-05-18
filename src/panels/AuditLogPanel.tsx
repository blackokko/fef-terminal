import { useEffect, useState } from "react";

const API_BASE = "http://127.0.0.1:8765";

interface AuditEntry {
  id: number;
  timestamp: string;
  category: string;
  actor: string;
  summary: string;
  details: Record<string, unknown>;
}

const CATEGORY_COLOR: Record<string, string> = {
  order: "var(--color-blue)",
  kill_switch: "var(--color-red)",
  config: "var(--color-amber)",
  calendar: "var(--color-purple)",
  symbol_switch: "var(--color-green)",
  auto_walk_forward: "var(--color-text-muted)",
};

export function AuditLogPanel() {
  const [entries, setEntries] = useState<AuditEntry[]>([]);
  const [filterCat, setFilterCat] = useState<string>("");

  useEffect(() => {
    const refresh = async () => {
      try {
        const url = filterCat
          ? `${API_BASE}/audit_log?category=${filterCat}&limit=100`
          : `${API_BASE}/audit_log?limit=100`;
        const r = await fetch(url);
        if (r.ok) setEntries(await r.json());
      } catch {}
    };
    void refresh();
    const id = window.setInterval(refresh, 10_000);
    return () => window.clearInterval(id);
  }, [filterCat]);

  const categories = Array.from(new Set(entries.map(e => e.category))).sort();

  return (
    <section className="sidebar-section">
      <h3 className="sidebar-section-title">Audit log ({entries.length})</h3>
      <select value={filterCat} onChange={e => setFilterCat(e.target.value)}
              style={{ marginBottom: 8, width: "100%", textAlign: "left" }}
              className="settings-input">
        <option value="">All categories</option>
        {categories.map(c => <option key={c} value={c}>{c}</option>)}
      </select>

      {entries.length === 0 ? (
        <p className="muted small">no audit entries yet</p>
      ) : (
        <ul className="audit-log-list">
          {entries.slice(0, 30).map(e => (
            <li key={e.id} className="audit-log-row">
              <div className="audit-log-cat" style={{ color: CATEGORY_COLOR[e.category] ?? "var(--color-text-muted)" }}>
                {e.category}
              </div>
              <div className="audit-log-summary">{e.summary}</div>
              <div className="muted small">
                {new Date(e.timestamp + (e.timestamp.endsWith("Z") ? "" : "Z"))
                  .toLocaleString("en-US", {
                    timeZone: "America/Chicago",
                    hour12: false,
                    month: "2-digit", day: "2-digit",
                    hour: "2-digit", minute: "2-digit", second: "2-digit",
                  })} CT · {e.actor}
              </div>
            </li>
          ))}
        </ul>
      )}
    </section>
  );
}

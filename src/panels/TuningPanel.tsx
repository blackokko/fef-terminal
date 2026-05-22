import { useEffect, useState } from "react";
import { formatDateTimeCT } from "../lib/displayHelpers";

const API_BASE = "http://127.0.0.1:8765";

interface TuningRec {
  id: number;
  generated_at: string;
  based_on_sessions: number;
  based_on_window_start: string;
  based_on_window_end: string;
  kind: string;
  priority: string;
  title: string;
  body: string;
  backing_data: Record<string, any>;
  status: string;
}

interface EngineChange {
  id: number;
  applied_at: string;
  name: string;
  description: string;
  category: string;
  measured_impact_r: number | null;
  git_commit_sha: string | null;
}

const PRIORITY_COLOR: Record<string, string> = {
  HIGH: "var(--color-red)",
  MEDIUM: "var(--color-amber)",
  LOW: "var(--color-text-muted)",
};

const STATE_COLOR: Record<string, string> = {
  GREEN: "var(--color-green)",
  AMBER: "var(--color-amber)",
  RED: "var(--color-red)",
  NO_DATA: "var(--color-text-muted)",
};

const fmtTime = (iso: string): string => {
  return formatDateTimeCT(iso + (iso.endsWith("Z") ? "" : "Z"));
};

export function TuningPanel() {
  const [recs, setRecs] = useState<TuningRec[]>([]);
  const [changes, setChanges] = useState<EngineChange[]>([]);
  const [busy, setBusy] = useState(false);
  const [rangeStart, setRangeStart] = useState("");
  const [rangeEnd, setRangeEnd] = useState("");

  const refresh = async () => {
    try {
      const r1 = await fetch(`${API_BASE}/tuning/recommendations`);
      if (r1.ok) setRecs(await r1.json());
      const r2 = await fetch(`${API_BASE}/engine/changes?limit=20`);
      if (r2.ok) setChanges(await r2.json());
    } catch {}
  };

  useEffect(() => {
    void refresh();
    const id = window.setInterval(refresh, 30000);
    return () => window.clearInterval(id);
  }, []);

  const recompute = async () => {
    setBusy(true);
    try {
      const body: any = {};
      if (rangeStart) body.start = rangeStart;
      if (rangeEnd) body.end = rangeEnd;
      await fetch(`${API_BASE}/tuning/recompute`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(body),
      });
      await refresh();
    } finally { setBusy(false); }
  };

  const decide = async (id: number, decision: "APPLIED" | "DISMISSED") => {
    setBusy(true);
    try {
      await fetch(`${API_BASE}/tuning/decide`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ recommendation_id: id, decision }),
      });
      await refresh();
    } finally { setBusy(false); }
  };

  const stateRec = recs.find(r => r.kind === "STATE_OF_MODEL");
  const otherRecs = recs.filter(r => r.kind !== "STATE_OF_MODEL");
  const state = stateRec?.backing_data?.state ?? "NO_DATA";

  return (
    <section className="sidebar-section">
      <h3 className="sidebar-section-title">Tuning recommendations</h3>

      <div style={{
        padding: "8px 10px", marginBottom: 10,
        background: "var(--color-bg-elevated)",
        borderLeft: `4px solid ${STATE_COLOR[state]}`,
      }}>
        <div style={{ fontFamily: "var(--font-mono)", fontWeight: 700,
                      fontSize: 13, color: STATE_COLOR[state] }}>
          STATE: {state}
        </div>
        {stateRec && <div className="muted small">{stateRec.body}</div>}
      </div>

      <div className="muted small" style={{ marginBottom: 6 }}>
        Date range (blank = all):
        <input type="date" value={rangeStart}
               onChange={e => setRangeStart(e.target.value)}
               className="settings-input" style={{ marginLeft: 4, width: 130 }} />
        <input type="date" value={rangeEnd}
               onChange={e => setRangeEnd(e.target.value)}
               className="settings-input" style={{ marginLeft: 4, width: 130 }} />
        <button onClick={() => void recompute()} disabled={busy}
                style={{ marginLeft: 4 }}>
          {busy ? "..." : "Re-run"}
        </button>
      </div>

      {otherRecs.length === 0 ? (
        <p className="muted small">No active recommendations. Engine looks stable.</p>
      ) : (
        <ul style={{ listStyle: "none", padding: 0, margin: 0 }}>
          {otherRecs.map(r => (
            <li key={r.id} style={{
              padding: "6px 8px", marginBottom: 6,
              background: "var(--color-bg-elevated)",
              borderLeft: `3px solid ${PRIORITY_COLOR[r.priority]}`,
            }}>
              <div style={{ display: "flex", justifyContent: "space-between" }}>
                <strong style={{ fontSize: 12 }}>{r.title}</strong>
                <span className="muted small">{r.kind}</span>
              </div>
              <div className="muted small" style={{ marginTop: 2 }}>
                {r.body}
              </div>
              <div className="muted small" style={{ marginTop: 2 }}>
                Based on {r.based_on_sessions} sessions
                {r.based_on_window_start && ` (${r.based_on_window_start} → ${r.based_on_window_end})`}
              </div>
              <div style={{ marginTop: 4, display: "flex", gap: 6 }}>
                <button onClick={() => void decide(r.id, "APPLIED")} disabled={busy}
                        style={{ fontSize: 10 }}>Mark applied</button>
                <button onClick={() => void decide(r.id, "DISMISSED")} disabled={busy}
                        style={{ fontSize: 10 }}>Dismiss</button>
              </div>
            </li>
          ))}
        </ul>
      )}

      <div className="muted small" style={{ marginTop: 10, fontWeight: 600, color: "var(--color-text)" }}>
        Recent engine changes ({changes.length})
      </div>
      <ul style={{ listStyle: "none", padding: 0, margin: 0, fontSize: 11 }}>
        {changes.slice(0, 10).map(c => (
          <li key={c.id} style={{
            padding: "3px 0", borderBottom: "1px solid var(--color-border)",
          }}>
            <div style={{ fontFamily: "var(--font-mono)" }}>
              {fmtTime(c.applied_at)} — <strong>{c.name}</strong>
              <span className="muted small" style={{ marginLeft: 6 }}>
                [{c.category}]
                {c.measured_impact_r != null
                  ? ` · impact ${c.measured_impact_r >= 0 ? "+" : ""}${c.measured_impact_r.toFixed(2)}R`
                  : ""}
              </span>
            </div>
            <div className="muted small">{c.description}</div>
          </li>
        ))}
      </ul>
    </section>
  );
}

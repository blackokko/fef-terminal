import { useEffect, useState } from "react";
import { formatDateTimeCT } from "../lib/displayHelpers";

const API_BASE = "http://127.0.0.1:8765";

/**
 * Engine change log — the factual half of the retired TuningPanel.
 *
 * WHY THIS EXISTS SEPARATELY (2026-07-30): TuningPanel bundled two things
 * with opposite trustworthiness. The `/tuning/recommendations` half produced
 * parameter suggestions that the 2026-07-01 research-tab audit found to be
 * unweighted, stale and hypothetical — and that the walk-forward drift study
 * showed were unstable (optimal `wall_proximity` flipped 3 -> 5 -> 8 across
 * consecutive sessions from the same 4-knob search). The `/engine/changes`
 * half is just a ledger of what was actually applied and when. That is a
 * record, not an inference: it cannot be overfit, and it is the only way to
 * answer "does this session look odd because we changed something?".
 *
 * So the recommendations were dropped and this was kept. This panel only
 * READS. It has no Apply button by design — applying a tuning recommendation
 * was itself an engine change, which is exactly the loop that produced
 * untracked drift.
 */

interface EngineChange {
  id: number;
  applied_at: string;
  name: string;
  description: string;
  category: string;
  measured_impact_r: number | null;
  git_commit_sha: string | null;
}

const fmtTime = (iso: string): string => {
  return formatDateTimeCT(iso + (iso.endsWith("Z") ? "" : "Z"));
};

export function EngineChangeLogPanel() {
  const [changes, setChanges] = useState<EngineChange[]>([]);
  const [err, setErr] = useState<string | null>(null);

  const refresh = async () => {
    try {
      const r = await fetch(`${API_BASE}/engine/changes?limit=25`);
      if (r.ok) {
        setChanges(await r.json());
        setErr(null);
      } else {
        setErr(`sidecar returned ${r.status}`);
      }
    } catch {
      setErr("sidecar unreachable");
    }
  };

  useEffect(() => {
    void refresh();
    const id = window.setInterval(refresh, 30000);
    return () => window.clearInterval(id);
  }, []);

  return (
    <section className="sidebar-section">
      <h3 className="sidebar-section-title">Engine change log</h3>

      {err && <p className="muted small">{err}</p>}

      {!err && changes.length === 0 && (
        <p className="muted small">No engine changes recorded.</p>
      )}

      <ul style={{ listStyle: "none", padding: 0, margin: 0, fontSize: 11 }}>
        {changes.map((c) => (
          <li
            key={c.id}
            style={{ padding: "3px 0", borderBottom: "1px solid var(--color-border)" }}
          >
            <div style={{ fontFamily: "var(--font-mono)" }}>
              {fmtTime(c.applied_at)} — <strong>{c.name}</strong>
              <span className="muted small" style={{ marginLeft: 6 }}>
                [{c.category}]
                {/* measured_impact_r is shown AS RECORDED. Historical R->$
                    conversions in this project were overstated ~7x (they
                    assumed ATM delta 0.50 where live strikes ran ~0.09), so
                    treat an old impact figure as a label, not a P&L claim. */}
                {c.measured_impact_r != null
                  ? ` · recorded impact ${c.measured_impact_r >= 0 ? "+" : ""}${c.measured_impact_r.toFixed(2)}R`
                  : ""}
                {c.git_commit_sha ? ` · ${c.git_commit_sha.slice(0, 7)}` : ""}
              </span>
            </div>
            <div className="muted small">{c.description}</div>
          </li>
        ))}
      </ul>
    </section>
  );
}

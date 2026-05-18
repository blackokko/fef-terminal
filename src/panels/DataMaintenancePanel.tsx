import { useEffect, useState } from "react";
import { useToast } from "../components/Toast";

const API_BASE = "http://127.0.0.1:8765";

interface RowCounts {
  signal_iter: number;
  trade_ticket: number;
  historical_bar: number;
  flow_snapshot: number;
  microstructure_sample: number;
  vix_sample: number;
  signal_iter_by_mode: { live: number; mock: number; untagged_pre_tag_system: number };
}

export function DataMaintenancePanel() {
  const [counts, setCounts] = useState<RowCounts | null>(null);
  const [busy, setBusy] = useState(false);
  const toast = useToast();

  const refresh = async () => {
    try {
      const r = await fetch(`${API_BASE}/db/row_counts`);
      if (r.ok) setCounts(await r.json());
    } catch {}
  };

  useEffect(() => {
    void refresh();
  }, []);

  const handleClear = async () => {
    if (!counts) return;
    const ok = window.confirm(
      `This will permanently delete:\n` +
      `  • ${counts.signal_iter_by_mode.mock} mock signals\n` +
      `  • ${counts.signal_iter_by_mode.untagged_pre_tag_system} untagged signals (pre-tagging system)\n` +
      `  • ${counts.historical_bar} historical bars\n` +
      `  • ${counts.flow_snapshot} flow snapshots\n` +
      `  • ${counts.microstructure_sample} microstructure samples\n` +
      `  • ${counts.vix_sample} VIX samples\n\n` +
      `LIVE signals (${counts.signal_iter_by_mode.live}) will be PRESERVED.\n\n` +
      `Continue?`
    );
    if (!ok) return;
    setBusy(true);
    try {
      const r = await fetch(`${API_BASE}/db/clear_mock_history`, { method: "POST" });
      const data = await r.json();
      toast.show(`Cleared ${data.total} rows`, { variant: "success" });
      await refresh();
    } catch (e) {
      toast.show(`Clear failed: ${(e as Error).message}`, { variant: "error" });
    } finally {
      setBusy(false);
    }
  };

  if (!counts) return (
    <section className="sidebar-section">
      <h3 className="sidebar-section-title">Data maintenance</h3>
      <p className="muted small">loading...</p>
    </section>
  );

  const m = counts.signal_iter_by_mode;
  return (
    <section className="sidebar-section">
      <h3 className="sidebar-section-title">Data maintenance</h3>

      <div className="muted small" style={{ marginBottom: 4 }}>Signal iter rows by provider mode:</div>
      <table className="db-counts-table">
        <tbody>
          <tr><td>Live</td><td style={{ color: "var(--color-green)", fontWeight: 600 }}>{m.live}</td></tr>
          <tr><td>Mock</td><td style={{ color: "var(--color-amber)" }}>{m.mock}</td></tr>
          <tr><td>Untagged (pre-tag)</td><td className="muted">{m.untagged_pre_tag_system}</td></tr>
        </tbody>
      </table>

      <div className="muted small" style={{ marginTop: 6, marginBottom: 4 }}>Other historical tables:</div>
      <table className="db-counts-table">
        <tbody>
          <tr><td>Trade tickets</td><td>{counts.trade_ticket}</td></tr>
          <tr><td>Historical bars</td><td>{counts.historical_bar}</td></tr>
          <tr><td>Flow snapshots</td><td>{counts.flow_snapshot}</td></tr>
          <tr><td>Microstructure</td><td>{counts.microstructure_sample}</td></tr>
          <tr><td>VIX samples</td><td>{counts.vix_sample}</td></tr>
        </tbody>
      </table>

      <button onClick={() => void handleClear()} disabled={busy}
              className="kill-switch-button" style={{ marginTop: 10 }}>
        {busy ? "Clearing..." : "🗑 Clear mock + untagged data"}
      </button>
      <div className="muted small" style={{ marginTop: 4 }}>
        Live-tagged signals preserved. Trade tickets attached to deleted signals
        also removed. Historical bars/flow/microstructure cleared (those aren't
        per-row tagged but came from mock testing).
      </div>
    </section>
  );
}

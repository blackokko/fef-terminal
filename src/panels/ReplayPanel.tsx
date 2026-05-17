import { useEffect, useState } from "react";

// IMPROVEMENTS #19: browse past session iter-by-iter.

interface SignalRow {
  id: number;
  iter_num: number;
  session_date: string;
  timestamp: string;
  spx_price: number;
  direction: string;
  confidence: string;
  reasoning: string;
}

const API_BASE = "http://127.0.0.1:8765";

function todayET(): string {
  const now = new Date();
  const et = new Date(now.getTime() - 4 * 60 * 60 * 1000);
  return et.toISOString().slice(0, 10);
}

export function ReplayPanel() {
  const [date, setDate] = useState<string>(todayET());
  const [rows, setRows] = useState<SignalRow[]>([]);
  const [loading, setLoading] = useState(false);
  const [selected, setSelected] = useState<SignalRow | null>(null);

  useEffect(() => {
    setLoading(true);
    void fetch(`${API_BASE}/history/replay/${date}`)
      .then((r) => r.json())
      .then((d) => setRows(d as SignalRow[]))
      .catch(() => setRows([]))
      .finally(() => setLoading(false));
  }, [date]);

  const stats = {
    total: rows.length,
    long_calls: rows.filter((r) => r.direction === "LONG_CALLS").length,
    long_puts: rows.filter((r) => r.direction === "LONG_PUTS").length,
    high: rows.filter((r) => r.confidence === "HIGH").length,
  };

  return (
    <details className="sidebar-section">
      <summary className="sidebar-section-title">Replay past session</summary>
      <div className="replay-body">
        <div className="replay-row">
          <input
            type="date"
            value={date}
            onChange={(e) => setDate(e.target.value)}
            className="settings-input"
            style={{ width: "auto" }}
          />
        </div>

        {loading ? (
          <p className="muted small">loading…</p>
        ) : (
          <>
            <div className="replay-stats small muted">
              {stats.total} iters · {stats.long_calls} calls · {stats.long_puts} puts ·{" "}
              <span style={{ color: "var(--color-green)" }}>{stats.high} HIGH</span>
            </div>

            {rows.length === 0 ? (
              <p className="muted small">no iters for {date}</p>
            ) : (
              <ul className="signal-log">
                {rows.map((r) => (
                  <li
                    key={r.id}
                    className={`signal-log-row signal-${r.direction.toLowerCase()}`}
                    style={{ cursor: "pointer" }}
                    onClick={() => setSelected(r === selected ? null : r)}
                  >
                    <span className="signal-iter">#{r.iter_num}</span>
                    <span className="signal-spx">${r.spx_price.toFixed(2)}</span>
                    <span className="signal-direction">{r.direction}</span>
                    <span className="signal-confidence muted small">{r.confidence}</span>
                  </li>
                ))}
              </ul>
            )}

            {selected && (
              <div className="replay-detail">
                <div className="small muted">
                  {new Date(selected.timestamp).toLocaleTimeString()}
                </div>
                <div className="small">{selected.reasoning}</div>
              </div>
            )}
          </>
        )}
      </div>
    </details>
  );
}

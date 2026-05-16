import { useEffect, useState } from "react";
import { useSidecar } from "../context/WebSocketContext";

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
const POLL_MS = 15_000;

export function SignalLogPanel() {
  const { lastIter } = useSidecar();
  const [rows, setRows] = useState<SignalRow[]>([]);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    let cancelled = false;

    const fetchRows = async () => {
      try {
        const r = await fetch(`${API_BASE}/history/signals?limit=25`);
        if (!r.ok) throw new Error(`status ${r.status}`);
        const data = (await r.json()) as SignalRow[];
        if (!cancelled) {
          setRows(data);
          setError(null);
        }
      } catch (e) {
        if (!cancelled) setError((e as Error).message);
      }
    };

    void fetchRows();
    const id = window.setInterval(fetchRows, POLL_MS);
    return () => {
      cancelled = true;
      window.clearInterval(id);
    };
    // re-fetch when a new iter arrives, so we don't wait for the poll
  }, [lastIter?.iter_num]);

  return (
    <section className="sidebar-section">
      <h3 className="sidebar-section-title">
        Signal Log {rows.length > 0 && `· ${rows.length}`}
      </h3>

      {error && (
        <p className="muted small" style={{ color: "var(--color-red)" }}>
          {error}
        </p>
      )}

      {rows.length === 0 ? (
        <p className="muted">no entries yet</p>
      ) : (
        <ul className="signal-log">
          {rows.map((r) => (
            <li
              key={r.id}
              className={`signal-log-row signal-${r.direction.toLowerCase()}`}
            >
              <span className="signal-iter">#{r.iter_num}</span>
              <span className="signal-spx">${r.spx_price.toFixed(2)}</span>
              <span className="signal-direction">{r.direction}</span>
              <span className="signal-confidence muted small">
                {r.confidence}
              </span>
            </li>
          ))}
        </ul>
      )}
    </section>
  );
}

import { useEffect, useState } from "react";

const API_BASE = "http://127.0.0.1:8765";

interface LoopState {
  status: string;
  tick_quote_count?: number;
  tick_trade_count?: number;
  last_tick_age_ms?: number | null;
}

interface StreamsResponse {
  now_ms: number;
  loops: Record<string, LoopState>;
  depth_update_count: number;
  ibkr: string;
}

const STATUS_COLOR: Record<string, string> = {
  RUNNING: "var(--color-green)",
  NOT_STARTED: "var(--color-text-muted)",
  DONE: "var(--color-red)",
  CANCELLED: "var(--color-red)",
  UNKNOWN: "var(--color-amber)",
};

export function StreamHealthPanel() {
  const [data, setData] = useState<StreamsResponse | null>(null);

  useEffect(() => {
    const refresh = async () => {
      try {
        const r = await fetch(`${API_BASE}/diag/streams`);
        if (r.ok) setData(await r.json());
      } catch {}
    };
    void refresh();
    const id = window.setInterval(refresh, 3000);
    return () => window.clearInterval(id);
  }, []);

  if (!data) {
    return (
      <section className="sidebar-section">
        <h3 className="sidebar-section-title">Stream health</h3>
        <p className="muted small">loading...</p>
      </section>
    );
  }

  // Tick stream — special: highlight RED if tick age > 30s
  const tickStream = data.loops.tick_stream;
  const tickStale = tickStream?.last_tick_age_ms != null && tickStream.last_tick_age_ms > 30000;
  const tickColor = tickStale ? "var(--color-red)" : STATUS_COLOR[tickStream?.status ?? "UNKNOWN"];

  return (
    <section className="sidebar-section">
      <h3 className="sidebar-section-title">Stream health</h3>

      <table style={{ width: "100%", fontFamily: "var(--font-mono)", fontSize: 11 }}>
        <thead>
          <tr style={{ color: "var(--color-text-muted)", textAlign: "left" }}>
            <th>Loop</th><th>Status</th><th style={{ textAlign: "right" }}>Detail</th>
          </tr>
        </thead>
        <tbody>
          {Object.entries(data.loops).map(([name, ls]) => {
            const isTick = name === "tick_stream";
            const color = isTick ? tickColor : STATUS_COLOR[ls.status] ?? "var(--color-text-muted)";
            const detail = isTick
              ? (ls.last_tick_age_ms != null ? `${Math.round(ls.last_tick_age_ms/1000)}s ago` : "no data")
              : "";
            return (
              <tr key={name} style={{ borderTop: "1px solid var(--color-border)" }}>
                <td>{name}</td>
                <td>
                  <span style={{
                    display: "inline-block", width: 8, height: 8, borderRadius: "50%",
                    background: color, marginRight: 4,
                  }} />
                  <span style={{ color }}>{ls.status}</span>
                </td>
                <td style={{ textAlign: "right", color: tickStale && isTick ? "var(--color-red)" : undefined }}>
                  {detail}
                </td>
              </tr>
            );
          })}
        </tbody>
      </table>

      <div className="muted small" style={{ marginTop: 6 }}>
        Counts: quote {tickStream?.tick_quote_count ?? 0} · trade {tickStream?.tick_trade_count ?? 0} · depth {data.depth_update_count}
      </div>
    </section>
  );
}

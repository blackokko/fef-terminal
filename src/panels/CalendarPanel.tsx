import { useEffect, useState } from "react";

const API_BASE = "http://127.0.0.1:8765";

interface CalendarEvent {
  id: number;
  event_date: string;
  event_time_et: string | null;
  category: string;
  title: string;
  impact: string;
  auto_pause_minutes_before: number;
  auto_pause_minutes_after: number;
  notes: string;
}

interface PauseCheck {
  paused: boolean;
  reason: string;
  event: { date: string; category: string; title: string } | null;
}

const IMPACT_COLOR: Record<string, string> = {
  HIGH: "var(--color-red)",
  MEDIUM: "var(--color-amber)",
  LOW: "var(--color-text-muted)",
};

export function CalendarPanel() {
  const [events, setEvents] = useState<CalendarEvent[]>([]);
  const [pause, setPause] = useState<PauseCheck | null>(null);
  const today = new Date().toISOString().slice(0, 10);

  const refresh = async () => {
    try {
      const [eRes, pRes] = await Promise.all([
        fetch(`${API_BASE}/calendar?from_date=${today}`),
        fetch(`${API_BASE}/calendar/pause_check`),
      ]);
      if (eRes.ok) setEvents(await eRes.json());
      if (pRes.ok) setPause(await pRes.json());
    } catch {}
  };

  useEffect(() => {
    void refresh();
    const id = window.setInterval(refresh, 30_000);
    return () => window.clearInterval(id);
  }, []);

  return (
    <section className="sidebar-section">
      <h3 className="sidebar-section-title">Macro calendar · auto-pause</h3>

      {pause && (
        <div className="calendar-pause-banner" style={{
          borderLeft: `3px solid ${pause.paused ? "var(--color-red)" : "var(--color-green)"}`,
        }}>
          <div style={{
            color: pause.paused ? "var(--color-red)" : "var(--color-green)",
            fontWeight: 700, fontFamily: "var(--font-mono)",
          }}>
            {pause.paused ? "🛑 PAUSED" : "✓ active"}
          </div>
          <div className="muted small">{pause.reason || "no event window"}</div>
        </div>
      )}

      {events.length === 0 ? (
        <p className="muted small">no upcoming events</p>
      ) : (
        <table className="calendar-table">
          <thead><tr><th>Date</th><th>Time</th><th>Event</th><th>Impact</th></tr></thead>
          <tbody>
            {events.slice(0, 15).map(e => (
              <tr key={e.id}>
                <td className="muted">{e.event_date}</td>
                <td className="muted">{e.event_time_et ?? "all day"}</td>
                <td>
                  <strong>{e.category}</strong>
                  <div className="muted small">{e.title}</div>
                </td>
                <td><span style={{ color: IMPACT_COLOR[e.impact], fontWeight: 600 }}>{e.impact}</span></td>
              </tr>
            ))}
          </tbody>
        </table>
      )}
    </section>
  );
}

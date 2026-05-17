import { useEffect, useState } from "react";
import { useToast } from "../components/Toast";
import { useSidecar } from "../context/WebSocketContext";
import { fetchJson } from "../lib/api";
import { formatDollars } from "../lib/format";

// Risk overlay: daily loss cap, max position size, circuit breaker visibility.
// Pulls /risk/state and exposes the limits to the user. When daily P/L
// crosses the cap, shows a big red breach banner.

interface RiskState {
  daily_pnl: number;
  daily_loss_cap: number | null;
  cap_used_pct: number | null;
  breached: boolean;
  max_position_dollars: number | null;
  max_contracts_per_trade: number;
}

export function RiskOverlayPanel() {
  const toast = useToast();
  const { lastAccount } = useSidecar();
  const [state, setState] = useState<RiskState | null>(null);
  const [editing, setEditing] = useState(false);
  const [cap, setCap] = useState<string>("");
  const [maxPos, setMaxPos] = useState<string>("");
  const [maxContracts, setMaxContracts] = useState<string>("25");

  const reload = async () => {
    const s = await fetchJson<RiskState>("/risk/state", toast);
    if (s) {
      setState(s);
      setCap(s.daily_loss_cap?.toString() ?? "");
      setMaxPos(s.max_position_dollars?.toString() ?? "");
      setMaxContracts(s.max_contracts_per_trade.toString());
    }
  };

  useEffect(() => {
    void reload();
    const id = window.setInterval(reload, 15_000);
    return () => window.clearInterval(id);
  }, []);

  // Recheck when account P/L updates
  useEffect(() => {
    if (lastAccount?.daily_pnl != null) void reload();
  }, [lastAccount?.daily_pnl]);

  const save = async () => {
    const updates: Record<string, number | null> = {
      daily_loss_cap_dollars: cap ? Number(cap) : null,
      max_position_dollars: maxPos ? Number(maxPos) : null,
      max_contracts_per_trade: Number(maxContracts) || 25,
    };
    await fetchJson("/config", toast, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(updates),
    });
    setEditing(false);
    toast.show("Risk limits saved", { variant: "success" });
    void reload();
  };

  if (!state) {
    return (
      <section className="sidebar-section">
        <h3 className="sidebar-section-title">Risk overlay</h3>
        <p className="muted small">loading…</p>
      </section>
    );
  }

  return (
    <section className={`sidebar-section risk-panel ${state.breached ? "risk-breached" : ""}`}>
      <h3 className="sidebar-section-title">
        Risk overlay
        {state.breached && <span style={{ color: "var(--color-red)", marginLeft: 6 }}>⚠ BREACH</span>}
      </h3>

      {state.breached && state.daily_loss_cap && (
        <div className="risk-breach-banner">
          Daily loss cap (${state.daily_loss_cap}) hit — STOP TRADING
        </div>
      )}

      <div className="risk-body">
        <div className="risk-row">
          <span className="risk-label">Daily P/L</span>
          <span
            className="risk-value"
            style={{ color: state.daily_pnl >= 0 ? "var(--color-green)" : "var(--color-red)" }}
          >
            {formatDollars(state.daily_pnl, { sign: true })}
          </span>
        </div>
        {state.daily_loss_cap != null && state.cap_used_pct != null && (
          <div className="risk-row">
            <span className="risk-label">Cap usage</span>
            <div className="risk-cap-bar-wrap">
              <div
                className="risk-cap-bar"
                style={{
                  width: `${state.cap_used_pct}%`,
                  background: state.cap_used_pct > 80 ? "var(--color-red)" :
                              state.cap_used_pct > 50 ? "var(--color-amber)" : "var(--color-green)",
                }}
              />
            </div>
            <span className="muted small">{state.cap_used_pct}%</span>
          </div>
        )}
        <div className="risk-row">
          <span className="risk-label">Cap</span>
          <span className="risk-value">{state.daily_loss_cap ? formatDollars(state.daily_loss_cap) : "—"}</span>
        </div>
        <div className="risk-row">
          <span className="risk-label">Max position</span>
          <span className="risk-value">{state.max_position_dollars ? formatDollars(state.max_position_dollars) : "—"}</span>
        </div>
        <div className="risk-row">
          <span className="risk-label">Max contracts</span>
          <span className="risk-value">{state.max_contracts_per_trade}</span>
        </div>

        {editing ? (
          <div className="risk-edit">
            <div className="outcome-row">
              <span className="ticket-label">Loss cap $</span>
              <input type="number" value={cap} onChange={(e) => setCap(e.target.value)}
                placeholder="e.g. 2000" className="settings-input" />
            </div>
            <div className="outcome-row">
              <span className="ticket-label">Max pos $</span>
              <input type="number" value={maxPos} onChange={(e) => setMaxPos(e.target.value)}
                placeholder="e.g. 25000" className="settings-input" />
            </div>
            <div className="outcome-row">
              <span className="ticket-label">Max contracts</span>
              <input type="number" value={maxContracts} onChange={(e) => setMaxContracts(e.target.value)}
                className="settings-input" />
            </div>
            <div className="outcome-actions">
              <button onClick={() => void save()}>Save</button>
              <button onClick={() => setEditing(false)}>Cancel</button>
            </div>
          </div>
        ) : (
          <button onClick={() => setEditing(true)} style={{ marginTop: 6 }}>Edit limits</button>
        )}
      </div>
    </section>
  );
}

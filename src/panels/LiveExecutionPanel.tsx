import { useEffect, useState } from "react";
import { useToast } from "../components/Toast";

const API_BASE = "http://127.0.0.1:8765";

interface LiveStatus {
  live_trading_enabled: boolean;
  live_trading_env_var: string;
  confirm_required_per_order: boolean;
  dry_run_default: boolean;
}

interface LiveOrder {
  id: number;
  submitted_at: string;
  symbol: string;
  strike: number;
  right: string;
  expiry: string;
  action: string;
  qty: number;
  order_type: string;
  limit_price: number | null;
  parent_id: number | null;
  bracket_role: string | null;
  status: string;
  filled_qty: number;
  avg_fill_price: number | null;
  commission: number;
  dry_run: boolean;
  ib_order_id: number | null;
}

export function LiveExecutionPanel() {
  const [status, setStatus] = useState<LiveStatus | null>(null);
  const [orders, setOrders] = useState<LiveOrder[]>([]);
  const [killing, setKilling] = useState(false);
  const toast = useToast();

  const refresh = async () => {
    try {
      const [s, o] = await Promise.all([
        fetch(`${API_BASE}/live/status`).then(r => r.json()),
        fetch(`${API_BASE}/live/orders?limit=20`).then(r => r.json()),
      ]);
      setStatus(s);
      setOrders(o);
    } catch {
      // sidecar down
    }
  };

  useEffect(() => {
    void refresh();
    const id = window.setInterval(refresh, 5000);
    return () => window.clearInterval(id);
  }, []);

  const handleKill = async () => {
    if (!window.confirm("Cancel all working orders? This is irreversible.")) return;
    setKilling(true);
    try {
      const r = await fetch(`${API_BASE}/live/kill`, { method: "POST" });
      const data = await r.json();
      toast.show(`Kill switch: ${data.cancelled ?? 0} order(s) cancelled`, { variant: "warn" });
      void refresh();
    } catch (e) {
      toast.show(`Kill failed: ${(e as Error).message}`, { variant: "error" });
    } finally {
      setKilling(false);
    }
  };

  if (!status) {
    return (
      <section className="sidebar-section">
        <h3 className="sidebar-section-title">Live execution</h3>
        <p className="muted small">loading...</p>
      </section>
    );
  }

  const parents = orders.filter(o => o.bracket_role === "parent" || o.bracket_role === null);

  return (
    <section className="sidebar-section live-exec-panel">
      <h3 className="sidebar-section-title">Live execution</h3>

      <div className="live-exec-banner" style={{
        borderLeft: `3px solid ${status.live_trading_enabled ? "var(--color-red)" : "var(--color-amber)"}`,
      }}>
        <div style={{
          color: status.live_trading_enabled ? "var(--color-red)" : "var(--color-amber)",
          fontWeight: 700, fontFamily: "var(--font-mono)",
        }}>
          {status.live_trading_enabled ? "🔴 LIVE TRADING" : "⚠ DRY RUN"}
        </div>
        <div className="muted small">
          {status.live_trading_enabled
            ? "Orders WILL hit IBKR. confirm=true still required per order."
            : `Set ${status.live_trading_env_var}=true env to enable`}
        </div>
      </div>

      <button onClick={() => void handleKill()} disabled={killing}
              className="kill-switch-button">
        {killing ? "Cancelling..." : "🛑 KILL ALL ORDERS"}
      </button>

      <div className="live-orders-section">
        <div className="muted small" style={{ marginTop: 8, marginBottom: 4 }}>
          Recent orders ({parents.length})
        </div>
        {parents.length === 0 ? (
          <p className="muted small">no orders yet</p>
        ) : (
          <ul className="live-orders-list">
            {parents.slice(0, 5).map(o => (
              <li key={o.id} className="live-order-row">
                <div>
                  <strong>{o.action} {o.qty} {o.symbol} {o.strike}{o.right}</strong>
                  {o.dry_run && <span className="live-order-dryrun"> DRY</span>}
                </div>
                <div className="muted small">
                  {o.status} · ${o.limit_price?.toFixed(2) ?? "—"} · {new Date(o.submitted_at).toLocaleTimeString()}
                </div>
              </li>
            ))}
          </ul>
        )}
      </div>
    </section>
  );
}

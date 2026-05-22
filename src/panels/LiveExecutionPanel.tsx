import { useEffect, useState } from "react";
import { useToast } from "../components/Toast";
import { formatTimeCT } from "../lib/displayHelpers";

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
    if (!window.confirm(
      "Cancel all working orders AND market-close every open option position?\n\n" +
      "This is irreversible. Auto-trade + scanner stay ON (use 🚨 FLAT for full kill-switch)."
    )) return;
    setKilling(true);
    try {
      const r = await fetch(`${API_BASE}/live/kill`, { method: "POST" });
      const data = await r.json();
      toast.show(
        `Kill: ${data.cancelled ?? 0} order(s) cancelled, ${data.positions_closed ?? 0} position(s) market-closed`,
        { variant: "warn" }
      );
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
        {killing ? "Closing..." : "🛑 KILL ORDERS + CLOSE POSITIONS"}
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
                  {o.status} · {o.order_type === "MKT"
                    ? "MARKET"
                    : `LMT @ $${o.limit_price?.toFixed(2) ?? "—"}`} · {
                    formatTimeCT(o.submitted_at + (o.submitted_at.endsWith("Z") ? "" : "Z"), { seconds: true })
                  }
                </div>
                {(o as any).dynamic_stop_level != null && (o as any).entry_spx_price != null && (() => {
                  const entry = (o as any).entry_spx_price as number;
                  const origStop = (o as any).spx_target_stop as number;
                  const dynStop = (o as any).dynamic_stop_level as number;
                  const risk = Math.abs(entry - origStop) || 1;
                  // True locked-R: how much profit (in R units) is now guaranteed
                  // vs entry SPX. Positive = locked profit, 0 = BE, negative = original stop.
                  const lockedR = o.right === "C"
                    ? (dynStop - entry) / risk
                    : (entry - dynStop) / risk;
                  const stepFired = (o as any).be_ladder_max_step_R ?? 0;
                  return (
                    <div className="muted small" style={{ marginTop: 2,
                      color: lockedR > 0 ? "var(--color-green)" :
                             lockedR === 0 && stepFired > 0 ? "var(--color-amber)" :
                             "var(--color-text-muted)" }}>
                      BE ladder: {stepFired === 0
                        ? `armed (orig stop ${origStop.toFixed(2)})`
                        : lockedR > 0
                          ? `locked +${lockedR.toFixed(2)}R → stop ${dynStop.toFixed(2)} (trigger ${stepFired.toFixed(1)}R)`
                          : `at BE (stop ${dynStop.toFixed(2)}, trigger ${stepFired.toFixed(1)}R)`}
                    </div>
                  );
                })()}
              </li>
            ))}
          </ul>
        )}
      </div>
    </section>
  );
}

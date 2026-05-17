import { useSidecar } from "../context/WebSocketContext";

// Sub-second microstructure signal — standalone from FEF.
// Based on: OFI (Cont/Kukanov/Stoikov 2014), AVI / Lee-Ready (1991),
// Microprice (Stoikov 2018). All peer-reviewed concepts.

const DIRECTION_COLOR: Record<string, string> = {
  BULLISH_PRESSURE: "var(--color-green)",
  BEARISH_PRESSURE: "var(--color-red)",
  BALANCED: "var(--color-text-muted)",
};

export function MicrostructurePanel() {
  const { lastMicrostructure } = useSidecar();

  if (!lastMicrostructure) {
    return (
      <section className="sidebar-section">
        <h3 className="sidebar-section-title">Microstructure · 60s window</h3>
        <p className="muted small">collecting quote + trade events…</p>
      </section>
    );
  }

  const m = lastMicrostructure;
  const color = DIRECTION_COLOR[m.direction] ?? "var(--color-text-muted)";
  const avi_pct = m.avi_pct;
  // AVI 50% = balanced. >55 = buy pressure, <45 = sell pressure
  const aviBalance = avi_pct - 50;

  return (
    <section className="sidebar-section microstructure-panel">
      <h3 className="sidebar-section-title">
        Microstructure · 60s · {m.quote_count}q / {m.trade_count}t
      </h3>

      <div className="micro-hero" style={{ borderLeft: `3px solid ${color}` }}>
        <div className="micro-direction" style={{ color }}>
          {m.direction.replace("_", " ")}
        </div>
        <div className="muted small">{m.confidence} · {m.reasoning}</div>
      </div>

      <div className="micro-grid">
        <div className="micro-stat">
          <div className="micro-stat-label">OFI (cum)</div>
          <div className="micro-stat-value" style={{ color: m.ofi_window > 0 ? "var(--color-green)" : m.ofi_window < 0 ? "var(--color-red)" : undefined }}>
            {m.ofi_window > 0 ? "+" : ""}{m.ofi_window.toFixed(0)}
          </div>
        </div>
        <div className="micro-stat">
          <div className="micro-stat-label">AVI (buy %)</div>
          <div className="micro-stat-value" style={{ color: avi_pct > 55 ? "var(--color-green)" : avi_pct < 45 ? "var(--color-red)" : undefined }}>
            {avi_pct.toFixed(1)}%
          </div>
        </div>
        <div className="micro-stat">
          <div className="micro-stat-label">Buy vol</div>
          <div className="micro-stat-value">{m.aggressive_buy_volume.toFixed(0)}</div>
        </div>
        <div className="micro-stat">
          <div className="micro-stat-label">Sell vol</div>
          <div className="micro-stat-value">{m.aggressive_sell_volume.toFixed(0)}</div>
        </div>
        <div className="micro-stat">
          <div className="micro-stat-label">Microprice</div>
          <div className="micro-stat-value">{m.microprice != null ? m.microprice.toFixed(2) : "—"}</div>
        </div>
        <div className="micro-stat">
          <div className="micro-stat-label">μP-dev (bp)</div>
          <div className="micro-stat-value" style={{ color: m.microprice_deviation_bp != null && m.microprice_deviation_bp > 0 ? "var(--color-green)" : m.microprice_deviation_bp != null && m.microprice_deviation_bp < 0 ? "var(--color-red)" : undefined }}>
            {m.microprice_deviation_bp != null
              ? (m.microprice_deviation_bp > 0 ? "+" : "") + m.microprice_deviation_bp.toFixed(1)
              : "—"}
          </div>
        </div>
      </div>

      <div className="micro-avi-bar">
        <div className="micro-avi-bar-bg">
          <div
            className="micro-avi-bar-fill"
            style={{
              left: aviBalance >= 0 ? "50%" : `${50 + aviBalance}%`,
              width: `${Math.abs(aviBalance)}%`,
              background: aviBalance >= 0 ? "var(--color-green)" : "var(--color-red)",
            }}
          />
          <div className="micro-avi-bar-mid" />
        </div>
        <div className="micro-avi-bar-labels muted small">
          <span>sell</span><span>50/50</span><span>buy</span>
        </div>
      </div>

      <details style={{ marginTop: 6 }}>
        <summary className="muted small">about these signals</summary>
        <ul className="micro-glossary small">
          <li><strong>OFI</strong>: Order Flow Imbalance (Cont/Kukanov/Stoikov 2014). Cumulative change in best-bid + best-ask sizes. Positive = net buying pressure on the book.</li>
          <li><strong>AVI</strong>: Aggressive Volume Imbalance via Lee-Ready (1991) trade classification. Trades at/above ask = buy, at/below bid = sell.</li>
          <li><strong>Microprice</strong>: Stoikov (2018) size-weighted midpoint. Better than naive mid at predicting next-trade direction.</li>
        </ul>
      </details>
    </section>
  );
}

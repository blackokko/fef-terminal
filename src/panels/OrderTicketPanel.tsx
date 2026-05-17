import { useEffect, useState } from "react";
import { useSidecar } from "../context/WebSocketContext";

const DEFAULT_QTY = 15;  // [[user-trading-style]]

export function OrderTicketPanel() {
  const { lastIter } = useSidecar();
  const [qty, setQty] = useState<number>(DEFAULT_QTY);
  const [copied, setCopied] = useState(false);

  useEffect(() => {
    if (!copied) return;
    const t = window.setTimeout(() => setCopied(false), 1500);
    return () => window.clearTimeout(t);
  }, [copied]);

  if (!lastIter) {
    return (
      <section className="sidebar-section">
        <h3 className="sidebar-section-title">Order Ticket</h3>
        <p className="muted">no actionable signal yet</p>
      </section>
    );
  }

  const v = lastIter.verdict;
  if (v.direction === "WAIT" || !v.strike_rec) {
    return (
      <section className="sidebar-section">
        <h3 className="sidebar-section-title">Order Ticket</h3>
        <p className="muted">
          last verdict WAIT — no ticket
          <br />
          <span className="small">({v.reasoning})</span>
        </p>
      </section>
    );
  }

  const side = v.direction === "LONG_CALLS" ? "C" : "P";
  const opt = v.option_pricing ?? null;

  const ticketText = formatTicketText(
    v.direction === "LONG_CALLS" ? "CALL" : "PUT",
    v.strike_rec,
    lastIter.session_date,
    qty,
    opt?.entry_limit ?? null,
  );

  const handleCopy = () => {
    void navigator.clipboard.writeText(ticketText).then(() => setCopied(true));
  };

  return (
    <section className="sidebar-section">
      <h3 className="sidebar-section-title">Order Ticket</h3>

      <div className="ticket-block">
        <div className="ticket-main">
          BUY × <input
            type="number"
            value={qty}
            min={1}
            max={100}
            className="qty-input"
            onChange={(e) => setQty(Number(e.target.value) || DEFAULT_QTY)}
          /> SPX {v.strike_rec.toFixed(0)}{side}
          {opt && (
            <span className="ticket-option-entry">
              @ ${opt.entry_limit.toFixed(2)}
            </span>
          )}
        </div>

        <div className="muted small">
          expiry {lastIter.session_date} · 0DTE
          {opt && opt.delta != null && (
            <> · delta {opt.delta.toFixed(2)}</>
          )}
        </div>

        <div className="ticket-targets-grid">
          <div className="ticket-targets-col">
            <div className="ticket-targets-header">SPX target</div>
            <Row label="T1" value={v.t1} suffix="" />
            <Row label="T2" value={v.t2} suffix="" />
            <Row label="T3" value={v.t3} suffix="" />
            <Row label="STOP" value={v.stop} suffix="" danger />
          </div>
          {opt && (
            <div className="ticket-targets-col">
              <div className="ticket-targets-header">Option price</div>
              <Row label="T1" value={opt.t1} prefix="$" />
              <Row label="T2" value={opt.t2} prefix="$" />
              <Row label="T3" value={opt.t3} prefix="$" />
              <Row label="STOP" value={opt.stop} prefix="$" danger />
            </div>
          )}
        </div>

        <button onClick={handleCopy}>
          {copied ? "✓ Copied" : "Copy to clipboard"}
        </button>

        <details className="ticket-text-detail">
          <summary className="small muted">show order text</summary>
          <pre>{ticketText}</pre>
        </details>
      </div>
    </section>
  );
}

function Row({
  label,
  value,
  prefix = "",
  suffix = "",
  danger = false,
}: {
  label: string;
  value: number | null;
  prefix?: string;
  suffix?: string;
  danger?: boolean;
}) {
  if (value == null) return null;
  return (
    <div>
      <span
        className="ticket-label"
        style={danger ? { color: "var(--color-red)" } : undefined}
      >
        {label}
      </span>
      <span>
        {prefix}
        {value.toFixed(value < 100 ? 2 : 0)}
        {suffix}
      </span>
    </div>
  );
}

function formatTicketText(
  right: "CALL" | "PUT",
  strike: number,
  expiryDate: string,
  qty: number,
  limitPrice: number | null,
): string {
  const date = new Date(expiryDate);
  const monthShort = date.toLocaleString("en-US", { month: "short" }).toUpperCase();
  const day = date.getDate().toString().padStart(2, "0");
  const year = date.getFullYear().toString().slice(-2);
  const price = limitPrice ? `$${limitPrice.toFixed(2)} LMT` : "LMT";
  return `BUY ${qty} SPX ${monthShort}${day}'${year} ${strike.toFixed(0)} ${right} @ ${price}`;
}

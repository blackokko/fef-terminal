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
  const ticketText = formatTicketText(
    v.direction === "LONG_CALLS" ? "CALL" : "PUT",
    v.strike_rec,
    lastIter.session_date,
    qty,
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
        </div>

        <div className="muted small">
          expiry {lastIter.session_date} · 0DTE
        </div>

        <div className="ticket-targets">
          <div>
            <span className="ticket-label">T1</span>
            <span>SPX {v.t1?.toFixed(0)}</span>
          </div>
          <div>
            <span className="ticket-label">T2</span>
            <span>SPX {v.t2?.toFixed(0)}</span>
          </div>
          <div>
            <span className="ticket-label">T3</span>
            <span>SPX {v.t3?.toFixed(0)}</span>
          </div>
          <div>
            <span className="ticket-label" style={{ color: "var(--color-red)" }}>
              STOP
            </span>
            <span>SPX {v.stop?.toFixed(0)}</span>
          </div>
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

function formatTicketText(
  right: "CALL" | "PUT",
  strike: number,
  expiryDate: string,
  qty: number,
): string {
  // Expiry: YYYY-MM-DD -> mmm DD 'YY style (TWS-friendly)
  const date = new Date(expiryDate);
  const monthShort = date.toLocaleString("en-US", { month: "short" }).toUpperCase();
  const day = date.getDate().toString().padStart(2, "0");
  const year = date.getFullYear().toString().slice(-2);
  return `BUY ${qty} SPX ${monthShort}${day}'${year} ${strike.toFixed(0)} ${right} @ LMT`;
}

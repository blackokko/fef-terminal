import { useEffect, useState } from "react";
import { useSidecar } from "../context/WebSocketContext";

const DEFAULT_QTY = 15;  // [[user-trading-style]]
const API_BASE = "http://127.0.0.1:8765";

interface SignalRow {
  id: number;
  iter_num: number;
}

export function OrderTicketPanel() {
  const { lastIter } = useSidecar();
  const [qty, setQty] = useState<number>(DEFAULT_QTY);
  const [copied, setCopied] = useState(false);
  const [qtyUserOverridden, setQtyUserOverridden] = useState(false);
  const [ticketId, setTicketId] = useState<number | null>(null);
  const [showOutcomeForm, setShowOutcomeForm] = useState(false);
  const [outcomeStatus, setOutcomeStatus] = useState<string | null>(null);
  const [sending, setSending] = useState(false);
  const [sendResult, setSendResult] = useState<{ ok: boolean; message: string } | null>(null);

  // Update default qty to track verdict's suggested_qty when it changes,
  // unless the user has manually edited it for this verdict.
  useEffect(() => {
    if (!qtyUserOverridden && lastIter?.verdict.suggested_qty) {
      setQty(lastIter.verdict.suggested_qty);
    }
  }, [lastIter?.iter_num, lastIter?.verdict.suggested_qty, qtyUserOverridden]);

  useEffect(() => {
    setQtyUserOverridden(false);
    setTicketId(null);  // new iter = new ticket lifecycle
    setShowOutcomeForm(false);
    setOutcomeStatus(null);
  }, [lastIter?.iter_num]);

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
  const spread = (v as any).spread_recommendation ?? null;

  const ticketText = formatTicketText(
    v.direction === "LONG_CALLS" ? "CALL" : "PUT",
    v.strike_rec,
    lastIter.session_date,
    qty,
    opt?.entry_limit ?? null,
  );

  const handleCopy = async () => {
    await navigator.clipboard.writeText(ticketText);
    setCopied(true);
    // Also create a DB ticket record so we can attach an outcome later
    if (ticketId === null) {
      try {
        // Look up signal_iter_id by iter_num for today's session
        const r = await fetch(`${API_BASE}/history/signals?limit=1`);
        const rows: SignalRow[] = await r.json();
        const sig = rows.find((x) => x.iter_num === lastIter!.iter_num);
        if (!sig) return;

        const resp = await fetch(`${API_BASE}/tickets`, {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({
            signal_iter_id: sig.id,
            direction: v.direction,
            contract_strike: v.strike_rec,
            contract_expiry: lastIter!.session_date,
            qty,
            entry_limit: opt?.entry_limit ?? 0,
            t1: v.t1 ?? 0,
            t2: v.t2 ?? 0,
            t3: v.t3 ?? 0,
            stop: v.stop ?? 0,
            status: "COPIED",
          }),
        });
        const data = await resp.json();
        if (data.ok) setTicketId(data.ticket_id);
      } catch (e) {
        console.error("Failed to record ticket:", e);
      }
    }
  };

  const handleSendToIBKR = async () => {
    if (!lastIter || !opt) return;
    const v = lastIter.verdict;
    if (!v.strike_rec) return;
    const today = new Date().toISOString().slice(0, 10).replace(/-/g, "");
    const direction = v.direction;
    const right = direction === "LONG_CALLS" ? "C" : "P";
    // 85/10/5 split (matches user trading style)
    const t1q = Math.max(1, Math.round(qty * 0.85));
    const t2q = Math.max(1, Math.round(qty * 0.10));
    const t3q = Math.max(1, qty - t1q - t2q);
    // Two-stage confirm
    const ok = window.confirm(
      `Place BRACKET ORDER via IBKR?\n\n` +
      `  BUY ${qty} SPX ${today} ${v.strike_rec}${right} @ $${opt.entry_limit.toFixed(2)}\n` +
      `  T1 (${t1q}× ~85%): $${opt.t1.toFixed(2)}\n` +
      `  T2 (${t2q}× runner): $${opt.t2.toFixed(2)}\n` +
      `  T3 (${t3q}× runner): $${opt.t3.toFixed(2)}\n` +
      `  STOP (all):   $${opt.stop.toFixed(2)}\n\n` +
      `Runner protector will exit T2/T3 at market if premium retraces to T1 fill.\n\n` +
      `Requires FEF_LIVE_TRADING=true env. Otherwise DRY_RUN (no real order).`
    );
    if (!ok) return;
    setSending(true);
    setSendResult(null);
    try {
      const r = await fetch(`${API_BASE}/live/place_bracket`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          symbol: "SPX", strike: v.strike_rec, right, expiry: today,
          qty,
          entry_limit: opt.entry_limit,
          t1_limit: opt.t1, t2_limit: opt.t2, t3_limit: opt.t3,
          stop_limit: opt.stop,
          confirm: true,
          ticket_id: ticketId,
        }),
      });
      const data = await r.json();
      if (data.ok) {
        setSendResult({
          ok: true,
          message: data.dry_run
            ? `DRY_RUN — wrote order #${data.parent_order_id} (FEF_LIVE_TRADING not set)`
            : `LIVE order placed — parent #${data.parent_order_id}`,
        });
      } else {
        setSendResult({ ok: false, message: data.error || data.reason || "submission failed" });
      }
    } catch (e) {
      setSendResult({ ok: false, message: (e as Error).message });
    } finally {
      setSending(false);
    }
  };

  const submitOutcome = async (fields: Record<string, number | string>) => {
    if (ticketId === null) return;
    try {
      const r = await fetch(`${API_BASE}/tickets/${ticketId}/close`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(fields),
      });
      const data = await r.json();
      if (data.ok) {
        setOutcomeStatus(`✓ Recorded (${data.status})`);
        setShowOutcomeForm(false);
      }
    } catch (e) {
      setOutcomeStatus(`Failed: ${(e as Error).message}`);
    }
  };

  return (
    <section className="sidebar-section">
      <h3 className="sidebar-section-title">Order Ticket</h3>

      <div className="ticket-block">
        {spread && (spread.kind === "PCS" || spread.kind === "CCS") && spread.short_strike != null && spread.long_strike != null && (
          <div className="ticket-spread-banner">
            <div className="ticket-spread-kind">
              {spread.kind}: SELL {fmt(spread.short_strike, 0)} / BUY {fmt(spread.long_strike, 0)}
            </div>
            <div className="muted small">
              credit ~${fmt((spread.estimated_credit_per_contract ?? 0) / 100, 2)} ·
              max profit ${fmt(spread.max_profit_per_contract, 0)} ·
              max loss ${fmt(spread.max_loss_per_contract, 0)} ·
              BE ${fmt(spread.breakeven, 0)}
            </div>
            {spread.reasoning && <div className="muted small">{spread.reasoning}</div>}
          </div>
        )}
        <div className="ticket-main">
          BUY × <input
            type="number"
            value={qty}
            min={1}
            max={100}
            className="qty-input"
            onChange={(e) => {
              setQty(Number(e.target.value) || DEFAULT_QTY);
              setQtyUserOverridden(true);
            }}
          /> SPX {fmt(v.strike_rec, 0)}{side}
          {opt && opt.entry_limit != null && (
            <span className="ticket-option-entry">
              @ ${fmt(opt.entry_limit, 2)}
            </span>
          )}
        </div>

        <div className="muted small">
          expiry {lastIter.session_date ?? "—"} · 0DTE
          {opt && opt.delta != null && (
            <> · delta {fmt(opt.delta, 2)}</>
          )}
          {v.suggested_qty != null && qty !== v.suggested_qty && (
            <> · model suggests {v.suggested_qty}× (based on {v.confidence})</>
          )}
        </div>

        <div className="ticket-targets-grid">
          <div className="ticket-targets-col">
            <div className="ticket-targets-header">SPX target</div>
            <TargetCell label="T1" value={v.t1} suffix="" />
            <TargetCell label="T2" value={v.t2} suffix="" />
            <TargetCell label="T3" value={v.t3} suffix="" />
            <TargetCell label="STOP" value={v.stop} suffix="" danger />
          </div>
          {opt && (
            <div className="ticket-targets-col">
              <div className="ticket-targets-header">Option price</div>
              <TargetCell label="T1" value={opt.t1} prefix="$" />
              <TargetCell label="T2" value={opt.t2} prefix="$" />
              <TargetCell label="T3" value={opt.t3} prefix="$" />
              <TargetCell label="STOP" value={opt.stop} prefix="$" danger />
            </div>
          )}
        </div>

        <div className="ticket-actions">
          <button onClick={() => void handleCopy()}>
            {copied ? "✓ Copied" : "Copy to clipboard"}
          </button>
          <button
            onClick={() => void handleSendToIBKR()}
            disabled={sending || !opt}
            title={opt ? "Place bracket order via IBKR (requires FEF_LIVE_TRADING=true)" : "No option pricing available"}
            style={{ background: "var(--color-blue)", color: "white", fontWeight: 600 }}
          >
            {sending ? "Sending…" : "📤 Send to IBKR"}
          </button>
          {ticketId !== null && !showOutcomeForm && (
            <button onClick={() => setShowOutcomeForm(true)}>Record outcome</button>
          )}
        </div>
        {sendResult && (
          <div className="small" style={{
            color: sendResult.ok ? "var(--color-green)" : "var(--color-red)",
            marginTop: 6, fontFamily: "var(--font-mono)",
          }}>
            {sendResult.ok ? "✓" : "✗"} {sendResult.message}
          </div>
        )}

        {outcomeStatus && (
          <div className="small" style={{ color: "var(--color-green)", marginTop: 6 }}>
            {outcomeStatus}
          </div>
        )}

        {showOutcomeForm && ticketId !== null && (
          <OutcomeForm onSubmit={submitOutcome} onCancel={() => setShowOutcomeForm(false)} />
        )}

        <details className="ticket-text-detail">
          <summary className="small muted">show order text</summary>
          <pre>{ticketText}</pre>
        </details>
      </div>
    </section>
  );
}

function OutcomeForm({
  onSubmit,
  onCancel,
}: {
  onSubmit: (fields: Record<string, number | string>) => void;
  onCancel: () => void;
}) {
  const [fillPrice, setFillPrice] = useState<string>("");
  const [exitT1, setExitT1] = useState<string>("");
  const [exitT2, setExitT2] = useState<string>("");
  const [exitT3, setExitT3] = useState<string>("");
  const [exitStop, setExitStop] = useState<string>("");
  const [pnl, setPnl] = useState<string>("");
  const [status, setStatus] = useState<string>("CLOSED");

  const handle = () => {
    const fields: Record<string, number | string> = { status };
    if (fillPrice) fields.fill_price = Number(fillPrice);
    if (exitT1) fields.exit_t1_price = Number(exitT1);
    if (exitT2) fields.exit_t2_price = Number(exitT2);
    if (exitT3) fields.exit_t3_price = Number(exitT3);
    if (exitStop) fields.exit_stop_price = Number(exitStop);
    if (pnl) fields.pnl_dollars = Number(pnl);
    onSubmit(fields);
  };

  return (
    <div className="outcome-form">
      <Row label="Fill" value={fillPrice} setValue={setFillPrice} />
      <Row label="T1 exit" value={exitT1} setValue={setExitT1} />
      <Row label="T2 exit" value={exitT2} setValue={setExitT2} />
      <Row label="T3 exit" value={exitT3} setValue={setExitT3} />
      <Row label="Stop exit" value={exitStop} setValue={setExitStop} />
      <Row label="Net P/L $" value={pnl} setValue={setPnl} />
      <div className="outcome-row">
        <span className="ticket-label">Status</span>
        <select value={status} onChange={(e) => setStatus(e.target.value)} className="settings-input">
          <option>CLOSED</option>
          <option>STOPPED</option>
          <option>PARTIAL</option>
        </select>
      </div>
      <div className="outcome-actions">
        <button onClick={handle}>Save</button>
        <button onClick={onCancel}>Cancel</button>
      </div>
    </div>
  );
}

function Row({
  label,
  value,
  setValue,
}: {
  label: string;
  value: string;
  setValue: (v: string) => void;
}) {
  return (
    <div className="outcome-row">
      <span className="ticket-label">{label}</span>
      <input
        type="number"
        step={0.01}
        value={value}
        onChange={(e) => setValue(e.target.value)}
        className="settings-input"
      />
    </div>
  );
}

function TargetCell({
  label,
  value,
  prefix = "",
  suffix = "",
  danger = false,
}: {
  label: string;
  value: number | null | undefined;
  prefix?: string;
  suffix?: string;
  danger?: boolean;
}) {
  if (value == null || !Number.isFinite(value)) return null;
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

/** Defensive number formatter — returns "—" for null/undefined/NaN/Infinity. */
function fmt(value: number | null | undefined, digits: number): string {
  if (value == null || !Number.isFinite(value)) return "—";
  return value.toFixed(digits);
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

/**
 * Credit-spread auto-trader control.
 *
 * WHY THIS IS A SEPARATE PANEL FROM AutoTradePanel: they are two different
 * bots. The directional bot buys 0DTE options on a verdict; this one sells a
 * put vertical on a structure. They share nothing but the account, and folding
 * this into that panel's preset/floor/cooldown machinery would imply a
 * relationship that does not exist.
 *
 * Until 2026-08-17 this module had NO api surface at all — it could only be
 * armed by restarting the process with FEF_ALLOW_CREDIT_SPREADS set, and there
 * was no way to ask why it had not fired. On 2026-08-12 that combination cost
 * an hour of hand-querying signal_log.db, and the answer turned out to be that
 * an order our own gate had refused had silently consumed the day's
 * once-per-session budget.
 *
 * TWO LEVELS, DELIBERATELY:
 *   env FEF_ALLOW_CREDIT_SPREADS = CAPABILITY ("this machine may sell an
 *     uncovered short leg"). Set at launch, not clickable.
 *   this toggle              = INTENT ("do it today"). Per session.
 * That is exactly how the directional bot behaves, and the asymmetry it
 * removes is that this module used to be armed the instant the process
 * started.
 */
import { useEffect, useState } from "react";
import { useToast } from "../components/Toast";

const API_BASE = "http://127.0.0.1:8765";

interface SpreadStatus {
  enabled: boolean;
  observe_only: boolean;
  would_transmit: boolean;
  credit_spread_env: boolean;
  live_trading_env: boolean;
  qty: number;
  width: number | null;
  delta_max: number | null;
  delta_floor_frac: number | null;
  min_credit_width_ratio: number | null;
  puts_only: boolean | null;
  require_gate: boolean | null;
  rec_max_age_sec: number | null;
  last_placement_date: string;
  count_session: number;
  last_reason: string;
  last_reason_code: string;
  last_eval_at_ms: number;
  has_recommendation: boolean;
}

export function SpreadAutoTradePanel() {
  const [status, setStatus] = useState<SpreadStatus | null>(null);
  const [busy, setBusy] = useState(false);
  const toast = useToast();

  const refresh = async () => {
    try {
      const r = await fetch(`${API_BASE}/spread_auto_trade/status`);
      if (r.ok) setStatus(await r.json());
    } catch {}
  };

  useEffect(() => {
    void refresh();
    const id = window.setInterval(refresh, 3000);
    return () => window.clearInterval(id);
  }, []);

  if (!status) return <div className="muted small">Loading spread auto-trade…</div>;

  // The capability layer. Without it the broker call refuses regardless of
  // this toggle, so the button is disabled rather than offering an arm that
  // cannot work — that combination is precisely what made 8/12 confusing.
  const canArm = status.credit_spread_env && status.live_trading_env;

  const handleToggle = async () => {
    const turningOn = !status.enabled;
    if (turningOn) {
      const ok = window.confirm(
        `Enable the CREDIT-SPREAD auto-trader?\n\n` +
        `It will sell ONE ${status.width ?? "?"}-wide SPX put vertical per session, ` +
        `short strike nearest ${status.delta_max ?? "?"} delta, ${status.qty} contract(s), ` +
        `held to expiry.\n\n` +
        `The short leg is UNCOVERED until the long wing fills.\n` +
        `Account is PAPER (DU-prefixed; enforced in code, not by this dialog).\n\n` +
        `Proceed?`
      );
      if (!ok) return;
    }
    setBusy(true);
    try {
      const r = await fetch(`${API_BASE}/spread_auto_trade/toggle`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ enabled: turningOn, confirm: true }),
      });
      const data = await r.json();
      if (r.ok) {
        toast.show(turningOn ? "CREDIT SPREADS ENABLED" : "Credit spreads disabled",
                   { variant: turningOn ? "warn" : "success" });
        await refresh();
      } else {
        toast.show(`Toggle failed: ${data.detail || "unknown"}`, { variant: "error" });
      }
    } catch (e) {
      toast.show(`Toggle failed: ${(e as Error).message}`, { variant: "error" });
    } finally {
      setBusy(false);
    }
  };

  const setObserve = async (observe: boolean) => {
    setBusy(true);
    try {
      await fetch(`${API_BASE}/spread_auto_trade/toggle`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ enabled: status.enabled, confirm: true, observe_only: observe }),
      });
      await refresh();
    } catch {} finally { setBusy(false); }
  };

  const evalAge = status.last_eval_at_ms
    ? Math.round((Date.now() - status.last_eval_at_ms) / 1000)
    : null;

  return (
    <div>
      <div style={{ marginBottom: 8 }}>
        <div style={{ fontWeight: 700, fontSize: 13 }}>
          Credit spreads{" "}
          <span style={{
            color: status.would_transmit ? "var(--color-red)" : "var(--color-text-muted)",
            fontWeight: 700,
          }}>
            {status.would_transmit ? "● ARMED" : status.enabled ? "● ON (not transmitting)" : "○ OFF"}
          </span>
        </div>
        <div className="muted small">
          {status.would_transmit
            ? `One ${status.width ?? "?"}-wide put vertical per session at ~${status.delta_max ?? "?"}Δ, ${status.qty} ct, held to expiry`
            : status.enabled
              ? "Enabled, but a layer below is off — see below; nothing will be sent"
              : canArm
                ? "Toggle ON to start selling one put vertical per session"
                : "Relaunch with -CreditSpreads to make this available"}
        </div>
      </div>

      {!canArm && (
        <div style={{
          background: "var(--color-bg-elevated)",
          borderLeft: "3px solid var(--color-red)",
          padding: "6px 10px", marginBottom: 8, fontSize: 11,
        }}>
          <strong style={{ color: "var(--color-red)" }}>Button disabled.</strong>{" "}
          {!status.credit_spread_env && <>The sidecar was launched without{" "}
            <code>FEF_ALLOW_CREDIT_SPREADS</code>, so the uncovered short leg would be
            refused at the broker. </>}
          {!status.live_trading_env && <>The sidecar was launched without{" "}
            <code>FEF_LIVE_TRADING</code>, so spreads would stay DRY_RUN. </>}
          Close the app and relaunch via <code>FEF-Paper-AutoTrade.bat</code>.
        </div>
      )}

      {status.enabled && status.observe_only && (
        <div style={{
          background: "var(--color-bg-elevated)",
          borderLeft: "3px solid var(--color-amber)",
          padding: "6px 10px", marginBottom: 8, fontSize: 11,
        }}>
          <strong>Observe-only.</strong> Every gate runs against live data and the
          intent is recorded, but nothing is transmitted.
        </div>
      )}

      <button
        onClick={() => void handleToggle()}
        disabled={busy || (!canArm && !status.enabled)}
        className={status.enabled ? "kill-switch-button" : ""}
        style={{
          width: "100%", marginBottom: 8,
          background: !canArm && !status.enabled
            ? "var(--color-bg-elevated)"
            : status.enabled ? "var(--color-red)" : "var(--color-green)",
          color: !canArm && !status.enabled ? "var(--color-text-muted)" : "white",
          fontWeight: 700, padding: 10, fontSize: 13,
          cursor: !canArm && !status.enabled ? "not-allowed" : "pointer",
        }}
      >
        {busy ? "…" :
         !canArm && !status.enabled ? "ENABLE CREDIT SPREADS (launch flag required)" :
         status.enabled ? "🛑 DISABLE CREDIT SPREADS" : "▶ ENABLE CREDIT SPREADS"}
      </button>

      <label className="small" style={{ display: "block", marginBottom: 8, cursor: "pointer" }}>
        <input
          type="checkbox"
          checked={status.observe_only}
          disabled={busy}
          onChange={(e) => void setObserve(e.target.checked)}
          style={{ marginRight: 6 }}
        />
        Observe only (run every gate, transmit nothing)
      </label>

      {/* WHY IT DID OR DID NOT FIRE. The reason this panel exists. */}
      <div style={{
        background: "var(--color-bg-elevated)", padding: "6px 10px",
        marginBottom: 8, fontSize: 11,
      }}>
        <div style={{ fontWeight: 700, marginBottom: 2 }}>Last evaluation</div>
        {status.last_reason_code ? (
          <>
            <div>
              <code>{status.last_reason_code}</code>
              {evalAge !== null && <span className="muted"> · {evalAge}s ago</span>}
            </div>
            <div className="muted" style={{ marginTop: 2 }}>{status.last_reason}</div>
          </>
        ) : (
          <div className="muted">No evaluation yet this session.</div>
        )}
      </div>

      <div className="small muted" style={{ lineHeight: 1.6 }}>
        <div>Placed this session: <strong>{status.count_session}</strong>
          {status.last_placement_date && <> · last {status.last_placement_date}</>}</div>
        <div>Structure: {status.width ?? "?"}-wide · ≤{status.delta_max ?? "?"}Δ
          {status.delta_floor_frac != null && status.delta_max != null &&
            <> · floor {(status.delta_max * status.delta_floor_frac).toFixed(3)}Δ</>}
          {status.min_credit_width_ratio != null && status.width != null &&
            <> · min credit ${(status.width * status.min_credit_width_ratio).toFixed(2)}</>}
        </div>
        <div>{status.puts_only ? "Puts only" : "Puts + calls"}
          {" · "}{status.require_gate ? "TA gate ON" : "ungated (sell every session)"}
          {" · "}rec max age {status.rec_max_age_sec ?? "?"}s</div>
        <div>Recommendation available: {status.has_recommendation ? "yes" : "no"}</div>
      </div>
    </div>
  );
}

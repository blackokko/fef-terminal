import { useEffect, useState } from "react";
import { useToast } from "../components/Toast";
import { NumberSetting } from "../components/NumberSetting";

const API_BASE = "http://127.0.0.1:8765";
const SCANNER_SYMBOLS = ["SPX", "SPY", "QQQ", "IWM", "GLD", "SMH", "TLT"];

interface ScannerVerdict {
  direction: string;
  confidence: string;
  reasoning: string;
  spx_price: number;
  strike_rec: number | null;
  timestamp_ms: number;
}

interface ScannerStatus {
  enabled: Record<string, boolean>;
  max_per_symbol: number;
  max_per_session_global: number;
  implied_global_cap?: number;
  count_global: number;
  count_by_symbol: Record<string, number>;
  cool_down_sec: number;
  cool_remaining_by_symbol: Record<string, number>;
  last_verdict_by_symbol: Record<string, ScannerVerdict>;
}

const DIRECTION_COLOR: Record<string, string> = {
  LONG_CALLS: "var(--color-green)",
  LONG_PUTS: "var(--color-red)",
  WAIT: "var(--color-text-muted)",
};

const CONF_COLOR: Record<string, string> = {
  HIGH: "var(--color-green)",
  MEDIUM: "var(--color-amber)",
  LOW: "var(--color-text-muted)",
  NONE: "var(--color-text-muted)",
};

export function ScannerPanel() {
  const [status, setStatus] = useState<ScannerStatus | null>(null);
  const [busy, setBusy] = useState(false);
  const toast = useToast();

  const refresh = async () => {
    try {
      const r = await fetch(`${API_BASE}/scanner/symbols`);
      if (r.ok) setStatus(await r.json());
    } catch {}
  };

  useEffect(() => {
    void refresh();
    const id = window.setInterval(refresh, 3000);
    return () => window.clearInterval(id);
  }, []);

  const toggle = async (sym: string) => {
    if (!status) return;
    const next = !status.enabled[sym];
    setBusy(true);
    try {
      const r = await fetch(`${API_BASE}/scanner/symbols`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ enabled: { [sym]: next } }),
      });
      if (r.ok) {
        toast.show(`${sym} scanner ${next ? "ON" : "OFF"}`,
                   { variant: next ? "warn" : "success" });
        await refresh();
      }
    } catch {} finally { setBusy(false); }
  };

  const updateMaxPerSymbol = async (n: number) => {
    setBusy(true);
    try {
      await fetch(`${API_BASE}/scanner/symbols`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ max_per_symbol: n }),
      });
      await refresh();
    } catch {} finally { setBusy(false); }
  };

  if (!status) {
    return (
      <section className="sidebar-section">
        <h3 className="sidebar-section-title">Multi-symbol scanner</h3>
        <p className="muted small">loading…</p>
      </section>
    );
  }

  return (
    <section className="sidebar-section">
      <h3 className="sidebar-section-title">Multi-symbol scanner</h3>

      <div className="muted small" style={{ marginBottom: 6 }}>
        Per-symbol cap: <NumberSetting
          min={1} max={50}
          value={status.max_per_symbol}
          onCommit={(n) => void updateMaxPerSymbol(n)}
          style={{ width: 50, margin: "0 4px" }}
        /> ·
        total budget: <strong>{status.count_global}/{status.max_per_session_global}</strong>
        <span className="muted small" style={{ marginLeft: 4 }}>
          (auto = cap × enabled symbols)
        </span>
      </div>

      <table style={{ width: "100%", fontSize: 11, fontFamily: "var(--font-mono)" }}>
        <thead>
          <tr style={{ color: "var(--color-text-muted)", textAlign: "left" }}>
            <th style={{ width: 50 }}>Sym</th>
            <th style={{ width: 32 }}>On</th>
            <th>Verdict</th>
            <th style={{ width: 50, textAlign: "right" }}>Count</th>
            <th style={{ width: 60, textAlign: "right" }}>CD</th>
          </tr>
        </thead>
        <tbody>
          {SCANNER_SYMBOLS.map(sym => {
            const enabled = status.enabled[sym] ?? false;
            const verdict = status.last_verdict_by_symbol[sym];
            const count = status.count_by_symbol[sym] ?? 0;
            const cd = status.cool_remaining_by_symbol[sym] ?? 0;
            const capped = count >= status.max_per_symbol;
            return (
              <tr key={sym} style={{
                opacity: enabled ? 1 : 0.5,
                borderTop: "1px solid var(--color-border)",
              }}>
                <td style={{ padding: "4px 0", fontWeight: 600 }}>{sym}</td>
                <td>
                  <input
                    type="checkbox"
                    checked={enabled}
                    onChange={() => void toggle(sym)}
                    disabled={busy}
                  />
                </td>
                <td>
                  {verdict ? (
                    <>
                      <span style={{ color: DIRECTION_COLOR[verdict.direction] ?? "var(--color-text)" }}>
                        {verdict.direction}
                      </span>{" "}
                      <span style={{ color: CONF_COLOR[verdict.confidence] ?? "var(--color-text-muted)" }}>
                        {verdict.confidence}
                      </span>
                      {verdict.strike_rec != null && verdict.direction !== "WAIT" && (
                        <span className="muted small"> · {verdict.strike_rec.toFixed(0)}</span>
                      )}
                    </>
                  ) : (
                    <span className="muted small">no scan yet</span>
                  )}
                </td>
                <td style={{ textAlign: "right",
                             color: capped ? "var(--color-red)" : undefined }}>
                  {count}/{status.max_per_symbol}
                </td>
                <td style={{ textAlign: "right" }}>
                  {cd > 0 ? (
                    <span style={{ color: "var(--color-amber)" }}>{cd}s</span>
                  ) : (
                    <span className="muted small">—</span>
                  )}
                </td>
              </tr>
            );
          })}
        </tbody>
      </table>

      <div className="muted small" style={{ marginTop: 6 }}>
        Auto-trade requires the global Auto-trade panel toggle to be ON.
        Each symbol has independent cool-down ({status.cool_down_sec}s).
      </div>
    </section>
  );
}

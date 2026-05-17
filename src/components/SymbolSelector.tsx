import { useEffect, useState } from "react";

// Switches the active symbol on the sidecar — triggers a respawn of all
// per-symbol data loops on the backend. Brief gap (~1s) while loops cycle.

const API_BASE = "http://127.0.0.1:8765";

interface Cfg {
  active_symbol: string;
  available_symbols: string[];
}

export function SymbolSelector() {
  const [cfg, setCfg] = useState<Cfg | null>(null);
  const [busy, setBusy] = useState(false);

  useEffect(() => {
    void (async () => {
      try {
        const r = await fetch(`${API_BASE}/config/symbol`);
        if (r.ok) setCfg(await r.json());
      } catch {
        // sidecar not up yet
      }
    })();
  }, []);

  if (!cfg) return null;

  const handleChange = async (symbol: string) => {
    if (symbol === cfg.active_symbol || busy) return;
    setBusy(true);
    try {
      const r = await fetch(`${API_BASE}/config/symbol`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ symbol }),
      });
      if (r.ok) {
        const data = await r.json();
        setCfg((prev) => (prev ? { ...prev, active_symbol: data.active_symbol } : prev));
        // Hard-reload so every component (chart, sidecar context, panels) re-reads
        // state from the freshly-restarted backend loops. ~100ms gap.
        setTimeout(() => window.location.reload(), 300);
      }
    } finally {
      setBusy(false);
    }
  };

  return (
    <div className="topbar-section symbol-selector">
      <span className="topbar-label">SYMBOL</span>
      <select
        value={cfg.active_symbol}
        onChange={(e) => void handleChange(e.target.value)}
        disabled={busy}
        className="symbol-select"
        title={busy ? "switching…" : "Active symbol — drives chart, FEF engine, microstructure"}
      >
        {cfg.available_symbols.map((s) => (
          <option key={s} value={s}>{s}</option>
        ))}
      </select>
    </div>
  );
}

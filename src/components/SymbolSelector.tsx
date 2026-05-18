import { useEffect, useState } from "react";
import { useSidecarSetters } from "../context/WebSocketContext";

// Switches the active symbol on the sidecar — triggers a respawn of all
// per-symbol data loops on the backend. ~1s gap while loops cycle.
// Updates SidecarContext directly so the chart + every panel reading
// activeSymbol re-renders immediately (no full window reload — which
// was unreliable in the Tauri webview and left activeSymbol stuck).

const API_BASE = "http://127.0.0.1:8765";

interface Cfg {
  active_symbol: string;
  available_symbols: string[];
}

export function SymbolSelector() {
  const [cfg, setCfg] = useState<Cfg | null>(null);
  const [busy, setBusy] = useState(false);
  const { setActiveSymbol } = useSidecarSetters();

  useEffect(() => {
    void (async () => {
      try {
        const r = await fetch(`${API_BASE}/config/symbol`);
        if (r.ok) {
          const data = await r.json();
          setCfg(data);
          setActiveSymbol(data.active_symbol);
        }
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
        // Propagate to SidecarContext immediately so chart + LiveQuote +
        // panels re-render with new symbol without needing a page reload.
        setActiveSymbol(data.active_symbol);
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

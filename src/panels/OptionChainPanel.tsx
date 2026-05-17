import { useEffect, useState } from "react";
import { useSidecar } from "../context/WebSocketContext";

// IMPROVEMENTS #D: full option chain ladder near spot, not just the rec strike.

interface ChainRow {
  strike: number;
  right: "C" | "P";
  last: number;
  bid: number | null;
  ask: number | null;
  delta: number | null;
}

interface ChainResponse {
  spx_price: number;
  expiry: string;
  calls: ChainRow[];
  puts: ChainRow[];
}

const API_BASE = "http://127.0.0.1:8765";

export function OptionChainPanel() {
  const { lastTick, lastIter } = useSidecar();
  const [chain, setChain] = useState<ChainResponse | null>(null);
  const [loading, setLoading] = useState(false);
  const [strikeRange, setStrikeRange] = useState(20);
  const spx = lastTick?.spx_price ?? lastIter?.spx_price ?? 0;

  const reload = async () => {
    setLoading(true);
    try {
      const r = await fetch(`${API_BASE}/option_chain?strike_range=${strikeRange}`);
      if (r.ok) setChain(await r.json());
    } finally {
      setLoading(false);
    }
  };

  useEffect(() => {
    // Don't auto-fetch on mount — chain query is expensive (N strike fetches)
  }, []);

  return (
    <details className="sidebar-section">
      <summary className="sidebar-section-title">Option chain ladder</summary>
      <div className="chain-body">
        <div className="chain-controls">
          <label className="small muted">±strikes</label>
          <input
            type="number"
            value={strikeRange}
            onChange={(e) => setStrikeRange(Number(e.target.value))}
            className="settings-input"
            min={5}
            max={100}
            step={5}
            style={{ width: 60 }}
          />
          <button onClick={() => void reload()} disabled={loading}>
            {loading ? "Loading…" : "Fetch chain"}
          </button>
        </div>

        {chain && (
          <div className="chain-grid">
            <div className="chain-header">
              <span>CALL</span>
              <span>Strike</span>
              <span>PUT</span>
            </div>
            {chain.calls.map((c, idx) => {
              const p = chain.puts[idx];
              const isATM = Math.abs(c.strike - spx) < 3;
              return (
                <div key={c.strike} className={`chain-row ${isATM ? "chain-atm" : ""}`}>
                  <span className="chain-cell chain-call">
                    {c.last.toFixed(2)}
                    {c.delta != null && <span className="muted small"> Δ{c.delta.toFixed(2)}</span>}
                  </span>
                  <span className="chain-strike">{c.strike.toFixed(0)}</span>
                  <span className="chain-cell chain-put">
                    {p ? p.last.toFixed(2) : "—"}
                    {p?.delta != null && <span className="muted small"> Δ{p.delta.toFixed(2)}</span>}
                  </span>
                </div>
              );
            })}
          </div>
        )}
      </div>
    </details>
  );
}

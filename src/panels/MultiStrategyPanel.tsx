import { useEffect, useState } from "react";

const API_BASE = "http://127.0.0.1:8765";

interface StrategyConfig {
  name: string;
  description: string;
  wall_proximity_pts: number;
  wall_collapse_pct: number;
  wall_collapse_floor_m: number;
  absorption_fe_ratio_min: number;
  base_qty: number;
}

export function MultiStrategyPanel() {
  const [configs, setConfigs] = useState<StrategyConfig[]>([]);

  useEffect(() => {
    let cancelled = false;
    void (async () => {
      try {
        const r = await fetch(`${API_BASE}/multi_strategy/configs`);
        if (r.ok && !cancelled) setConfigs(await r.json());
      } catch {}
    })();
    return () => { cancelled = true; };
  }, []);

  if (configs.length === 0) {
    return (
      <section className="sidebar-section">
        <h3 className="sidebar-section-title">Multi-strategy roster</h3>
        <p className="muted small">loading...</p>
      </section>
    );
  }

  return (
    <section className="sidebar-section">
      <h3 className="sidebar-section-title">Multi-strategy roster ({configs.length})</h3>
      <p className="muted small" style={{ marginBottom: 8 }}>
        Each runs in parallel via the tournament (capital allocation = future work)
      </p>
      <ul className="multi-strat-list">
        {configs.map(s => (
          <li key={s.name} className="multi-strat-row">
            <div>
              <div style={{ fontWeight: 600 }}>{s.name}</div>
              <div className="muted small">{s.description}</div>
            </div>
            <div className="multi-strat-params">
              <span title="wall_proximity_pts">prox {s.wall_proximity_pts}</span>
              <span title="wall_collapse_pct">coll {(s.wall_collapse_pct * 100).toFixed(0)}%</span>
              <span title="absorption_fe_ratio_min">abs {s.absorption_fe_ratio_min}</span>
              <span title="base_qty">×{s.base_qty}</span>
            </div>
          </li>
        ))}
      </ul>
    </section>
  );
}

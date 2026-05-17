import { useEffect, useState } from "react";
import { useSidecar } from "../context/WebSocketContext";

// Tournament mode panel — shows what each strategy variant would fire on
// the current iter. Consensus row at top tells you "4 of 5 agree LONG_CALLS"
// or "all split — no edge".

interface Variant {
  name: string;
  direction: string;
  confidence: string;
  reasoning: string;
  aligned: boolean;
  absorption: boolean;
}

interface TournamentSnapshot {
  iter_num: number;
  spx_price: number;
  variants: Variant[];
  consensus: {
    n: number;
    long_calls: number;
    long_puts: number;
    wait: number;
    high_confidence: number;
    aligned: number;
    consensus_direction: string;
    consensus_strength: number;
  };
}

const DIRECTION_COLOR: Record<string, string> = {
  LONG_CALLS: "var(--color-green)",
  LONG_PUTS: "var(--color-red)",
  WAIT: "var(--color-text-muted)",
  MIXED: "var(--color-amber)",
};

const CONFIDENCE_BADGE: Record<string, string> = {
  HIGH: "var(--color-green)",
  MEDIUM: "var(--color-amber)",
  LOW: "var(--color-text-muted)",
  NONE: "transparent",
};

export function TournamentPanel() {
  // Pull tournament events off the WS stream directly via a small subscription
  const [tournament, setTournament] = useState<TournamentSnapshot | null>(null);
  const { lastIter } = useSidecar();

  useEffect(() => {
    // Listen to the raw WS — re-using SidecarProvider but TournamentEvent
    // isn't tracked there. Cheap hack: poll the events via a custom WS here.
    let ws: WebSocket | null = null;
    const connect = () => {
      ws = new WebSocket("ws://127.0.0.1:8765/ws");
      ws.onmessage = (e) => {
        try {
          const parsed = JSON.parse(e.data);
          if (parsed.type === "tournament") setTournament(parsed);
        } catch {}
      };
    };
    connect();
    return () => ws?.close();
    // re-eval on iter so we stay subscribed
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [lastIter?.iter_num != null]);

  if (!tournament) {
    return (
      <section className="sidebar-section">
        <h3 className="sidebar-section-title">Strategy tournament</h3>
        <p className="muted small">waiting for first iter…</p>
      </section>
    );
  }

  const c = tournament.consensus;
  const consColor = DIRECTION_COLOR[c.consensus_direction] ?? "var(--color-text-muted)";

  return (
    <section className="sidebar-section">
      <h3 className="sidebar-section-title">
        Strategy tournament · iter #{tournament.iter_num}
      </h3>

      <div className="tournament-consensus" style={{ borderLeft: `3px solid ${consColor}` }}>
        <div className="tournament-consensus-direction" style={{ color: consColor }}>
          {c.consensus_direction}
        </div>
        <div className="tournament-consensus-stats muted small">
          {c.consensus_strength}% consensus · {c.long_calls}C / {c.long_puts}P / {c.wait}W · {c.high_confidence} HIGH
        </div>
      </div>

      <ul className="tournament-variants">
        {tournament.variants.map((v) => (
          <li key={v.name} className="tournament-variant-row">
            <span className="tournament-variant-name">{v.name}</span>
            <span
              className="tournament-variant-direction"
              style={{ color: DIRECTION_COLOR[v.direction] }}
            >
              {v.direction === "LONG_CALLS" ? "▲ CALLS" : v.direction === "LONG_PUTS" ? "▼ PUTS" : "—"}
            </span>
            <span
              className="tournament-variant-conf"
              style={{
                background: CONFIDENCE_BADGE[v.confidence],
                color: v.confidence === "HIGH" ? "#0e1116" : "var(--color-text-muted)",
              }}
            >
              {v.confidence}
            </span>
          </li>
        ))}
      </ul>
    </section>
  );
}

// Definitions of every term shown in this terminal, in plain English and
// in the specific FEF-signaling context. Default-collapsed so it doesn't
// eat space; click the header to expand.

interface TermEntry {
  term: string;
  short: string;
  fefContext: string;
  positive?: string;
  negative?: string;
}

const TERMS: { section: string; entries: TermEntry[] }[] = [
  {
    section: "Greek exposures",
    entries: [
      {
        term: "GEX (Gamma Exposure)",
        short:
          "Net gamma that dealers are exposed to at each strike. Tells you how dealer hedging will dampen or amplify moves near that price.",
        fefContext:
          "Big GEX walls act as magnets or barriers. Spot near a dominant GEX wall = pin-prone — FEF skips trades within 5pts of one.",
        positive:
          "+net = call-side dominance at that strike → wall acts as a FLOOR (dealers buying to hedge as price falls).",
        negative:
          "−net = put-side dominance at that strike → wall acts as a CEILING.",
      },
      {
        term: "DEX (Delta Exposure)",
        short:
          "Net delta of all open options at each strike. Where the gravity of directional bets is concentrated.",
        fefContext:
          "DEX call walls = real support (dealers long calls hedge by selling SPX on rallies). DEX put walls = real resistance. Use the dominant DEX strike as the floor/ceiling reference for the day.",
        positive:
          "+net at strike = massive call wall (e.g., 5/15 $7,400 = +$1.34M net DEX). Defended hard.",
        negative:
          "−net at strike = massive put wall (e.g., 5/15 $7,500 = −$976K net DEX). Defended hard from above.",
      },
    ],
  },
  {
    section: "Order flow",
    entries: [
      {
        term: "Net Drift",
        short:
          "Cumulative session premium flow: dollars hitting calls minus dollars hitting puts, summed since open.",
        fefContext:
          "ΔDrift over the 3-iter window is the FEF model's primary 'effort' input. Combined with ΔPrice via the FE ratio.",
        positive: "Cumulative + = bullish flow building under price.",
        negative: "Cumulative − = bearish flow building.",
      },
      {
        term: "Aggression %",
        short:
          "What fraction of trades hit at or above the ask (aggressive buyers) vs at or below the bid (aggressive sellers).",
        fefContext:
          "Call aggression > put aggression = bull tape. Confirms or contradicts the drift bias.",
        positive: "Call aggr > put aggr = buyers chasing calls (bullish confirmation).",
        negative: "Put aggr > call aggr = buyers chasing puts (bearish confirmation).",
      },
    ],
  },
  {
    section: "Pin gravity",
    entries: [
      {
        term: "Max Pain (raw)",
        short:
          "Strike where total option holder losses are maximized at expiration. Price gravitates toward it.",
        fefContext:
          "⚠️ Raw chain MP includes far-OTM macro hedges (LEAPS, deep-OTM puts) and is often nonsense. On 5/15 it returned $7,000 while real pin was $7,410.",
      },
      {
        term: "NTM Max Pain",
        short:
          "Max pain recomputed only over strikes within $50 of spot. Filters out macro-hedge noise.",
        fefContext:
          "This is the number FEF actually uses for MP gravity. On 5/15 = $7,400, exactly where price camped. Late-session, NTM MP acts as a magnet — set T3 (last partial) near it on aligned trades.",
      },
    ],
  },
  {
    section: "FEF metrics",
    entries: [
      {
        term: "ΔPrice / ΔDrift",
        short:
          "Change in SPX price and cumulative net drift over the last 3 iters (default ~6 minutes).",
        fefContext:
          "Same sign + meaningful magnitude = 'aligned' signal. Opposing signs with high FE = 'absorption' setup.",
      },
      {
        term: "FE Ratio (Flow Effectiveness)",
        short: "|ΔDrift in $M| ÷ |ΔPrice in pts|. How much money pushed per point of move.",
        fefContext:
          "High FE = a lot of effort per move. With alignment = strong fire. With opposing direction = absorption (potential reversal).",
      },
      {
        term: "Alignment",
        short: "ΔPrice and ΔDrift moving the same direction with magnitude.",
        fefContext:
          "Required for a directional fire. Without alignment, no LONG_CALLS/LONG_PUTS verdict regardless of other factors.",
      },
      {
        term: "Absorption",
        short: "Drift pushing one way while price refuses to follow.",
        fefContext:
          "Often a reversal setup. FEF flags it but stays WAIT until price confirms with alignment in the drift direction.",
      },
      {
        term: "Wall proximity skip",
        short: "Spot within 5pts of the dominant nearby wall.",
        fefContext:
          "Hard skip — no trade regardless of other signals. Wait for a clean break or rejection.",
      },
      {
        term: "Wall collapse",
        short: "Dominant wall lost >20% of its exposure in a single iter.",
        fefContext:
          "Special HIGH-confidence trigger — fire in direction away from the collapsing wall (collapsing call wall above spot = expect upward break).",
      },
    ],
  },
];

export function GlossaryPanel() {
  return (
    <details className="sidebar-section glossary-panel">
      <summary className="sidebar-section-title glossary-summary">
        Definitions · what these numbers mean
      </summary>
      <div className="glossary-body">
        {TERMS.map((section) => (
          <div key={section.section} className="glossary-section">
            <div className="glossary-section-title">{section.section}</div>
            {section.entries.map((e) => (
              <div key={e.term} className="glossary-entry">
                <div className="glossary-term">{e.term}</div>
                <div className="glossary-short">{e.short}</div>
                <div className="glossary-context">{e.fefContext}</div>
                {e.positive && (
                  <div className="glossary-sign">
                    <span className="sign-pos">positive</span> {e.positive}
                  </div>
                )}
                {e.negative && (
                  <div className="glossary-sign">
                    <span className="sign-neg">negative</span> {e.negative}
                  </div>
                )}
              </div>
            ))}
          </div>
        ))}
      </div>
    </details>
  );
}

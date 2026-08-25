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

// [REMOVED 2026-08-25] Three groups of entries went out with the QuantData
// API and data subscription, which were CANCELLED:
//
//   "Greek exposures"  — GEX (Gamma Exposure), DEX (Delta Exposure)
//   "Pin gravity"      — Max Pain (raw), NTM Max Pain
//   "Aggression %"     — from the Order flow section
//
// All four numbers came from QuantData's exposure/max-pain/trade-side
// endpoints and nothing else, and every UI surface that displayed them (the
// QuantData panel, the chart's wall + max-pain price lines) was removed in the
// same pass. A glossary that defines readouts the terminal can no longer show
// teaches the operator to look for something that is not there. Restore these
// only alongside a replacement data source.
const TERMS: { section: string; entries: TermEntry[] }[] = [
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

import { useState, type ReactNode } from "react";
import { ErrorBoundary } from "./ErrorBoundary";
import { AccountScorecardPanel } from "../panels/AccountScorecardPanel";
import { AutoTradePanel } from "../panels/AutoTradePanel";
import { SpreadAutoTradePanel } from "../panels/SpreadAutoTradePanel";
import { CalendarPanel } from "../panels/CalendarPanel";
import { ScannerPanel } from "../panels/ScannerPanel";
import { VerdictTimelinePanel } from "../panels/VerdictTimelinePanel";
import { ModelEvolutionPanel } from "../panels/ModelEvolutionPanel";
import { StreamHealthPanel } from "../panels/StreamHealthPanel";
import { ExitStrategyPanel } from "../panels/ExitStrategyPanel";
import { CrossSymbolPanel } from "../panels/CrossSymbolPanel";
import { HFTPanel } from "../panels/HFTPanel";
import { LiveExecutionPanel } from "../panels/LiveExecutionPanel";
import { LivePositionsPanel } from "../panels/LivePositionsPanel";
import { LiveQuotePanel } from "../panels/LiveQuotePanel";
import { MicrostructurePanel } from "../panels/MicrostructurePanel";
import { FEFVerdictPanel } from "../panels/FEFVerdictPanel";
import { OptionChainPanel } from "../panels/OptionChainPanel";
import { OrderTicketPanel } from "../panels/OrderTicketPanel";
import { QuantDataPanel } from "../panels/QuantDataPanel";
import { RegimePanel } from "../panels/RegimePanel";
import { SignalLogPanel } from "../panels/SignalLogPanel";
import { AnalyticsPanel } from "../panels/AnalyticsPanel";

/**
 * Trading sidebar — only panels relevant to live trading.
 *
 * Removed sections (moved to dedicated screens, see AppShell):
 *  - Patterns / ML / Strategies / Tournament  → Research screen
 *  - WalkForward / Optimize / Replay          → Research screen
 *  - Settings / DataMaintenance / AuditLog
 *    / ChartAnnotations / Glossary / Calendar → Settings screen
 *  - Attribution                              → Research screen
 */
type Tab = "trade" | "data" | "history";

const TABS: { id: Tab; label: string }[] = [
  { id: "trade", label: "Trade" },
  { id: "data", label: "Data" },
  { id: "history", label: "History" },
];

function Safe({ name, children }: { name: string; children: ReactNode }) {
  return <ErrorBoundary label={name}>{children}</ErrorBoundary>;
}

export function Sidebar() {
  const [tab, setTab] = useState<Tab>("trade");

  return (
    <aside className="sidebar">
      <nav className="sidebar-tabs">
        {TABS.map((t) => (
          <button
            key={t.id}
            className={`sidebar-tab ${tab === t.id ? "active" : ""}`}
            onClick={() => setTab(t.id)}
          >
            {t.label}
          </button>
        ))}
      </nav>

      <div className="sidebar-tab-content">
        {tab === "trade" && (
          <>
            <Safe name="AccountScorecard"><AccountScorecardPanel /></Safe>
            <Safe name="Calendar"><CalendarPanel /></Safe>
            <Safe name="Regime"><RegimePanel /></Safe>
            <Safe name="LiveQuote"><LiveQuotePanel /></Safe>
            <Safe name="FEFVerdict"><FEFVerdictPanel /></Safe>
            <Safe name="HFT"><HFTPanel /></Safe>
            <Safe name="Microstructure"><MicrostructurePanel /></Safe>
            <Safe name="CrossSymbol"><CrossSymbolPanel /></Safe>
            <Safe name="OrderTicket"><OrderTicketPanel /></Safe>
            <Safe name="ExitStrategy"><ExitStrategyPanel /></Safe>
            {/* PhaseDetectorPanel retired from the sidebar 2026-07-06 — its
                post-capitulation logic now lives in the Market Regime panel's
                Expected-move tile. Code kept at panels/PhaseDetectorPanel.tsx. */}
            <Safe name="AutoTrade"><AutoTradePanel /></Safe>
            {/* Separate bot, separate switch (2026-08-17). The directional bot
                above buys 0DTE options on a verdict; this one sells a put
                vertical on a structure. Sitting adjacent so the two arming
                states are visible together — before this existed, the spread
                module could only be armed by relaunching the process. */}
            <Safe name="SpreadAutoTrade"><SpreadAutoTradePanel /></Safe>
            <Safe name="Scanner"><ScannerPanel /></Safe>
            <Safe name="LiveExecution"><LiveExecutionPanel /></Safe>
            <Safe name="LivePositions"><LivePositionsPanel /></Safe>
            <Safe name="OptionChain"><OptionChainPanel /></Safe>
          </>
        )}
        {tab === "data" && (
          <>
            <Safe name="QuantData"><QuantDataPanel /></Safe>
            <Safe name="SignalLog"><SignalLogPanel /></Safe>
          </>
        )}
        {tab === "history" && (
          <>
            <Safe name="VerdictTimeline"><VerdictTimelinePanel /></Safe>
            <Safe name="ModelEvolution"><ModelEvolutionPanel /></Safe>
            <Safe name="StreamHealth"><StreamHealthPanel /></Safe>
            <Safe name="Analytics"><AnalyticsPanel /></Safe>
          </>
        )}
      </div>
    </aside>
  );
}

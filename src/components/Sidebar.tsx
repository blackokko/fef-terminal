import { useState, type ReactNode } from "react";
import { ErrorBoundary } from "./ErrorBoundary";
import { AccountScorecardPanel } from "../panels/AccountScorecardPanel";
import { AttributionPanel } from "../panels/AttributionPanel";
import { AutoTradePanel } from "../panels/AutoTradePanel";
import { ScannerPanel } from "../panels/ScannerPanel";
import { TuningPanel } from "../panels/TuningPanel";
import { VerdictTimelinePanel } from "../panels/VerdictTimelinePanel";
import { ModelEvolutionPanel } from "../panels/ModelEvolutionPanel";
import { StreamHealthPanel } from "../panels/StreamHealthPanel";
import { AuditLogPanel } from "../panels/AuditLogPanel";
import { CalendarPanel } from "../panels/CalendarPanel";
import { DataMaintenancePanel } from "../panels/DataMaintenancePanel";
import { ExitStrategyPanel } from "../panels/ExitStrategyPanel";
import { CrossSymbolPanel } from "../panels/CrossSymbolPanel";
import { HFTPanel } from "../panels/HFTPanel";
import { LiveExecutionPanel } from "../panels/LiveExecutionPanel";
import { LivePositionsPanel } from "../panels/LivePositionsPanel";
import { LiveQuotePanel } from "../panels/LiveQuotePanel";
import { MLPanel } from "../panels/MLPanel";
import { MultiStrategyPanel } from "../panels/MultiStrategyPanel";
import { WalkForwardPanel } from "../panels/WalkForwardPanel";
import { ChartAnnotationsPanel } from "../panels/ChartAnnotationsPanel";
import { MicrostructurePanel } from "../panels/MicrostructurePanel";
import { OptimizePanel } from "../panels/OptimizePanel";
import { RiskOverlayPanel } from "../panels/RiskOverlayPanel";
import { TournamentPanel } from "../panels/TournamentPanel";
import { FEFVerdictPanel } from "../panels/FEFVerdictPanel";
import { GlossaryPanel } from "../panels/GlossaryPanel";
import { OptionChainPanel } from "../panels/OptionChainPanel";
import { OrderTicketPanel } from "../panels/OrderTicketPanel";
import { PatternLibraryPanel } from "../panels/PatternLibraryPanel";
import { QuantDataPanel } from "../panels/QuantDataPanel";
import { RegimePanel } from "../panels/RegimePanel";
import { ReplayPanel } from "../panels/ReplayPanel";
import { SettingsPanel } from "../panels/SettingsPanel";
import { SignalLogPanel } from "../panels/SignalLogPanel";
import { StrategyPresetsPanel } from "../panels/StrategyPresetsPanel";
import { AnalyticsPanel } from "../panels/AnalyticsPanel";

type Tab = "trade" | "data" | "patterns" | "history" | "config";

const TABS: { id: Tab; label: string }[] = [
  { id: "trade", label: "Trade" },
  { id: "data", label: "Data" },
  { id: "patterns", label: "Patterns" },
  { id: "history", label: "History" },
  { id: "config", label: "Config" },
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
            <Safe name="RiskOverlay"><RiskOverlayPanel /></Safe>
            <Safe name="Regime"><RegimePanel /></Safe>
            <Safe name="LiveQuote"><LiveQuotePanel /></Safe>
            <Safe name="FEFVerdict"><FEFVerdictPanel /></Safe>
            <Safe name="HFT"><HFTPanel /></Safe>
            <Safe name="Microstructure"><MicrostructurePanel /></Safe>
            <Safe name="CrossSymbol"><CrossSymbolPanel /></Safe>
            <Safe name="OrderTicket"><OrderTicketPanel /></Safe>
            <Safe name="ExitStrategy"><ExitStrategyPanel /></Safe>
            <Safe name="AutoTrade"><AutoTradePanel /></Safe>
            <Safe name="Scanner"><ScannerPanel /></Safe>
            <Safe name="Tuning"><TuningPanel /></Safe>
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
        {tab === "patterns" && (
          <>
            <Safe name="Tournament"><TournamentPanel /></Safe>
            <Safe name="MultiStrategy"><MultiStrategyPanel /></Safe>
            <Safe name="ML"><MLPanel /></Safe>
            <Safe name="PatternLibrary"><PatternLibraryPanel /></Safe>
            <Safe name="StrategyPresets"><StrategyPresetsPanel /></Safe>
          </>
        )}
        {tab === "history" && (
          <>
            <Safe name="VerdictTimeline"><VerdictTimelinePanel /></Safe>
            <Safe name="ModelEvolution"><ModelEvolutionPanel /></Safe>
            <Safe name="StreamHealth"><StreamHealthPanel /></Safe>
            <Safe name="Analytics"><AnalyticsPanel /></Safe>
            <Safe name="Attribution"><AttributionPanel /></Safe>
            <Safe name="WalkForward"><WalkForwardPanel /></Safe>
            <Safe name="Optimize"><OptimizePanel /></Safe>
            <Safe name="Replay"><ReplayPanel /></Safe>
          </>
        )}
        {tab === "config" && (
          <>
            <Safe name="Settings"><SettingsPanel /></Safe>
            <Safe name="DataMaintenance"><DataMaintenancePanel /></Safe>
            <Safe name="AuditLog"><AuditLogPanel /></Safe>
            <Safe name="ChartAnnotations"><ChartAnnotationsPanel /></Safe>
            <Safe name="Glossary"><GlossaryPanel /></Safe>
          </>
        )}
      </div>
    </aside>
  );
}

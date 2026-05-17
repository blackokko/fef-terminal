import { useState } from "react";
import { AccountScorecardPanel } from "../panels/AccountScorecardPanel";
import { ChartAnnotationsPanel } from "../panels/ChartAnnotationsPanel";
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
            <AccountScorecardPanel />
            <RegimePanel />
            <FEFVerdictPanel />
            <OrderTicketPanel />
            <OptionChainPanel />
          </>
        )}
        {tab === "data" && (
          <>
            <QuantDataPanel />
            <SignalLogPanel />
          </>
        )}
        {tab === "patterns" && (
          <>
            <PatternLibraryPanel />
            <StrategyPresetsPanel />
          </>
        )}
        {tab === "history" && (
          <>
            <AnalyticsPanel />
            <ReplayPanel />
          </>
        )}
        {tab === "config" && (
          <>
            <SettingsPanel />
            <ChartAnnotationsPanel />
            <GlossaryPanel />
          </>
        )}
      </div>
    </aside>
  );
}

import { ChartAnnotationsPanel } from "../panels/ChartAnnotationsPanel";
import { FEFVerdictPanel } from "../panels/FEFVerdictPanel";
import { GlossaryPanel } from "../panels/GlossaryPanel";
import { OptionChainPanel } from "../panels/OptionChainPanel";
import { OrderTicketPanel } from "../panels/OrderTicketPanel";
import { QuantDataPanel } from "../panels/QuantDataPanel";
import { ReplayPanel } from "../panels/ReplayPanel";
import { SettingsPanel } from "../panels/SettingsPanel";
import { SignalLogPanel } from "../panels/SignalLogPanel";
import { StrategyPresetsPanel } from "../panels/StrategyPresetsPanel";

export function Sidebar() {
  return (
    <aside className="sidebar">
      <FEFVerdictPanel />
      <OrderTicketPanel />
      <OptionChainPanel />
      <QuantDataPanel />
      <SignalLogPanel />
      <StrategyPresetsPanel />
      <ChartAnnotationsPanel />
      <ReplayPanel />
      <SettingsPanel />
      <GlossaryPanel />
    </aside>
  );
}

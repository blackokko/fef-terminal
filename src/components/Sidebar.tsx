import { FEFVerdictPanel } from "../panels/FEFVerdictPanel";
import { GlossaryPanel } from "../panels/GlossaryPanel";
import { OrderTicketPanel } from "../panels/OrderTicketPanel";
import { QuantDataPanel } from "../panels/QuantDataPanel";
import { SettingsPanel } from "../panels/SettingsPanel";
import { SignalLogPanel } from "../panels/SignalLogPanel";

export function Sidebar() {
  return (
    <aside className="sidebar">
      <FEFVerdictPanel />
      <OrderTicketPanel />
      <QuantDataPanel />
      <SignalLogPanel />
      <SettingsPanel />
      <GlossaryPanel />
    </aside>
  );
}

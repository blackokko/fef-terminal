import { FEFVerdictPanel } from "../panels/FEFVerdictPanel";
import { GlossaryPanel } from "../panels/GlossaryPanel";
import { OrderTicketPanel } from "../panels/OrderTicketPanel";
import { QuantDataPanel } from "../panels/QuantDataPanel";
import { ReplayPanel } from "../panels/ReplayPanel";
import { SettingsPanel } from "../panels/SettingsPanel";
import { SignalLogPanel } from "../panels/SignalLogPanel";

export function Sidebar() {
  return (
    <aside className="sidebar">
      <FEFVerdictPanel />
      <OrderTicketPanel />
      <QuantDataPanel />
      <SignalLogPanel />
      <ReplayPanel />
      <SettingsPanel />
      <GlossaryPanel />
    </aside>
  );
}

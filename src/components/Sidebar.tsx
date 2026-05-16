import { FEFVerdictPanel } from "../panels/FEFVerdictPanel";
import { OrderTicketPanel } from "../panels/OrderTicketPanel";
import { QuantDataPanel } from "../panels/QuantDataPanel";
import { SignalLogPanel } from "../panels/SignalLogPanel";

export function Sidebar() {
  return (
    <aside className="sidebar">
      <FEFVerdictPanel />
      <OrderTicketPanel />
      <QuantDataPanel />
      <SignalLogPanel />
    </aside>
  );
}

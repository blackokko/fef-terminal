import { useState } from "react";
import { TradingScreen } from "./TradingScreen";
import { ResearchScreen } from "./ResearchScreen";
import { SettingsScreen } from "./SettingsScreen";
import { TopBar } from "./TopBar";

export type Screen = "trading" | "research" | "settings";

export function AppShell() {
  const [screen, setScreen] = useState<Screen>("trading");
  return (
    <div className="app-shell">
      <TopBar screen={screen} onScreenChange={setScreen} />
      {screen === "trading" && <TradingScreen />}
      {screen === "research" && <ResearchScreen />}
      {screen === "settings" && <SettingsScreen />}
    </div>
  );
}

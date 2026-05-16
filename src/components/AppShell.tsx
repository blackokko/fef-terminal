import { Group, Panel, Separator } from "react-resizable-panels";
import { ChartPane } from "./ChartPane";
import { Sidebar } from "./Sidebar";
import { TopBar } from "./TopBar";

export function AppShell() {
  return (
    <div className="app-shell">
      <TopBar />
      <Group
        orientation="horizontal"
        className="main-split"
        defaultLayout={{ chart: 70, sidebar: 30 }}
      >
        <Panel id="chart" minSize={40} className="panel-chart">
          <ChartPane />
        </Panel>
        <Separator className="resize-handle" />
        <Panel id="sidebar" minSize={20} className="panel-sidebar">
          <Sidebar />
        </Panel>
      </Group>
    </div>
  );
}

import { AppShell } from "./components/AppShell";
import { ChartPane } from "./components/ChartPane";
import { SidecarProvider } from "./context/WebSocketContext";
import { isChartOnlyView } from "./lib/popoutChart";
import "./App.css";

function App() {
  const chartOnly = isChartOnlyView();
  return (
    <SidecarProvider>
      {chartOnly ? (
        <div className="chart-only-shell">
          <ChartPane />
        </div>
      ) : (
        <AppShell />
      )}
    </SidecarProvider>
  );
}

export default App;

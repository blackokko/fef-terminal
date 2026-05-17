import { AppShell } from "./components/AppShell";
import { ChartPane } from "./components/ChartPane";
import { ToastProvider } from "./components/Toast";
import { SidecarProvider } from "./context/WebSocketContext";
import { isChartOnlyView } from "./lib/popoutChart";
import "./App.css";

function App() {
  const chartOnly = isChartOnlyView();
  return (
    <ToastProvider>
      <SidecarProvider>
        {chartOnly ? (
          <div className="chart-only-shell">
            <ChartPane />
          </div>
        ) : (
          <AppShell />
        )}
      </SidecarProvider>
    </ToastProvider>
  );
}

export default App;

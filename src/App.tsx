import { AppShell } from "./components/AppShell";
import { ChartPane } from "./components/ChartPane";
import { ErrorBoundary } from "./components/ErrorBoundary";
import { ToastProvider } from "./components/Toast";
import { SidecarProvider } from "./context/WebSocketContext";
import { isChartOnlyView } from "./lib/popoutChart";
import "./App.css";

if (typeof window !== "undefined") {
  window.addEventListener("error", (e) => {
    // eslint-disable-next-line no-console
    console.error("[window.onerror]", e.message, e.filename, e.lineno, e.error);
  });
  window.addEventListener("unhandledrejection", (e) => {
    // eslint-disable-next-line no-console
    console.error("[unhandledrejection]", e.reason);
  });
}

function App() {
  const chartOnly = isChartOnlyView();
  return (
    <ErrorBoundary label="root">
      <ToastProvider>
        <SidecarProvider>
          {chartOnly ? (
            <div className="chart-only-shell">
              <ErrorBoundary label="chart"><ChartPane /></ErrorBoundary>
            </div>
          ) : (
            <ErrorBoundary label="shell"><AppShell /></ErrorBoundary>
          )}
        </SidecarProvider>
      </ToastProvider>
    </ErrorBoundary>
  );
}

export default App;

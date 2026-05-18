import { AppShell } from "./components/AppShell";
import { ChartPane } from "./components/ChartPane";
import { ErrorBoundary } from "./components/ErrorBoundary";
import { SymbolSelector } from "./components/SymbolSelector";
import { ToastProvider } from "./components/Toast";
import { SidecarProvider, useSidecar } from "./context/WebSocketContext";
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

function PopoutChartShell() {
  const { lastTick, activeSymbol } = useSidecar();
  const symbol = activeSymbol ?? "SPX";
  const price = lastTick?.spx_price;
  return (
    <div className="chart-only-shell">
      <header className="popout-chart-bar">
        <SymbolSelector />
        <div className="popout-chart-price">
          <span className="topbar-label">PRICE</span>
          <span className="topbar-price" style={{ marginLeft: 6 }}>
            {price != null ? price.toFixed(2) : "—"}
          </span>
        </div>
        <div className="popout-chart-symbol-current">
          {symbol}
        </div>
      </header>
      <div className="popout-chart-body">
        <ErrorBoundary label="chart"><ChartPane /></ErrorBoundary>
      </div>
    </div>
  );
}

function App() {
  const chartOnly = isChartOnlyView();
  return (
    <ErrorBoundary label="root">
      <ToastProvider>
        <SidecarProvider>
          {chartOnly ? (
            <PopoutChartShell />
          ) : (
            <ErrorBoundary label="shell"><AppShell /></ErrorBoundary>
          )}
        </SidecarProvider>
      </ToastProvider>
    </ErrorBoundary>
  );
}

export default App;

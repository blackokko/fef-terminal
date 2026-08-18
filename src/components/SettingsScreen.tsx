import { ErrorBoundary } from "./ErrorBoundary";
import { DataMaintenancePanel } from "../panels/DataMaintenancePanel";
import { AuditLogPanel } from "../panels/AuditLogPanel";
import { ChartAnnotationsPanel } from "../panels/ChartAnnotationsPanel";
import { GlossaryPanel } from "../panels/GlossaryPanel";
import { EngineThresholdsPanel } from "../panels/EngineThresholdsPanel";

/**
 * Settings screen — configuration, maintenance, reference. Two columns give
 * each panel more horizontal room than the cramped sidebar provided.
 *
 * 2026-07-30: threshold editing returned here as EngineThresholdsPanel. It
 * had been living in Research behind Strategy Presets, which was retired with
 * the rest of the Research teardown — the preset bundles came with a
 * tournament that ranked which one "would have won", and that ranking was
 * noise (the sweep behind it returned optimal wall-proximity 3, then 5, then
 * 8 on consecutive sessions). Editing thresholds is legitimate operator work
 * and belongs in Settings; being told which values won a backtest was the
 * part worth deleting.
 *
 * Calendar remains in the Trading sidebar, where economic events are more
 * visible during the session.
 */
export function SettingsScreen() {
  return (
    <div className="settings-screen">
      <header className="research-header">
        <h1>Settings &amp; Reference</h1>
        <p className="muted">
          Engine thresholds, data maintenance, audit log, chart annotations,
          glossary.
        </p>
      </header>

      <div className="settings-grid">
        <div className="settings-column">
          <ErrorBoundary label="EngineThresholds"><EngineThresholdsPanel /></ErrorBoundary>
          <ErrorBoundary label="ChartAnnotations"><ChartAnnotationsPanel /></ErrorBoundary>
          <ErrorBoundary label="Glossary"><GlossaryPanel /></ErrorBoundary>
        </div>
        <div className="settings-column">
          <ErrorBoundary label="DataMaintenance"><DataMaintenancePanel /></ErrorBoundary>
          <ErrorBoundary label="AuditLog"><AuditLogPanel /></ErrorBoundary>
        </div>
      </div>
    </div>
  );
}

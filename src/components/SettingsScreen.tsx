import { ErrorBoundary } from "./ErrorBoundary";
import { DataMaintenancePanel } from "../panels/DataMaintenancePanel";
import { AuditLogPanel } from "../panels/AuditLogPanel";
import { ChartAnnotationsPanel } from "../panels/ChartAnnotationsPanel";
import { GlossaryPanel } from "../panels/GlossaryPanel";

/**
 * Settings screen — maintenance + reference. Two columns give each panel
 * more horizontal room than the cramped sidebar provided.
 *
 * Previously included a SettingsPanel for threshold tuning + a Calendar
 * panel; the threshold panel is now exposed via Strategy Presets (in
 * Research), and Calendar lives in the Trading sidebar where economic
 * events are more visible during the session.
 */
export function SettingsScreen() {
  return (
    <div className="settings-screen">
      <header className="research-header">
        <h1>Settings &amp; Reference</h1>
        <p className="muted">
          Data maintenance, audit log, chart annotations, glossary.
        </p>
      </header>

      <div className="settings-grid">
        <div className="settings-column">
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

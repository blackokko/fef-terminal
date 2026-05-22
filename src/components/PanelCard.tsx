import { useState, type ReactNode } from "react";
import { ErrorBoundary } from "./ErrorBoundary";

/**
 * Card wrapper for panels on the Research screen. Provides an expandable
 * "What is this?" help block so each tool is self-explanatory.
 */
export interface PanelHelp {
  title: string;
  whatItDoes: string;
  useItWhen: string;
  expect: string;
  liveOnly?: boolean;
  note?: string;
  /** When in your workflow this tool belongs. Drives the badge color/text. */
  cadence?:
    | "start-here"        // do this today, before anything else
    | "daily-eod"         // end-of-day review
    | "daily-live"        // during market hours
    | "pre-market"        // morning prep
    | "weekly"            // weekly review
    | "after-more-data"   // wait until 10+ sessions accumulate
    | "post-freeze";      // gated until freeze counter elapses
}

const CADENCE_LABEL: Record<NonNullable<PanelHelp["cadence"]>, string> = {
  "start-here": "Start here today",
  "daily-eod": "Daily — end of day",
  "daily-live": "Live market hours",
  "pre-market": "Pre-market prep",
  "weekly": "Weekly review",
  "after-more-data": "After 10+ sessions",
  "post-freeze": "Post-freeze only",
};

const CADENCE_COLOR: Record<NonNullable<PanelHelp["cadence"]>, string> = {
  "start-here": "var(--color-green)",
  "daily-eod": "var(--color-accent)",
  "daily-live": "var(--color-amber)",
  "pre-market": "var(--color-accent)",
  "weekly": "var(--color-text-muted)",
  "after-more-data": "var(--color-text-muted)",
  "post-freeze": "var(--color-text-muted)",
};

export function PanelCard({ help, children }: { help: PanelHelp; children: ReactNode }) {
  const [expanded, setExpanded] = useState(false);
  return (
    <div className="research-panel-card">
      <header className="research-panel-header">
        <div className="research-panel-title-row">
          <h3 className="research-panel-title">{help.title}</h3>
          {help.cadence && (
            <span
              className="research-panel-cadence-badge"
              style={{
                background: CADENCE_COLOR[help.cadence],
                color: help.cadence === "daily-live" ? "var(--color-bg)" : "white",
              }}
            >
              {CADENCE_LABEL[help.cadence]}
            </span>
          )}
        </div>
        <button
          className="research-panel-help-toggle"
          onClick={() => setExpanded((e) => !e)}
        >
          {expanded ? "Hide help" : "What is this?"}
        </button>
      </header>
      {expanded && (
        <div className="research-panel-help">
          <p><strong>What it does:</strong> {help.whatItDoes}</p>
          <p><strong>Use it when:</strong> {help.useItWhen}</p>
          <p><strong>Expected output:</strong> {help.expect}</p>
          {help.note && <p className="research-panel-help-note"><em>{help.note}</em></p>}
          {help.liveOnly && (
            <p className="research-panel-help-warning">
              ⚠ Live data only — empty outside market hours.
            </p>
          )}
        </div>
      )}
      <ErrorBoundary label={help.title}>{children}</ErrorBoundary>
    </div>
  );
}

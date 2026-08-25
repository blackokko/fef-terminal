import { useState } from "react";
import { ErrorBoundary } from "./ErrorBoundary";
import { PanelCard } from "./PanelCard";
import { ReplayPanel } from "../panels/ReplayPanel";
import { EngineChangeLogPanel } from "../panels/EngineChangeLogPanel";

/**
 * Research screen — REDUCED 2026-07-30 from 12 panels to 2.
 *
 * WHAT HAPPENED
 * -------------
 * Nine panels were removed because they GENERATE strategy conclusions, and
 * every one of them had been found to generate wrong ones. The 2026-07-01
 * research-tab audit already flagged most of them (walk-forward/optimize
 * dollars 6x inflated and their "optimal" params noise; tournament winners
 * flipped by a +0.5R BE-stop bug; ML vacuous, inflated and lookahead-
 * contaminated; what-if discarding partials; tuning recs unweighted and
 * stale). Nothing was fixed in the intervening month, and the research done
 * since made the case worse rather than better.
 *
 * THE DEEPER REASON — this is the part worth reading before rebuilding any
 * of it. Roughly thirty pre-registered studies in the research archive
 * established that there is no mechanical 0DTE directional edge on the SPX
 * price series at tradeable strikes: the achievable edge over 1-60 minutes
 * is 0.08-0.35 index points against a 0.442-point friction floor. So these
 * panels were searching for something that is not there, and a search that
 * cannot succeed still returns a top row. Every one of them ranks its
 * results and shows you a winner.
 *
 * And the form is the problem, not just the implementations. What made the
 * archive's work trustworthy was a discipline layer: pre-registered locks
 * written before results, untradeable-vehicle controls, TRAIN/OOS splits,
 * BH-FDR across cells, trivial-baseline comparators, adversarial replication.
 * A UI button cannot enforce any of that. Clicking "Optimize" yields a number
 * with no lock file, no null, no control, and no record of how many cells
 * were searched. That is not a panel that is currently broken and could be
 * repaired — it is a panel whose shape is incompatible with a trustworthy
 * result.
 *
 * The concrete harm was observable: walk-forward's optimal `wall_proximity`
 * flipped 3 -> 5 -> 8 across consecutive sessions. That is noise, rendered in
 * the visual grammar of a recommendation.
 *
 * WHAT SURVIVED, AND WHY
 * ----------------------
 * The two panels here REPORT rather than INFER. Replay shows a session as it
 * happened. The engine change log lists what was applied and when. Neither
 * ranks anything, neither has a winner row, and neither can be overfit,
 * because neither is fitting.
 *
 * REMOVED (component files remain on disk, unreferenced, pending the deploy
 * window; their sidecar endpoints are untouched and still live):
 *   PresetTournamentPanel, StrategyPresetsPanel, MultiStrategyPanel,
 *   TournamentPanel, PatternLibraryPanel, MLPanel, CounterfactualPanel,
 *   WalkForwardPanel, OptimizePanel, TuningPanel (recommendations half;
 *   its engine-change log was extracted to EngineChangeLogPanel).
 *
 * Real strategy research lives in C:\Users\diant\fef-research-archive\ —
 * RESEARCH_REGISTRY.md is the index, and section K0 states what that work
 * can and cannot establish.
 */
export function ResearchScreen() {
  const [showWhy, setShowWhy] = useState(false);

  return (
    <div className="research-screen">
      <header className="research-header">
        <div className="research-header-row">
          <h1>Research</h1>
          <button
            className="research-quickstart-toggle"
            onClick={() => setShowWhy((s) => !s)}
          >
            {showWhy ? "Hide" : "Why is this screen nearly empty?"}
          </button>
        </div>
        <p className="muted">
          Session replay and the engine change log. Both read-only, both
          historical. Nothing here places trades, changes config, or ranks
          anything.
        </p>

        {showWhy && (
          <div className="research-quickstart">
            <h3>Nine panels were removed on 2026-07-30</h3>

            <p className="muted small">
              They generated strategy conclusions, and the conclusions were
              not reliable. The 2026-07-01 audit had already flagged most of
              them — walk-forward and optimize reporting dollars inflated
              roughly sixfold with unstable "optimal" parameters, tournament
              winners flipped by a break-even stop bug, the ML tab vacuous
              and lookahead-contaminated, what-if discarding partial exits,
              tuning recommendations unweighted and stale.
            </p>

            <p className="muted small">
              The deeper reason is that they were searching for something the
              research archive has since ruled out. Around thirty
              pre-registered studies put the achievable directional edge over
              1&ndash;60 minutes at 0.08&ndash;0.35 index points against a
              0.442-point friction floor. A search that cannot succeed still
              returns a top row &mdash; every one of these panels ranked its
              results and showed a winner.
            </p>

            <p className="muted small">
              And the form was the problem as much as the code. What made the
              archive's work trustworthy was pre-registered locks written
              before results, controls that could falsify the finding,
              train/test splits, multiple-comparison correction, and
              independent replication. A button cannot enforce any of that.
              Clicking "Optimize" gave a number with no lock, no null, and no
              record of how many combinations were searched.
            </p>

            <p className="muted small">
              The observable harm: walk-forward's optimal wall-proximity
              value flipped 3 &rarr; 5 &rarr; 8 across consecutive sessions.
              Noise, drawn in the visual grammar of a recommendation.
            </p>

            <p className="muted small">
              <strong>What survived reports rather than infers.</strong>{" "}
              Replay shows a session as it happened; the change log lists what
              was applied and when. Neither ranks anything, so neither can be
              overfit.
            </p>

            <p className="muted small" style={{ marginTop: 10 }}>
              Strategy research now lives in the research archive.{" "}
              <code>RESEARCH_REGISTRY.md</code> is the index; section K0 states
              what that work can and cannot establish, including the parts that
              are permanently unavailable.
            </p>
          </div>
        )}
      </header>

      <div className="research-grid">
        <div className="research-column">
          <h2 className="research-section-title">Historical analysis</h2>

          <PanelCard help={{
            title: "Replay",
            cadence: "weekly",
            whatItDoes:
              // Walls and max pain were dropped from this list 2026-08-25:
              // the QuantData API and data subscription were CANCELLED, so
              // replay no longer repopulates either overlay.
              "Visual playback of a past session on the chart with all " +
              "indicators populated. Drift, regime — everything you saw " +
              "live, rewound and scrubbed forward at your pace.",
            useItWhen:
              "When a specific session was confusing or anomalous and you " +
              "want to SEE what happened — not just read aggregate stats. " +
              "Useful for post-mortems on losing trades.",
            expect:
              "Chart loads with historical bars. You select a date and " +
              "time, hit play, indicators populate in sequence. Scrub " +
              "controls to advance one iter at a time.",
            note:
              "Replay shows you a session; it does not score one. Reading a " +
              "cause into a single replayed session is the same forking-path " +
              "error the removed panels automated — with n=1.",
          }}>
            <ErrorBoundary label="Replay">
              <ReplayPanel />
            </ErrorBoundary>
          </PanelCard>
        </div>

        <div className="research-column">
          <h2 className="research-section-title">Audit record</h2>

          <PanelCard help={{
            title: "Engine change log",
            cadence: "start-here",
            whatItDoes:
              "Chronological record of every engine change ever applied — " +
              "what changed, when, in what category, and at which commit.",
            useItWhen:
              "When a session's numbers look unlike the ones around it, and " +
              "you need to know whether something was changed underneath " +
              "them. This is the first thing to check before reading any " +
              "drift in metrics as signal.",
            expect:
              "A dated list, newest first. Read-only — there is no Apply " +
              "button, by design.",
            note:
              "Recorded impact figures are shown AS RECORDED. Historical " +
              "R-to-dollar conversions in this project were overstated ~7x " +
              "(they assumed ATM delta 0.50 where live strikes ran ~0.09), " +
              "so treat an older impact number as a label, not a P&L claim.",
          }}>
            <ErrorBoundary label="EngineChangeLog">
              <EngineChangeLogPanel />
            </ErrorBoundary>
          </PanelCard>
        </div>
      </div>
    </div>
  );
}

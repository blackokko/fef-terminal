import { useState } from "react";
import { ErrorBoundary } from "./ErrorBoundary";
import { PanelCard } from "./PanelCard";
import { PresetTournamentPanel } from "../panels/PresetTournamentPanel";
import { StrategyPresetsPanel } from "../panels/StrategyPresetsPanel";
import { PatternLibraryPanel } from "../panels/PatternLibraryPanel";
import { MultiStrategyPanel } from "../panels/MultiStrategyPanel";
import { TournamentPanel } from "../panels/TournamentPanel";
import { WalkForwardPanel } from "../panels/WalkForwardPanel";
import { OptimizePanel } from "../panels/OptimizePanel";
import { MLPanel } from "../panels/MLPanel";
import { CounterfactualPanel } from "../panels/CounterfactualPanel";
import { ReplayPanel } from "../panels/ReplayPanel";
import { AttributionPanel } from "../panels/AttributionPanel";
import { TuningPanel } from "../panels/TuningPanel";

/**
 * Research screen — consolidates backtest, parameter sweep, pattern
 * discovery, and strategy comparison tools. Each panel has an expandable
 * "What is this?" help block so the screen is self-documenting.
 */
export function ResearchScreen() {
  const [showQuickstart, setShowQuickstart] = useState(true);

  return (
    <div className="research-screen">
      <header className="research-header">
        <div className="research-header-row">
          <h1>Research Orchestration</h1>
          <button
            className="research-quickstart-toggle"
            onClick={() => setShowQuickstart((s) => !s)}
          >
            {showQuickstart ? "Hide quickstart" : "Show quickstart"}
          </button>
        </div>
        <p className="muted">
          Backtest, parameter sweep, pattern discovery, and strategy comparison.
          Everything here operates on historical or shadow data. Nothing on
          this screen places live trades.
        </p>

        {showQuickstart && (
          <div className="research-quickstart">
            <h3>How to use this screen — workflow in order</h3>
            <p className="muted small" style={{ marginBottom: 12 }}>
              You don't need every tool every day. Each panel has a colored
              timing badge — match those to your workflow stage.
            </p>

            <details className="quickstart-stage-details" open>
              <summary>
                <span className="quickstart-stage-tag" style={{ background: "var(--color-green)", color: "white" }}>
                  Start here today
                </span>
              </summary>
              <ol>
                <li>
                  <strong>Preset Tournament</strong> (top of screen) —
                  pick "last 7 days", click Run. See if any saved preset
                  would have outperformed your current config.
                </li>
                <li>
                  <strong>Attribution</strong> — see where your edge actually
                  came from over recent sessions. Hour of day, trigger type,
                  regime, FE ratio, wall distance.
                </li>
              </ol>
            </details>

            <details className="quickstart-stage-details" open>
              <summary>
                <span className="quickstart-stage-tag" style={{ background: "var(--color-accent)", color: "white" }}>
                  Each trading day
                </span>
              </summary>
              <ol>
                <li>
                  <strong>Pre-market</strong>: glance at Strategy Presets if
                  you have a regime read. Otherwise leave on "Default 2-min".
                </li>
                <li>
                  <strong>During hours</strong>: Multi-Strategy and Strategy
                  Tournament populate with live A/B verdicts (other panels
                  on Trading screen handle live execution).
                </li>
                <li>
                  <strong>End of day</strong>: run Preset Tournament to see
                  what would have done best, check Attribution for the day,
                  add notes to DEFERRED_CHANGES if anything stood out.
                </li>
              </ol>
            </details>

            <details className="quickstart-stage-details">
              <summary>
                <span className="quickstart-stage-tag" style={{ background: "var(--color-text-muted)", color: "white" }}>
                  Weekly review
                </span>
              </summary>
              <ol>
                <li>
                  <strong>Replay</strong> any confusing sessions visually.
                </li>
                <li>
                  <strong>Pattern Library</strong>: backtest specific setups
                  you noticed (define rule → run backtest → check Wilson CI).
                </li>
                <li>
                  <strong>ML proposals</strong>: review queued recommendations.
                  Add interesting ones to Pattern Library for validation.
                </li>
              </ol>
            </details>

            <details className="quickstart-stage-details">
              <summary>
                <span className="quickstart-stage-tag" style={{ background: "var(--color-text-muted)", color: "white" }}>
                  After 10+ sessions of data
                </span>
              </summary>
              <ol>
                <li>
                  <strong>Walk-Forward</strong>: validate that your current
                  thresholds aren't overfit. Two Sharpe numbers — train and
                  test — close together = robust.
                </li>
                <li>
                  <strong>Parameter Sweep</strong>: only AFTER walk-forward.
                  Find new optimal thresholds, then validate via walk-forward
                  before shipping.
                </li>
                <li>
                  <strong>Auto Walk-Forward</strong>: runs nightly on its own.
                  Check the latest WalkForwardRun in the morning.
                </li>
              </ol>
            </details>

            <p style={{ marginTop: 10, fontSize: 11 }} className="muted">
              <strong>Important:</strong> nothing on this screen places
              live trades or resets the freeze counter — all research is
              read-only on historical data. Loading a preset into the live
              engine via the Strategy Presets panel IS a live config change;
              everything else just analyses.
            </p>
          </div>
        )}
      </header>

      <ErrorBoundary label="PresetTournament">
        <PresetTournamentPanel />
      </ErrorBoundary>

      <div className="research-grid">
        <div className="research-column">
          <h2 className="research-section-title">Strategies</h2>

          <PanelCard help={{
            title: "Strategy Presets",
            cadence: "pre-market",
            whatItDoes:
              "Saved bundles of FEF threshold values (wall proximity, " +
              "wall collapse %, absorption FE minimum, etc.). Each preset " +
              "is a complete config you can load into the live engine.",
            useItWhen:
              "Before market open, if you have a strong read on what kind " +
              "of day you expect — load the matching preset (Pin / Trend / " +
              "Volatile / Chop / Power Hour). 'Default 2-min' is the safe " +
              "baseline when uncertain.",
            expect:
              "A list of saved presets with Apply / Edit / Delete buttons. " +
              "Hitting Apply hot-swaps the live engine's thresholds. The " +
              "active preset name shows in the UI; auto-trader will fire " +
              "differently based on the loaded thresholds.",
            note:
              "Loading a preset IS a live engine change. During the freeze " +
              "period, prefer the 'Default 2-min' preset unless you have a " +
              "specific tested reason to swap.",
          }}>
            <StrategyPresetsPanel />
          </PanelCard>

          <PanelCard help={{
            title: "Multi-Strategy",
            cadence: "daily-live",
            whatItDoes:
              "Runs N FEFEngine instances in parallel on the SAME live " +
              "data, each with different threshold configs. Your production " +
              "engine is the only one that fires real trades; the others " +
              "are silent observers logging what they WOULD have fired.",
            useItWhen:
              "During market hours, when you want to A/B test multiple " +
              "presets simultaneously without committing to one. Useful " +
              "to spot 'the Trend Day preset would have caught this move " +
              "that my Default 2-min missed' patterns.",
            expect:
              "Side-by-side verdict per strategy per iter, updating live. " +
              "Discrepancies between strategies are flagged. Empty outside " +
              "market hours.",
            liveOnly: true,
          }}>
            <MultiStrategyPanel />
          </PanelCard>

          <PanelCard help={{
            title: "Strategy Tournament (live)",
            cadence: "daily-live",
            whatItDoes:
              "Same parallel runner as Multi-Strategy, but adds a consensus " +
              "tally on top — 'how many strategies agree on this direction?' " +
              "Surfaces strong-agreement entries and warns on splits.",
            useItWhen:
              "During market hours, as a confirmation overlay. Strong " +
              "consensus (4 of 5 same direction) suggests robust signal; " +
              "full disagreement suggests mixed-regime chop — skip.",
            expect:
              "Per-variant verdicts plus a consensus row at the top with " +
              "agreement counts and consensus_strength (0.0–1.0). Empty " +
              "outside market hours.",
            liveOnly: true,
          }}>
            <TournamentPanel />
          </PanelCard>
        </div>

        <div className="research-column">
          <h2 className="research-section-title">Patterns &amp; ML</h2>

          <PanelCard help={{
            title: "Pattern Library",
            cadence: "weekly",
            whatItDoes:
              "Hand-crafted rule definitions for repeatable setups. Example: " +
              "'drift_cum > 5 AND fe_ratio > 0.5 AND nearest_wall_distance > 15 " +
              "AND aligned = True.' Distinct from the FEF model itself — " +
              "these are deterministic if-then triggers you can backtest.",
            useItWhen:
              "When you notice a specific setup pattern and want quantitative " +
              "validation. Define the pattern as a rule, hit Backtest, see " +
              "how often it fired and what the win rate / expectancy was.",
            expect:
              "Per-pattern statistics: n matches, wins/losses, win rate " +
              "with Wilson confidence interval, expectancy R, max drawdown, " +
              "Sharpe ratio.",
          }}>
            <PatternLibraryPanel />
          </PanelCard>

          <PanelCard help={{
            title: "ML Pattern Discovery",
            cadence: "weekly",
            whatItDoes:
              "Engine watches the DB for winning streaks and proposes new " +
              "pattern definitions or gate changes. Not 'AI deciding trades' " +
              "— an idea queue surfacing combinations of features that " +
              "correlate with wins.",
            useItWhen:
              "Periodic review (weekly). Browse suggestions, evaluate " +
              "with skepticism, manually add to Pattern Library if " +
              "compelling.",
            expect:
              "List of proposed rules with backing data (sample size, win " +
              "rate, p-value). Currently INFORMATIONAL only — Apply buttons " +
              "don't auto-mutate the engine.",
            note:
              "Auto-applying recommendations is in DEFERRED_CHANGES because " +
              "it's a freeze-impacting change. Small samples easily lie — " +
              "human review required.",
          }}>
            <MLPanel />
          </PanelCard>

          <PanelCard help={{
            title: "What-If Analysis",
            cadence: "daily-eod",
            whatItDoes:
              "Shows what today's WR% and P/L would be with proposed changes " +
              "(option-price BE ladder + B+C+D exits, or faster polling intervals) " +
              "without actually shipping them. Quantifies the impact of deferred " +
              "changes so you can prioritize which to ship after the freeze.",
            useItWhen:
              "Before deciding to reset the freeze counter for an engine change — " +
              "see the projected upside vs current behavior.",
            expect:
              "Side-by-side actual vs counterfactual P/L for the BE/TP changes, " +
              "plus a polling-interval comparison table (120s actual vs 30s/60s/90s).",
            note:
              "All read-only — no DB writes, no engine changes, freeze counter " +
              "unaffected. BE/TP counterfactual uses BE-step as proxy for option " +
              "MFE; polling counterfactual replays FEFEngine over FlowSnapshots.",
          }}>
            <CounterfactualPanel />
          </PanelCard>
        </div>

        <div className="research-column">
          <h2 className="research-section-title">Optimization</h2>

          <PanelCard help={{
            title: "Walk-Forward",
            cadence: "after-more-data",
            whatItDoes:
              "Parameter sweep PLUS out-of-sample validation. Sweeps " +
              "threshold combinations on the first 60% of historical data, " +
              "picks the winner, then tests it on the LAST 40% (data the " +
              "sweep never saw). Reports both train and test metrics.",
            useItWhen:
              "When you think you've found good parameters and need to " +
              "know if they'll generalize — versus being curve-fit to " +
              "history. The gold standard for parameter selection.",
            expect:
              "Two Sharpe numbers per run: train Sharpe (in-sample) and " +
              "test Sharpe (out-of-sample). If they're close, the params " +
              "generalize. If train >> test, they overfit — don't use.",
            note:
              "Auto Walk-Forward runs this nightly at 10pm — check the " +
              "latest WalkForwardRun row each morning for fresh tuning " +
              "context.",
          }}>
            <WalkForwardPanel />
          </PanelCard>

          <PanelCard help={{
            title: "Parameter Sweep (Optimize)",
            cadence: "after-more-data",
            whatItDoes:
              "Grid search through threshold combinations. Tries hundreds " +
              "of combos (wall_proximity × wall_collapse_pct × floor × " +
              "absorption_FE_min) and ranks each by historical Sharpe / " +
              "expectancy.",
            useItWhen:
              "Exploration phase — when you want to FIND optimal threshold " +
              "values from data instead of guessing. The output feeds into " +
              "Walk-Forward for validation.",
            expect:
              "A ranked table of parameter combinations with Sharpe / " +
              "expectancy / max drawdown. The top combination is the BEST " +
              "ON HISTORY — does not guarantee it works going forward " +
              "(that's what Walk-Forward proves).",
            note:
              "Parameter Sweep alone is the engine. Walk-Forward is the " +
              "methodology that wraps it with train/test validation. Always " +
              "validate sweep results via Walk-Forward before shipping.",
          }}>
            <OptimizePanel />
          </PanelCard>
        </div>

        <div className="research-column">
          <h2 className="research-section-title">Historical analysis</h2>

          <PanelCard help={{
            title: "Replay",
            cadence: "weekly",
            whatItDoes:
              "Visual playback of a past session on the chart with all " +
              "indicators populated. Walls, drift, regime, max pain — " +
              "everything you saw live, rewound and scrubbed forward at " +
              "your pace.",
            useItWhen:
              "When a specific session was confusing or anomalous and you " +
              "want to SEE what happened — not just read aggregate stats. " +
              "Useful for post-mortems on losing trades.",
            expect:
              "Chart loads with historical bars. You select a date and " +
              "time, hit play, indicators populate in sequence. Scrub " +
              "controls to advance one iter at a time.",
          }}>
            <ReplayPanel />
          </PanelCard>

          <PanelCard help={{
            title: "Attribution",
            cadence: "start-here",
            whatItDoes:
              "Performance breakdown by dimensions — trigger type, regime, " +
              "hour-of-day, FE ratio band, wall distance band, direction. " +
              "Answers 'WHERE is my edge actually coming from?'",
            useItWhen:
              "Weekly review. After enough trades to have statistical " +
              "signal, this is the panel that tells you which subset of " +
              "your strategy is paying you and which is bleeding R.",
            expect:
              "Tables of expectancy / win rate per dimension. Example: " +
              "'confluence trigger = +0.37R, aligned-only = -0.26R; " +
              "11am-noon CT = +1.04R, 2pm-3pm CT = -0.45R.'",
          }}>
            <AttributionPanel />
          </PanelCard>

          <PanelCard help={{
            title: "Tuning recommendations + Engine changes log",
            cadence: "after-more-data",
            whatItDoes:
              "Suggested parameter changes (from auto-walk-forward analysis) " +
              "plus a chronological log of every engine change ever applied. " +
              "Use this to understand WHY a session's data looks the way it " +
              "does (recent change vs steady-state) and what tuning the data " +
              "is currently suggesting.",
            useItWhen:
              "When investigating drift in metrics, considering a tuning " +
              "decision, or auditing the engine-change history before " +
              "applying a recommendation.",
            expect:
              "Tuning panel with current recommendations + recent engine " +
              "changes table. Applying a tuning recommendation IS an engine " +
              "change — it resets the freeze counter.",
            note:
              "Moved here from the trading sidebar — this is research/tuning " +
              "work, not live-trade work.",
          }}>
            <TuningPanel />
          </PanelCard>
        </div>
      </div>
    </div>
  );
}

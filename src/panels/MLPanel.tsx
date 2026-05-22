import { useEffect, useState } from "react";
import { formatDateTimeCT } from "../lib/displayHelpers";

const API_BASE = "http://127.0.0.1:8765";

interface MLRun {
  id: number;
  trained_at: string;
  algorithm: string;
  n_training_rows: number;
  n_features: number;
  label_kind: "r_multiple" | "binary_win";
  cv_mae: number | null;
  cv_r2: number | null;
  cv_n_folds: number | null;
  model_path: string | null;
  train_accuracy: number | null;
  test_accuracy: number | null;
  test_auc: number | null;
  feature_importances: Array<{ name: string; importance: number }>;
}

interface PatternProposal {
  feature: string;
  operator: string;
  threshold: number;
  n_match: number;
  n_match_wins: number;
  expected_R: number;
  win_rate_pct: number;
  baseline_expected_R: number;
  r_lift_vs_baseline: number;
  score: number;
  notes: string;
}

export function MLPanel() {
  const [runs, setRuns] = useState<MLRun[]>([]);
  const [training, setTraining] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [proposals, setProposals] = useState<PatternProposal[]>([]);
  const [loadingProposals, setLoadingProposals] = useState(false);
  const [acceptedKeys, setAcceptedKeys] = useState<Set<string>>(new Set());

  const refresh = async () => {
    try {
      const r = await fetch(`${API_BASE}/ml/runs?limit=5`);
      if (r.ok) setRuns(await r.json());
    } catch {}
  };

  const refreshProposals = async () => {
    setLoadingProposals(true);
    try {
      const r = await fetch(`${API_BASE}/ml/proposals?top_k=10`);
      const data = await r.json();
      if (data.ok) setProposals(data.proposals);
    } catch {}
    setLoadingProposals(false);
  };

  useEffect(() => { void refresh(); void refreshProposals(); }, []);

  const handleTrain = async () => {
    setTraining(true);
    setError(null);
    try {
      const r = await fetch(`${API_BASE}/ml/train`, { method: "POST" });
      const data = await r.json();
      if (!data.ok) setError(data.error || "Training failed");
      else {
        await refresh();
        await refreshProposals();
      }
    } catch (e) {
      setError((e as Error).message);
    }
    setTraining(false);
  };

  const acceptProposal = async (p: PatternProposal) => {
    const key = `${p.feature}|${p.operator}|${p.threshold}`;
    try {
      const r = await fetch(`${API_BASE}/ml/proposals/accept`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          feature: p.feature,
          operator: p.operator,
          threshold: p.threshold,
          notes: `ML proposal · n=${p.n_match} · ER=${p.expected_R}R · WR=${p.win_rate_pct}%`,
        }),
      });
      const data = await r.json();
      if (data.ok || data.error?.includes("already exists")) {
        setAcceptedKeys(prev => new Set(prev).add(key));
      } else {
        setError(data.error || "Accept failed");
      }
    } catch (e) {
      setError((e as Error).message);
    }
  };

  const latest = runs[0];

  return (
    <section className="sidebar-section">
      <h3 className="sidebar-section-title">ML pattern discovery</h3>
      <button onClick={() => void handleTrain()} disabled={training} style={{ marginBottom: 8 }}>
        {training ? "Training random forest..." : "Train model"}
      </button>

      {error && <div className="muted small" style={{ color: "var(--color-red)", marginBottom: 6 }}>{error}</div>}

      {!latest ? (
        <p className="muted small">no models trained yet — needs ≥30 closed trades</p>
      ) : (
        <>
          <div className="ml-stats">
            {latest.label_kind === "r_multiple" ? (
              <>
                <div><div className="muted small">CV MAE (R)</div><div className="ml-stat-val">{latest.cv_mae?.toFixed(3) ?? "—"}</div></div>
                <div><div className="muted small">CV R²</div><div className="ml-stat-val">{latest.cv_r2?.toFixed(3) ?? "—"}</div></div>
                <div><div className="muted small">Folds</div><div className="ml-stat-val">{latest.cv_n_folds ?? "—"}</div></div>
                <div><div className="muted small">Rows</div><div className="ml-stat-val">{latest.n_training_rows}</div></div>
              </>
            ) : (
              <>
                <div><div className="muted small">Train</div><div className="ml-stat-val">{((latest.train_accuracy ?? 0) * 100).toFixed(1)}%</div></div>
                <div><div className="muted small">Test</div><div className="ml-stat-val">{((latest.test_accuracy ?? 0) * 100).toFixed(1)}%</div></div>
                <div><div className="muted small">AUC</div><div className="ml-stat-val">{latest.test_auc?.toFixed(3) ?? "—"}</div></div>
                <div><div className="muted small">Rows</div><div className="ml-stat-val">{latest.n_training_rows}</div></div>
              </>
            )}
          </div>

          <div className="muted small" style={{ marginTop: 8, marginBottom: 4 }}>Top features (importance)</div>
          <ul className="ml-features-list">
            {latest.feature_importances.slice(0, 8).map(f => (
              <li key={f.name}>
                <span>{f.name}</span>
                <div className="ml-feature-bar-bg">
                  <div className="ml-feature-bar-fill" style={{ width: `${Math.min(100, f.importance * 200)}%` }} />
                </div>
                <span className="muted small">{(f.importance * 100).toFixed(1)}%</span>
              </li>
            ))}
          </ul>

          <div className="muted small" style={{ marginTop: 6 }}>
            {latest.algorithm} · {formatDateTimeCT(latest.trained_at)}
          </div>
        </>
      )}

      <div style={{ marginTop: 14, borderTop: "1px solid var(--color-border)", paddingTop: 10 }}>
        <div style={{ display: "flex", justifyContent: "space-between", alignItems: "center", marginBottom: 6 }}>
          <strong className="small">Proposed patterns</strong>
          <button onClick={() => void refreshProposals()} disabled={loadingProposals} className="small">
            {loadingProposals ? "scanning..." : "rescan"}
          </button>
        </div>
        {proposals.length === 0 ? (
          <p className="muted small">
            {loadingProposals ? "scanning training data..." : "no candidates yet — needs more closed trades, or no single-condition rule cleared the +0.10R lift bar"}
          </p>
        ) : (
          <ul className="ml-proposals-list" style={{ listStyle: "none", padding: 0, margin: 0 }}>
            {proposals.map(p => {
              const key = `${p.feature}|${p.operator}|${p.threshold}`;
              const accepted = acceptedKeys.has(key);
              return (
                <li key={key} style={{ marginBottom: 6, padding: 6, background: "var(--color-bg-alt)", borderRadius: 4 }}>
                  <div style={{ display: "flex", justifyContent: "space-between", alignItems: "baseline" }}>
                    <code style={{ fontSize: 12 }}>{p.feature} {p.operator} {p.threshold}</code>
                    <button
                      disabled={accepted}
                      onClick={() => void acceptProposal(p)}
                      className="small"
                      style={accepted ? { opacity: 0.5 } : undefined}
                    >
                      {accepted ? "✓ accepted" : "accept"}
                    </button>
                  </div>
                  <div className="muted small" style={{ marginTop: 2 }}>
                    n={p.n_match} · ER={p.expected_R > 0 ? "+" : ""}{p.expected_R.toFixed(2)}R · WR={p.win_rate_pct.toFixed(0)}% · lift +{p.r_lift_vs_baseline.toFixed(2)}R vs baseline
                  </div>
                </li>
              );
            })}
          </ul>
        )}
      </div>
    </section>
  );
}

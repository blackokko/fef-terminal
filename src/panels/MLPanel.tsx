import { useEffect, useState } from "react";

const API_BASE = "http://127.0.0.1:8765";

interface MLRun {
  id: number;
  trained_at: string;
  algorithm: string;
  n_training_rows: number;
  n_features: number;
  train_accuracy: number;
  test_accuracy: number;
  test_auc: number | null;
  feature_importances: Array<{ name: string; importance: number }>;
}

export function MLPanel() {
  const [runs, setRuns] = useState<MLRun[]>([]);
  const [training, setTraining] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const refresh = async () => {
    try {
      const r = await fetch(`${API_BASE}/ml/runs?limit=5`);
      if (r.ok) setRuns(await r.json());
    } catch {}
  };

  useEffect(() => { void refresh(); }, []);

  const handleTrain = async () => {
    setTraining(true);
    setError(null);
    try {
      const r = await fetch(`${API_BASE}/ml/train`, { method: "POST" });
      const data = await r.json();
      if (!data.ok) setError(data.error || "Training failed");
      else await refresh();
    } catch (e) {
      setError((e as Error).message);
    }
    setTraining(false);
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
            <div><div className="muted small">Train</div><div className="ml-stat-val">{(latest.train_accuracy * 100).toFixed(1)}%</div></div>
            <div><div className="muted small">Test</div><div className="ml-stat-val">{(latest.test_accuracy * 100).toFixed(1)}%</div></div>
            <div><div className="muted small">AUC</div><div className="ml-stat-val">{latest.test_auc?.toFixed(3) ?? "—"}</div></div>
            <div><div className="muted small">Rows</div><div className="ml-stat-val">{latest.n_training_rows}</div></div>
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
            {latest.algorithm} · {new Date(latest.trained_at).toLocaleString()}
          </div>
        </>
      )}
    </section>
  );
}

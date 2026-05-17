import { useEffect, useState } from "react";
import { useToast } from "../components/Toast";
import { useSidecar } from "../context/WebSocketContext";
import { playTones, flashTitle } from "../lib/audioAlert";
import { formatDollars } from "../lib/format";

// IMPROVEMENTS #R: pattern library — feature-loaded for codifying discretionary setups.

interface FieldDef {
  name: string;
  label: string;
  kind: "number" | "int" | "bool" | "enum";
  enum_values: string[];
  description: string;
}

interface Condition {
  field: string;
  operator: string;
  value: number | string | boolean | number[];
}

interface Pattern {
  id: number;
  name: string;
  description: string;
  conditions: Condition[];
  enabled: boolean;
  audio_alert: boolean;
  toast_alert: boolean;
  match_count: number;
  last_matched_at: string | null;
}

const API_BASE = "http://127.0.0.1:8765";

const OPERATORS_BY_KIND: Record<string, string[]> = {
  number: ["==", "!=", "<", "<=", ">", ">=", "between"],
  int: ["==", "!=", "<", "<=", ">", ">=", "between"],
  bool: ["==", "!="],
  enum: ["==", "!=", "in", "not_in"],
};

export function PatternLibraryPanel() {
  const { recentPatternMatches } = useSidecar();
  const [patterns, setPatterns] = useState<Pattern[]>([]);
  const [fields, setFields] = useState<FieldDef[]>([]);
  const [editing, setEditing] = useState<Pattern | null>(null);
  const [showCreate, setShowCreate] = useState(false);
  const toast = useToast();

  const reload = async () => {
    const [pR, fR] = await Promise.all([
      fetch(`${API_BASE}/patterns`),
      fetch(`${API_BASE}/patterns/fields`),
    ]);
    if (pR.ok) setPatterns(await pR.json());
    if (fR.ok) setFields(await fR.json());
  };

  useEffect(() => {
    void reload();
  }, []);

  // Audio + toast on incoming pattern matches
  const lastMatch = recentPatternMatches[recentPatternMatches.length - 1];
  useEffect(() => {
    if (!lastMatch) return;
    if (lastMatch.audio_alert) {
      // Distinctive 3-tone chime — different from FEF HIGH chimes
      playTones([659.25, 783.99, 987.77], 0.15);  // E5, G5, B5
    }
    if (lastMatch.toast_alert) {
      toast.show(`🎯 ${lastMatch.pattern_name} matched (iter #${lastMatch.iter_num})`, {
        variant: "warn",
        duration: 5000,
      });
    }
    flashTitle(`🎯 ${lastMatch.pattern_name}`, 4);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [lastMatch?.timestamp_ms]);

  const toggle = async (id: number) => {
    await fetch(`${API_BASE}/patterns/${id}/toggle`, { method: "POST" });
    await reload();
  };

  const remove = async (id: number, name: string) => {
    if (!confirm(`Delete pattern "${name}"?`)) return;
    await fetch(`${API_BASE}/patterns/${id}`, { method: "DELETE" });
    await reload();
    toast.show(`Deleted: ${name}`, { variant: "info" });
  };

  const seedDefaults = async () => {
    const r = await fetch(`${API_BASE}/patterns/seed_defaults`, { method: "POST" });
    if (r.ok) {
      const data = await r.json();
      await reload();
      toast.show(`Seeded ${data.added.length} default patterns`, { variant: "success" });
    }
  };

  const [backtest, setBacktest] = useState<{ pattern_id: number; result: any } | null>(null);
  const runBacktest = async (id: number, name: string) => {
    setBacktest({ pattern_id: id, result: null });
    const r = await fetch(`${API_BASE}/patterns/${id}/backtest?days=30`, { method: "POST" });
    if (r.ok) {
      const data = await r.json();
      setBacktest({ pattern_id: id, result: data });
      toast.show(`Backtest done: ${name}`, { variant: "info" });
    }
  };

  return (
    <details className="sidebar-section">
      <summary className="sidebar-section-title">
        Pattern library {patterns.length > 0 && `· ${patterns.length}`}
      </summary>
      <div className="patterns-body">
        <div style={{ display: "flex", gap: 6, marginBottom: 8 }}>
          <button onClick={() => void seedDefaults()}>+ Seed defaults</button>
          {!showCreate && !editing && (
            <button onClick={() => setShowCreate(true)}>+ New pattern</button>
          )}
        </div>
        {patterns.length === 0 ? (
          <p className="muted small">no patterns yet — click 'Seed defaults' for 17 starter setups</p>
        ) : (
          <ul className="patterns-list">
            {patterns.map((p) => (
              <li key={p.id} className={`pattern-row ${p.enabled ? "" : "pattern-disabled"}`}>
                <div className="pattern-header">
                  <input
                    type="checkbox"
                    checked={p.enabled}
                    onChange={() => void toggle(p.id)}
                    title={p.enabled ? "Disable" : "Enable"}
                  />
                  <span className="pattern-name">{p.name}</span>
                  <span className="muted small">{p.match_count}× today</span>
                  <button className="annotation-delete" title="Backtest" onClick={() => void runBacktest(p.id, p.name)}>📊</button>
                  <button className="annotation-delete" onClick={() => setEditing(p)}>✎</button>
                  <button className="annotation-delete" onClick={() => void remove(p.id, p.name)}>×</button>
                </div>
                {backtest?.pattern_id === p.id && (
                  <BacktestResultBlock result={backtest.result} onClose={() => setBacktest(null)} />
                )}
                {p.description && <div className="muted small">{p.description}</div>}
                <div className="pattern-conditions small">
                  {p.conditions.map((c, i) => (
                    <span key={i} className="pattern-condition">
                      {c.field} {c.operator} {JSON.stringify(c.value)}
                    </span>
                  ))}
                </div>
              </li>
            ))}
          </ul>
        )}

        {(showCreate || editing) && (
          <PatternEditor
            initial={editing}
            fields={fields}
            onSave={async () => {
              setShowCreate(false);
              setEditing(null);
              await reload();
              toast.show(editing ? "Pattern updated" : "Pattern created", { variant: "success" });
            }}
            onCancel={() => {
              setShowCreate(false);
              setEditing(null);
            }}
          />
        )}

        {recentPatternMatches.length > 0 && (
          <div className="pattern-matches">
            <div className="subsection-title">Recent matches</div>
            <ul className="signal-log">
              {[...recentPatternMatches].slice(-10).reverse().map((m, i) => (
                <li key={`${m.timestamp_ms}-${i}`} className="signal-log-row">
                  <span className="signal-iter">#{m.iter_num}</span>
                  <span className="signal-spx">${m.spx_price.toFixed(2)}</span>
                  <span className="signal-direction" style={{ color: "var(--color-amber)" }}>
                    {m.pattern_name}
                  </span>
                </li>
              ))}
            </ul>
          </div>
        )}
      </div>
    </details>
  );
}

function BacktestResultBlock({ result, onClose }: { result: any; onClose: () => void }) {
  if (!result) return <div className="muted small">running backtest…</div>;
  const sigColor = result.t_stat_vs_zero != null && Math.abs(result.t_stat_vs_zero) >= 2
    ? "var(--color-green)" : "var(--color-text-muted)";
  return (
    <div className="backtest-result">
      <div className="backtest-header">
        <strong>{result.window_days}d backtest</strong>
        <button className="annotation-delete" onClick={onClose}>×</button>
      </div>
      <div className="backtest-grid">
        <Stat label="Iters scanned" value={result.total_iters_scanned} />
        <Stat label="Matches" value={result.total_matches} />
        <Stat label="Matches/day" value={result.matches_per_day} />
        <Stat label="With trades" value={result.matches_with_trades} />
        <Stat label="Win rate" value={`${result.win_rate_pct}%`}
          extra={`95% CI ${result.win_rate_ci_low}-${result.win_rate_ci_high}`} />
        <Stat label="Wins / losses" value={`${result.wins}W / ${result.losses}L`} />
        <Stat label="Total P/L" value={formatDollars(result.total_pnl, { sign: true })}
          color={result.total_pnl >= 0 ? "var(--color-green)" : "var(--color-red)"} />
        <Stat label="Avg / trade" value={formatDollars(result.avg_pnl_per_trade, { sign: true })} />
        <Stat label="Best / worst" value={`${formatDollars(result.best_trade, { sign: true })} / ${formatDollars(result.worst_trade, { sign: true })}`} />
        <Stat label="Max drawdown" value={formatDollars(result.max_drawdown)} color="var(--color-red)" />
        <Stat label="Sharpe (ann)" value={result.sharpe_ratio ?? "n/a"} />
        <Stat label="t-stat vs 0" value={result.t_stat_vs_zero ?? "n/a"}
          color={sigColor}
          extra={result.t_stat_vs_zero != null && Math.abs(result.t_stat_vs_zero) >= 2 ? "significant" : ""} />
      </div>
      {result.matches_with_trades === 0 && (
        <p className="muted small">No closed-trade outcomes for these matches yet — fire the pattern, then close trades to build stats.</p>
      )}
    </div>
  );
}

function Stat({ label, value, extra, color }: { label: string; value: any; extra?: string; color?: string }) {
  return (
    <div className="backtest-stat">
      <div className="backtest-stat-label">{label}</div>
      <div className="backtest-stat-value" style={color ? { color } : undefined}>{value}</div>
      {extra && <div className="backtest-stat-extra">{extra}</div>}
    </div>
  );
}

function PatternEditor({
  initial,
  fields,
  onSave,
  onCancel,
}: {
  initial: Pattern | null;
  fields: FieldDef[];
  onSave: () => void | Promise<void>;
  onCancel: () => void;
}) {
  const [name, setName] = useState(initial?.name ?? "");
  const [description, setDescription] = useState(initial?.description ?? "");
  const [conditions, setConditions] = useState<Condition[]>(initial?.conditions ?? []);
  const [audioAlert, setAudioAlert] = useState(initial?.audio_alert ?? true);
  const [toastAlert, setToastAlert] = useState(initial?.toast_alert ?? true);
  const [testResult, setTestResult] = useState<any>(null);

  const addCondition = () => {
    setConditions([...conditions, { field: fields[0]?.name ?? "", operator: "==", value: 0 }]);
  };

  const updateCondition = (i: number, updates: Partial<Condition>) => {
    setConditions(conditions.map((c, idx) => (idx === i ? { ...c, ...updates } : c)));
  };

  const removeCondition = (i: number) => {
    setConditions(conditions.filter((_, idx) => idx !== i));
  };

  const save = async () => {
    if (!name.trim() || conditions.length === 0) return;
    const body = { name: name.trim(), description, conditions, audio_alert: audioAlert, toast_alert: toastAlert };
    const url = initial ? `${API_BASE}/patterns/${initial.id}` : `${API_BASE}/patterns`;
    const method = initial ? "PUT" : "POST";
    const r = await fetch(url, { method, headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) });
    if (r.ok) await onSave();
  };

  const test = async () => {
    const r = await fetch(`${API_BASE}/patterns/test`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ conditions }),
    });
    if (r.ok) setTestResult(await r.json());
  };

  return (
    <div className="pattern-editor">
      <input
        placeholder="Pattern name"
        value={name}
        onChange={(e) => setName(e.target.value)}
        className="settings-input"
        style={{ width: "100%" }}
      />
      <input
        placeholder="Description (optional)"
        value={description}
        onChange={(e) => setDescription(e.target.value)}
        className="settings-input"
        style={{ width: "100%" }}
      />
      <div className="conditions-list">
        {conditions.map((c, i) => {
          const fieldDef = fields.find((f) => f.name === c.field);
          const operators = fieldDef ? OPERATORS_BY_KIND[fieldDef.kind] : ["=="];
          return (
            <div key={i} className="condition-row">
              <select
                value={c.field}
                onChange={(e) => updateCondition(i, { field: e.target.value })}
                className="settings-input"
              >
                {fields.map((f) => (
                  <option key={f.name} value={f.name}>{f.label}</option>
                ))}
              </select>
              <select
                value={c.operator}
                onChange={(e) => updateCondition(i, { operator: e.target.value })}
                className="settings-input"
              >
                {operators.map((op) => <option key={op} value={op}>{op}</option>)}
              </select>
              <ValueInput field={fieldDef} value={c.value} onChange={(v) => updateCondition(i, { value: v })} />
              <button className="annotation-delete" onClick={() => removeCondition(i)}>×</button>
            </div>
          );
        })}
      </div>
      <button onClick={addCondition} style={{ marginTop: 4 }}>+ Add condition</button>

      <div className="pattern-alerts">
        <label><input type="checkbox" checked={audioAlert} onChange={(e) => setAudioAlert(e.target.checked)} /> audio chime</label>
        <label><input type="checkbox" checked={toastAlert} onChange={(e) => setToastAlert(e.target.checked)} /> toast</label>
      </div>

      <div className="outcome-actions">
        <button onClick={() => void test()}>Test against last iter</button>
        <button onClick={() => void save()}>Save</button>
        <button onClick={onCancel}>Cancel</button>
      </div>

      {testResult && (
        <div className="test-result small">
          <div className={testResult.matched ? "test-matched" : "test-unmatched"}>
            {testResult.matched ? "✓ Would match" : "✗ Would NOT match"}
          </div>
          {testResult.conditions.map((c: any, i: number) => (
            <div key={i} className={`test-condition ${c.matched ? "test-matched" : "test-unmatched"}`}>
              {c.field} {c.operator} {JSON.stringify(c.value)} → observed: {JSON.stringify(c.observed)} {c.matched ? "✓" : "✗"}
            </div>
          ))}
        </div>
      )}
    </div>
  );
}

function ValueInput({
  field,
  value,
  onChange,
}: {
  field: FieldDef | undefined;
  value: number | string | boolean | number[];
  onChange: (v: number | string | boolean | number[]) => void;
}) {
  if (!field) return null;
  if (field.kind === "bool") {
    return (
      <select
        value={String(value)}
        onChange={(e) => onChange(e.target.value === "true")}
        className="settings-input"
      >
        <option value="true">true</option>
        <option value="false">false</option>
      </select>
    );
  }
  if (field.kind === "enum") {
    return (
      <select
        value={String(value)}
        onChange={(e) => onChange(e.target.value)}
        className="settings-input"
      >
        {field.enum_values.map((v) => <option key={v} value={v}>{v}</option>)}
      </select>
    );
  }
  return (
    <input
      type="number"
      step={field.kind === "int" ? 1 : 0.1}
      value={Number(value)}
      onChange={(e) => onChange(Number(e.target.value))}
      className="settings-input"
    />
  );
}

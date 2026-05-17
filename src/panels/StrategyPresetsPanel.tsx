import { useEffect, useState } from "react";
import { useToast } from "../components/Toast";

// IMPROVEMENTS #C: named threshold sets ("tight pin day", "trend day", etc.)

interface Preset {
  id: number;
  name: string;
  description: string;
  fef_loop_interval_sec: number;
  wall_proximity_pts: number;
  wall_collapse_pct: number;
  wall_collapse_floor_m: number;
  absorption_fe_ratio_min: number;
}

const API_BASE = "http://127.0.0.1:8765";

export function StrategyPresetsPanel() {
  const [presets, setPresets] = useState<Preset[]>([]);
  const [showCreate, setShowCreate] = useState(false);
  const toast = useToast();

  const reload = async () => {
    const r = await fetch(`${API_BASE}/presets`);
    if (r.ok) setPresets(await r.json());
  };

  useEffect(() => {
    void reload();
  }, []);

  const apply = async (id: number, name: string) => {
    const r = await fetch(`${API_BASE}/presets/${id}/apply`, { method: "POST" });
    if (r.ok) toast.show(`Applied: ${name}`, { variant: "success" });
    else toast.show(`Failed to apply ${name}`, { variant: "error" });
  };

  const remove = async (id: number, name: string) => {
    if (!confirm(`Delete preset "${name}"?`)) return;
    const r = await fetch(`${API_BASE}/presets/${id}`, { method: "DELETE" });
    if (r.ok) {
      void reload();
      toast.show(`Deleted: ${name}`, { variant: "info" });
    }
  };

  return (
    <details className="sidebar-section">
      <summary className="sidebar-section-title">Strategy presets</summary>
      <div className="presets-body">
        {presets.length === 0 && !showCreate ? (
          <p className="muted small">no presets yet</p>
        ) : (
          <ul className="presets-list">
            {presets.map((p) => (
              <li key={p.id} className="preset-row">
                <div className="preset-info">
                  <div className="preset-name">{p.name}</div>
                  {p.description && <div className="muted small">{p.description}</div>}
                  <div className="preset-stats small muted">
                    prox {p.wall_proximity_pts}pt · collapse{" "}
                    {(p.wall_collapse_pct * 100).toFixed(0)}% · floor ${p.wall_collapse_floor_m}M
                  </div>
                </div>
                <div className="preset-actions">
                  <button onClick={() => void apply(p.id, p.name)}>Apply</button>
                  <button onClick={() => void remove(p.id, p.name)}>×</button>
                </div>
              </li>
            ))}
          </ul>
        )}
        {showCreate ? (
          <CreatePresetForm
            onCreate={async () => {
              await reload();
              setShowCreate(false);
              toast.show("Preset saved", { variant: "success" });
            }}
            onCancel={() => setShowCreate(false)}
          />
        ) : (
          <button onClick={() => setShowCreate(true)} style={{ marginTop: 8 }}>
            + New preset
          </button>
        )}
      </div>
    </details>
  );
}

function CreatePresetForm({
  onCreate,
  onCancel,
}: {
  onCreate: () => void | Promise<void>;
  onCancel: () => void;
}) {
  const [name, setName] = useState("");
  const [description, setDescription] = useState("");
  const [proximity, setProximity] = useState(5);
  const [collapsePct, setCollapsePct] = useState(0.2);
  const [collapseFloor, setCollapseFloor] = useState(5);

  const submit = async () => {
    if (!name.trim()) return;
    const r = await fetch(`${API_BASE}/presets`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        name: name.trim(),
        description: description.trim(),
        wall_proximity_pts: proximity,
        wall_collapse_pct: collapsePct,
        wall_collapse_floor_m: collapseFloor,
      }),
    });
    if (r.ok) await onCreate();
  };

  return (
    <div className="preset-create-form">
      <input
        placeholder="Name (e.g. 'tight pin')"
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
      <div className="outcome-row">
        <span className="ticket-label">Wall proximity</span>
        <input type="number" step={0.5} value={proximity} onChange={(e) => setProximity(Number(e.target.value))} className="settings-input" />
      </div>
      <div className="outcome-row">
        <span className="ticket-label">Collapse %</span>
        <input type="number" step={0.05} value={collapsePct} onChange={(e) => setCollapsePct(Number(e.target.value))} className="settings-input" />
      </div>
      <div className="outcome-row">
        <span className="ticket-label">Collapse floor $M</span>
        <input type="number" step={1} value={collapseFloor} onChange={(e) => setCollapseFloor(Number(e.target.value))} className="settings-input" />
      </div>
      <div className="outcome-actions">
        <button onClick={() => void submit()}>Save</button>
        <button onClick={onCancel}>Cancel</button>
      </div>
    </div>
  );
}

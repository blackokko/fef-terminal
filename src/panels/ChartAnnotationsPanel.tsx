import { useEffect, useState } from "react";
import { useToast } from "../components/Toast";

// IMPROVEMENTS #Q: persisted horizontal price levels on the chart.
// Used for marking levels the user cares about (intraday support/resistance,
// option strike clusters, daily pivot lines).

interface Annotation {
  id: number;
  price: number;
  label: string;
  color: string;
  line_style: string;
}

const API_BASE = "http://127.0.0.1:8765";
const COLORS = ["#d29922", "#58a6ff", "#a371f7", "#3fb950", "#f85149", "#e6edf3"];

export function ChartAnnotationsPanel() {
  const [annotations, setAnnotations] = useState<Annotation[]>([]);
  const [price, setPrice] = useState("");
  const [label, setLabel] = useState("");
  const [color, setColor] = useState(COLORS[0]);
  const toast = useToast();

  const reload = async () => {
    const r = await fetch(`${API_BASE}/annotations`);
    if (r.ok) setAnnotations(await r.json());
  };

  useEffect(() => {
    void reload();
  }, []);

  const add = async () => {
    if (!price.trim()) return;
    const r = await fetch(`${API_BASE}/annotations`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        price: Number(price),
        label: label.trim(),
        color,
        line_style: "dashed",
      }),
    });
    if (r.ok) {
      setPrice("");
      setLabel("");
      await reload();
      toast.show("Annotation added — reload chart to see", { variant: "success" });
    }
  };

  const remove = async (id: number) => {
    const r = await fetch(`${API_BASE}/annotations/${id}`, { method: "DELETE" });
    if (r.ok) {
      await reload();
      toast.show("Annotation removed", { variant: "info" });
    }
  };

  return (
    <details className="sidebar-section">
      <summary className="sidebar-section-title">Chart annotations</summary>
      <div className="annotations-body">
        {annotations.length === 0 ? (
          <p className="muted small">no annotations</p>
        ) : (
          <ul className="annotations-list">
            {annotations.map((a) => (
              <li key={a.id} className="annotation-row">
                <span className="annotation-swatch" style={{ background: a.color }} />
                <span className="annotation-price">${a.price.toFixed(2)}</span>
                <span className="annotation-label">{a.label || "—"}</span>
                <button onClick={() => void remove(a.id)} className="annotation-delete">×</button>
              </li>
            ))}
          </ul>
        )}
        <div className="annotation-create">
          <input
            type="number"
            step={0.5}
            placeholder="Price"
            value={price}
            onChange={(e) => setPrice(e.target.value)}
            className="settings-input"
          />
          <input
            placeholder="Label (e.g. 'PDH')"
            value={label}
            onChange={(e) => setLabel(e.target.value)}
            className="settings-input"
            style={{ flex: 1 }}
          />
          <select value={color} onChange={(e) => setColor(e.target.value)} className="settings-input">
            {COLORS.map((c) => (
              <option key={c} value={c} style={{ background: c }}>{c}</option>
            ))}
          </select>
          <button onClick={() => void add()}>Add</button>
        </div>
      </div>
    </details>
  );
}

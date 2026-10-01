// Multi-timeframe STRUCTURE LEVELS overlay — helpers for ChartPane.
//
// Data: GET /chart/structure_levels (sidecar engine/structure_levels.py), a port of the
// `mtflevels` study's marker. DISPLAY ONLY — the study (QQQ 2022–26) found no mechanical edge in
// these levels; they are decision support for the operator's eyes.
//
// This module is pure: payload type guards, the persisted settings, the draw plan (which lines and
// zones to draw, in what colour and style) and the zones series primitive. ChartPane owns the
// polling and the chart objects.

import {
  LineStyle,
  type IChartApiBase,
  type IPrimitivePaneRenderer,
  type IPrimitivePaneView,
  type ISeriesApi,
  type ISeriesPrimitive,
  type LineWidth,
  type Logical,
  type PrimitivePaneViewZOrder,
  type SeriesAttachedParameter,
  type SeriesType,
  type Time,
  type UTCTimestamp,
} from "lightweight-charts";

// ---------------------------------------------------------------------------
// settings
// ---------------------------------------------------------------------------

export const SL_TAUS = ["5m", "15m", "30m", "1H", "4H", "1D"] as const;
export type SlTau = (typeof SL_TAUS)[number];

export const SL_FAMILIES = [
  { key: "S", label: "Swings" },
  { key: "Z", label: "Zones" },
  { key: "M", label: "Midpoints" },
  { key: "F", label: "Fibs" },
  { key: "P", label: "Pivots" },
  { key: "D", label: "Prior day/week" },
  { key: "R", label: "Round numbers" },
  { key: "setups", label: "Midpoint setups" },
] as const;
export type SlFamilyKey = (typeof SL_FAMILIES)[number]["key"];

export interface StructureSettings {
  taus: Record<SlTau, boolean>;
  families: Record<SlFamilyKey, boolean>;
}

/** Defaults: every timeframe except 5m, every family on. */
export function defaultStructureSettings(): StructureSettings {
  const taus = {} as Record<SlTau, boolean>;
  for (const t of SL_TAUS) taus[t] = t !== "5m";
  const families = {} as Record<SlFamilyKey, boolean>;
  for (const f of SL_FAMILIES) families[f.key] = true;
  return { taus, families };
}

const SETTINGS_KEY = "fef.structureLevels.v1";

export function loadStructureSettings(): StructureSettings {
  const out = defaultStructureSettings();
  try {
    const raw = window.localStorage.getItem(SETTINGS_KEY);
    if (!raw) return out;
    const parsed: unknown = JSON.parse(raw);
    if (!parsed || typeof parsed !== "object") return out;
    const p = parsed as { taus?: unknown; families?: unknown };
    if (p.taus && typeof p.taus === "object") {
      const t = p.taus as Record<string, unknown>;
      for (const k of SL_TAUS) if (typeof t[k] === "boolean") out.taus[k] = t[k] as boolean;
    }
    if (p.families && typeof p.families === "object") {
      const f = p.families as Record<string, unknown>;
      for (const { key } of SL_FAMILIES) if (typeof f[key] === "boolean") out.families[key] = f[key] as boolean;
    }
  } catch {
    /* storage blocked or corrupt — defaults */
  }
  return out;
}

export function saveStructureSettings(s: StructureSettings): void {
  try {
    window.localStorage.setItem(SETTINGS_KEY, JSON.stringify(s));
  } catch {
    /* storage blocked — the toggle still works for this session */
  }
}

/** Query string for the route. An empty selection is sent as "none" (the route treats an empty
 *  parameter as "all"). */
export function structureQuery(symbol: string, s: StructureSettings): string {
  const taus = SL_TAUS.filter((t) => s.taus[t]);
  const fams = SL_FAMILIES.map((f) => f.key).filter((k) => k !== "setups" && s.families[k]);
  return `symbol=${encodeURIComponent(symbol)}`
    + `&taus=${encodeURIComponent(taus.length ? taus.join(",") : "none")}`
    + `&families=${encodeURIComponent(fams.length ? fams.join(",") : "none")}`
    + `&setups=${s.families.setups ? "true" : "false"}`;
}

// ---------------------------------------------------------------------------
// payload + type guards (never throw on bad data — drop what does not parse)
// ---------------------------------------------------------------------------

export interface SlLevel {
  id: string;
  family: string;
  tf: SlTau;
  kind: string;
  lo: number;
  hi: number;
  price: number;
  known_from: number;
  state: "active" | "broken" | "pending";
  label: string;
  group: string | null;
  touches: number | null;
}

export interface SlGroup {
  id: string;
  price: number;
  tf: SlTau;
  family: string;
  label: string;
  state: "active" | "broken" | "pending";
  confluence: number;
  members: string[];
}

export interface SlSetup {
  id: string;
  tf: SlTau;
  origin: string;
  direction: "long" | "short";
  O: number;
  X: number;
  M: number;
  status: string;
  ambiguous: boolean;
  recent: boolean;
}

export interface SlPayload {
  status: string;
  asof: number | null;
  last_price: number | null;
  atr1D: number | null;
  calendarStale: boolean;   // the sidecar's NYSE holiday table no longer covers the data
  levels: SlLevel[];
  groups: SlGroup[];
  setups: SlSetup[];
}

const isNum = (x: unknown): x is number => typeof x === "number" && Number.isFinite(x);
const isStr = (x: unknown): x is string => typeof x === "string";
const asTau = (x: unknown): SlTau | null =>
  isStr(x) && (SL_TAUS as readonly string[]).includes(x) ? (x as SlTau) : null;
const asState = (x: unknown): "active" | "broken" | "pending" | null =>
  x === "active" || x === "broken" || x === "pending" ? x : null;

function parseLevel(x: unknown): SlLevel | null {
  if (!x || typeof x !== "object") return null;
  const o = x as Record<string, unknown>;
  const tf = asTau(o.tf);
  const state = asState(o.state);
  if (!isStr(o.id) || !isStr(o.family) || !tf || !isStr(o.kind) || !isNum(o.lo) || !isNum(o.hi)
      || !isNum(o.price) || !isNum(o.known_from) || !state || !isStr(o.label)) return null;
  return {
    id: o.id, family: o.family, tf, kind: o.kind, lo: o.lo, hi: o.hi, price: o.price,
    known_from: o.known_from, state, label: o.label,
    group: isStr(o.group) ? o.group : null,
    touches: isNum(o.touches) ? o.touches : null,
  };
}

function parseGroup(x: unknown): SlGroup | null {
  if (!x || typeof x !== "object") return null;
  const o = x as Record<string, unknown>;
  const tf = asTau(o.tf);
  const state = asState(o.state);
  if (!isStr(o.id) || !isNum(o.price) || !tf || !isStr(o.family) || !isStr(o.label) || !state) return null;
  return {
    id: o.id, price: o.price, tf, family: o.family, label: o.label, state,
    confluence: isNum(o.confluence) ? o.confluence : 1,
    members: Array.isArray(o.members) ? o.members.filter(isStr) : [],
  };
}

function parseSetup(x: unknown): SlSetup | null {
  if (!x || typeof x !== "object") return null;
  const o = x as Record<string, unknown>;
  const tf = asTau(o.tf);
  if (!isStr(o.id) || !tf || !isStr(o.origin) || (o.direction !== "long" && o.direction !== "short")
      || !isNum(o.O) || !isNum(o.X) || !isNum(o.M) || !isStr(o.status)) return null;
  return {
    id: o.id, tf, origin: o.origin, direction: o.direction, O: o.O, X: o.X, M: o.M,
    status: o.status, ambiguous: o.ambiguous === true, recent: o.recent === true,
  };
}

function parseList<T>(x: unknown, f: (v: unknown) => T | null): T[] {
  if (!Array.isArray(x)) return [];
  const out: T[] = [];
  for (const v of x) {
    const p = f(v);
    if (p) out.push(p);
  }
  return out;
}

export function parseStructurePayload(raw: unknown): SlPayload | null {
  if (!raw || typeof raw !== "object") return null;
  const o = raw as Record<string, unknown>;
  const atr = o.atr && typeof o.atr === "object" ? (o.atr as Record<string, unknown>) : {};
  return {
    status: isStr(o.status) ? o.status : "unknown",
    asof: isNum(o.asof) ? o.asof : null,
    last_price: isNum(o.last_price) ? o.last_price : null,
    atr1D: isNum(atr["1D"]) ? (atr["1D"] as number) : null,
    calendarStale: o.calendar_stale === true,
    levels: parseList(o.levels, parseLevel),
    groups: parseList(o.groups, parseGroup),
    setups: parseList(o.setups, parseSetup),
  };
}

// ---------------------------------------------------------------------------
// the draw plan
// ---------------------------------------------------------------------------

export const TAU_COLORS: Record<SlTau, string> = {
  "5m": "#9e9e9e", "15m": "#26a69a", "30m": "#42a5f5", "1H": "#ab47bc", "4H": "#ffa726", "1D": "#ef5350",
};
const LONG_COLOR = "#00e676";
const SHORT_COLOR = "#ff5252";
const ORIGIN_ABBR: Record<string, string> = { BREAK: "BRK", RECLAIM: "RCL", REJECT: "REJ" };
const SETUP_LINE_LIMIT = 8;

/** "#rrggbb" + alpha -> "#rrggbbaa" (canvas accepts 8-digit hex). */
export function withAlpha(hex: string, a: number): string {
  if (!/^#[0-9a-fA-F]{6}$/.test(hex)) return hex;
  const v = Math.max(0, Math.min(255, Math.round(a * 255)));
  return hex + v.toString(16).padStart(2, "0");
}

function styleFor(family: string): LineStyle {
  if (family === "S") return LineStyle.Solid;
  if (family === "F") return LineStyle.Dashed;
  if (family === "M") return LineStyle.Dotted;
  return LineStyle.LargeDashed;          // pivots / prior day-week / round numbers
}

export interface LineSpec {
  key: string;
  price: number;
  color: string;
  lineWidth: LineWidth;
  lineStyle: LineStyle;
  title: string;
  axisLabelVisible: boolean;
}

export interface ZoneSpec {
  key: string;
  lo: number;
  hi: number;
  fromTime: number;      // epoch seconds the zone became known
  color: string;
  label: string;
  broken: boolean;
}

export interface DrawPlan {
  lines: LineSpec[];
  zones: ZoneSpec[];
  counts: { levels: number; zones: number; setups: number };
}

/**
 * Which price lines and zones to draw.
 * - One price line per confluence group (the server merges levels of different family-tau within
 *   0.25·ATR_5m), coloured by the group's highest timeframe (pivots / prior day-week / round numbers
 *   count as 1D), styled by its dominant family, width 2 when it holds a 4H or 1D structure level
 *   (swing, zone, midpoint, fib) — the daily reference lines stay thin so they do not shout.
 * - Clutter control: only levels within ±1.5·ATR_1D of the last price.
 * - Zones: shaded bands from their known_from time to the right edge.
 * - Setups: a waiting setup's midpoint as a thick line ("15m BRK▲ buy .5") with faint "stop" (O) and
 *   "tgt" (X) lines; a filled one "IN .5 → tgt / stop"; identical setups on several timeframes are
 *   merged into one line.
 */
export function buildDrawPlan(p: SlPayload, chartLast: number | null, showSetups: boolean): DrawPlan {
  const last = chartLast ?? p.last_price;
  const win = p.atr1D !== null && p.atr1D > 0 ? 1.5 * p.atr1D : last !== null ? 0.03 * last : Infinity;
  const near = (lo: number, hi: number): boolean =>
    last === null || (hi >= last - win && lo <= last + win);
  const lines: LineSpec[] = [];
  const structureTf = new Map<string, SlTau>();
  for (const lv of p.levels) if ("SZMF".includes(lv.family)) structureTf.set(lv.id, lv.tf);
  const strong = (g: SlGroup): boolean => g.members.some((m) => {
    const tf = structureTf.get(m);
    return tf === "4H" || tf === "1D";
  });
  let nLevels = 0;
  for (const g of p.groups) {
    if (!near(g.price, g.price)) continue;
    const base = TAU_COLORS[g.tf];
    const title = g.state === "broken" ? `× ${g.label}` : g.state === "pending" ? `next ${g.label}` : g.label;
    lines.push({
      key: `g:${g.id}`, price: g.price,
      color: g.state === "active" ? base : withAlpha(base, 0.55),
      lineWidth: strong(g) ? 2 : 1,
      lineStyle: styleFor(g.family),
      title, axisLabelVisible: false,
    });
    nLevels += 1;
  }
  const zones: ZoneSpec[] = [];
  for (const lv of p.levels) {
    if (lv.family !== "Z" || !near(lv.lo, lv.hi)) continue;
    zones.push({
      key: lv.id, lo: lv.lo, hi: lv.hi, fromTime: lv.known_from, color: TAU_COLORS[lv.tf],
      label: lv.state === "broken" ? `× ${lv.label}` : lv.label, broken: lv.state !== "active",
    });
  }
  let nSetups = 0;
  if (showSetups) {
    // merge the same setup seen on several timeframes (same direction, O, X, M, status)
    const merged = new Map<string, { s: SlSetup; tfs: SlTau[]; origins: string[] }>();
    for (const s of p.setups) {
      if (!near(Math.min(s.O, s.X), Math.max(s.O, s.X))) continue;
      const k = `${s.status}|${s.direction}|${s.O.toFixed(2)}|${s.X.toFixed(2)}|${s.M.toFixed(2)}`;
      const m = merged.get(k);
      const abbr = ORIGIN_ABBR[s.origin] ?? s.origin;
      if (!m) merged.set(k, { s, tfs: [s.tf], origins: [abbr] });
      else {
        if (!m.tfs.includes(s.tf)) m.tfs.push(s.tf);
        if (!m.origins.includes(abbr)) m.origins.push(abbr);
      }
    }
    const seenAux = new Set<string>();
    const aux = (role: "stop" | "tgt", price: number, alpha: number): void => {
      const k = `${role}|${price.toFixed(2)}`;
      if (seenAux.has(k)) return;
      seenAux.add(k);
      lines.push({
        key: `a:${k}`, price, color: withAlpha(role === "stop" ? SHORT_COLOR : LONG_COLOR, alpha),
        lineWidth: 1, lineStyle: LineStyle.Dotted, title: role, axisLabelVisible: false,
      });
    };
    const order = (t: SlTau): number => SL_TAUS.indexOf(t);
    for (const { s, tfs, origins } of Array.from(merged.values()).slice(0, SETUP_LINE_LIMIT)) {
      tfs.sort((a, b) => order(a) - order(b));
      const tfTxt = tfs.join("/");
      const arrow = s.direction === "long" ? "▲" : "▼";
      const color = s.direction === "long" ? LONG_COLOR : SHORT_COLOR;
      if (s.status === "waiting") {
        lines.push({
          key: `s:${s.id}`, price: s.M, color, lineWidth: 3, lineStyle: LineStyle.Solid,
          title: `${tfTxt} ${origins.join("+")}${arrow} ${s.direction === "long" ? "buy" : "sell"} .5`,
          axisLabelVisible: true,
        });
        aux("stop", s.O, 0.55);
        aux("tgt", s.X, 0.55);
      } else if (s.status === "filled") {
        lines.push({
          key: `s:${s.id}`, price: s.M, color, lineWidth: 2, lineStyle: LineStyle.Solid,
          title: `${tfTxt} IN .5${arrow} → tgt / stop`, axisLabelVisible: true,
        });
        aux("stop", s.O, 0.8);
        aux("tgt", s.X, 0.8);
      } else {
        // resolved in the last 30 minutes: a faint marker of what happened
        const what = s.ambiguous ? "lost?" : s.status;
        lines.push({
          key: `s:${s.id}`, price: s.M, color: withAlpha(color, 0.35), lineWidth: 1,
          lineStyle: LineStyle.Dotted, title: `${tfTxt} .5 ${what}`, axisLabelVisible: false,
        });
      }
      nSetups += 1;
    }
  }
  return { lines, zones, counts: { levels: nLevels, zones: zones.length, setups: nSetups } };
}

// ---------------------------------------------------------------------------
// zones: a series primitive drawing shaded bands from known_from to the right edge
// ---------------------------------------------------------------------------

type DrawTarget = Parameters<IPrimitivePaneRenderer["draw"]>[0];

interface ZoneRect {
  x: number;
  yTop: number;
  yBot: number;
  fill: string;
  stroke: string;
  text: string;
  label: string;
}

class ZonesRenderer implements IPrimitivePaneRenderer {
  private readonly rects: ZoneRect[];

  constructor(rects: ZoneRect[]) {
    this.rects = rects;
  }

  draw(target: DrawTarget): void {
    target.useMediaCoordinateSpace(({ context: ctx, mediaSize }) => {
      for (const r of this.rects) {
        const x0 = Math.max(0, r.x);
        const w = mediaSize.width - x0;
        if (w <= 0) continue;
        const y0 = Math.min(r.yTop, r.yBot);
        const h = Math.max(1, Math.abs(r.yBot - r.yTop));
        ctx.fillStyle = r.fill;
        ctx.fillRect(x0, y0, w, h);
        ctx.strokeStyle = r.stroke;
        ctx.lineWidth = 1;
        ctx.beginPath();
        ctx.moveTo(x0, y0 + 0.5);
        ctx.lineTo(x0 + w, y0 + 0.5);
        ctx.moveTo(x0, y0 + h - 0.5);
        ctx.lineTo(x0 + w, y0 + h - 0.5);
        ctx.stroke();
        ctx.fillStyle = r.text;
        ctx.font = '10px ui-monospace, "SF Mono", Menlo, Consolas, monospace';
        ctx.fillText(r.label, x0 + 4, y0 + Math.min(h, 11) - 1);
      }
    });
  }
}

class ZonesPaneView implements IPrimitivePaneView {
  private readonly source: StructureZonesPrimitive;

  constructor(source: StructureZonesPrimitive) {
    this.source = source;
  }

  zOrder(): PrimitivePaneViewZOrder {
    return "bottom";
  }

  renderer(): IPrimitivePaneRenderer | null {
    return new ZonesRenderer(this.source.rects());
  }
}

export class StructureZonesPrimitive implements ISeriesPrimitive<Time> {
  private chart: IChartApiBase<Time> | null = null;
  private series: ISeriesApi<SeriesType, Time> | null = null;
  private requestUpdate: (() => void) | null = null;
  private zones: ZoneSpec[] = [];
  private cache: ZoneRect[] = [];
  private readonly views: readonly IPrimitivePaneView[];

  constructor() {
    this.views = [new ZonesPaneView(this)];
  }

  attached(param: SeriesAttachedParameter<Time, SeriesType>): void {
    this.chart = param.chart;
    this.series = param.series;
    this.requestUpdate = param.requestUpdate;
  }

  detached(): void {
    this.chart = null;
    this.series = null;
    this.requestUpdate = null;
  }

  setZones(zones: ZoneSpec[]): void {
    this.zones = zones;
    this.updateAllViews();
    try {
      this.requestUpdate?.();
    } catch {
      /* chart tearing down */
    }
  }

  updateAllViews(): void {
    const chart = this.chart;
    const series = this.series;
    const out: ZoneRect[] = [];
    if (chart && series) {
      try {
        const ts = chart.timeScale();
        for (const z of this.zones) {
          const idx = ts.timeToIndex(z.fromTime as UTCTimestamp, true);
          if (idx === null) continue;
          const x = ts.logicalToCoordinate(idx as unknown as Logical);
          const yTop = series.priceToCoordinate(z.hi);
          const yBot = series.priceToCoordinate(z.lo);
          if (x === null || yTop === null || yBot === null) continue;
          out.push({
            x, yTop, yBot, label: z.label,
            fill: withAlpha(z.color, z.broken ? 0.06 : 0.12),
            stroke: withAlpha(z.color, z.broken ? 0.2 : 0.35),
            text: withAlpha(z.color, 0.9),
          });
        }
      } catch {
        /* no data yet / tearing down */
      }
    }
    this.cache = out;
  }

  paneViews(): readonly IPrimitivePaneView[] {
    return this.views;
  }

  rects(): ZoneRect[] {
    return this.cache;
  }
}

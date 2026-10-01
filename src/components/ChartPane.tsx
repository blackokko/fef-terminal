import { useEffect, useMemo, useRef, useState } from "react";
import {
  CandlestickSeries,
  ColorType,
  CrosshairMode,
  LineSeries,
  LineStyle,
  createChart,
  type AutoscaleInfo,
  type CandlestickData,
  type IChartApi,
  type IPriceLine,
  type ISeriesApi,
  type LineData,
  type UTCTimestamp,
} from "lightweight-charts";
import { useSidecar } from "../context/WebSocketContext";
import {
  SL_FAMILIES,
  SL_TAUS,
  StructureZonesPrimitive,
  TAU_COLORS,
  buildDrawPlan,
  loadStructureSettings,
  parseStructurePayload,
  saveStructureSettings,
  structureNote,
  structureQuery,
  type StructureSettings,
} from "./structureLevels";
// [REMOVED 2026-08-25] `import type { IterEvent, WallRow }` and
// `formatExposureM` — both existed only to build the GEX/DEX wall price lines
// and the NTM max-pain line. The QuantData API and data subscription were
// CANCELLED, so those overlays were removed. Nothing else in this file used
// them. The StdDev regression channel below is NOT affected — it is computed
// locally from price bars.

const API_BASE = "http://127.0.0.1:8765";

// Display chart times in America/Chicago, 12-hour with lowercase am/pm.
// Per user preference: NEVER show military / UTC time anywhere.
//
// lightweight-charts passes `Time` which can be:
//   - UTCTimestamp (number, seconds since epoch) — intraday charts
//   - BusinessDay ({year, month, day}) — daily/weekly timeframes
//   - string ("YYYY-MM-DD")
// We normalize to a Date and wrap in try/catch so a single bad value
// never blanks the entire chart.
function _toDate(time: unknown): Date | null {
  try {
    if (typeof time === "number" && isFinite(time)) {
      return new Date(time * 1000);
    }
    if (typeof time === "string" && time.length > 0) {
      const d = new Date(time);
      return isNaN(d.getTime()) ? null : d;
    }
    if (typeof time === "object" && time !== null && "year" in (time as any)) {
      const b = time as { year: number; month: number; day: number };
      return new Date(b.year, b.month - 1, b.day);
    }
  } catch { /* fall through */ }
  return null;
}

function _fmtChartTime(time: unknown, includeDate: boolean, includeSeconds: boolean): string {
  const date = _toDate(time);
  if (!date) return "";
  try {
    const parts = new Intl.DateTimeFormat("en-US", {
      timeZone: "America/Chicago",
      hour: "numeric",
      minute: "2-digit",
      ...(includeSeconds ? { second: "2-digit" } : {}),
      hour12: true,
    }).formatToParts(date);
    let h = "", m = "", s = "", period = "";
    for (const p of parts) {
      if (p.type === "hour") h = p.value;
      else if (p.type === "minute") m = p.value;
      else if (p.type === "second") s = p.value;
      else if (p.type === "dayPeriod") period = p.value.toLowerCase();
    }
    const timeStr = includeSeconds ? `${h}:${m}:${s}${period}` : `${h}:${m}${period}`;
    if (!includeDate) return timeStr;
    const datePart = new Intl.DateTimeFormat("en-US", {
      timeZone: "America/Chicago",
      // Year included so the crosshair tooltip reads e.g. "Jul 10, 2026,
      // 12:00pm" — the user does multi-YEAR lookback and must see the year.
      year: "numeric", month: "short", day: "numeric",
    }).format(date);
    return `${datePart}, ${timeStr}`;
  } catch {
    return "";
  }
}

// Axis tick-mark formatter. lightweight-charts calls tickMarkFormatter with
// (time, tickMarkType, locale). The OLD formatter ignored tickMarkType and
// always showed time-only, which loses all date context on daily/multi-year
// charts. tickMarkType tells us the granularity of each tick, so we show:
//   year ticks -> "2026", month ticks -> "Jul '26",
//   day ticks  -> "Jul 10", intraday ticks -> the time.
// Enum values (a const enum in lightweight-charts — NOT imported, so
// isolatedModules builds stay clean): 0=Year 1=Month 2=DayOfMonth 3=Time
// 4=TimeWithSeconds. All formatting stays in America/Chicago; never blank
// the chart on a bad value (try/catch -> "").
function _fmtAxisTick(time: unknown, tickMarkType: number, _locale: string): string {
  const date = _toDate(time);
  if (!date) return "";
  try {
    if (tickMarkType === 0) {
      return new Intl.DateTimeFormat("en-US", {
        timeZone: "America/Chicago", year: "numeric",
      }).format(date);
    }
    if (tickMarkType === 1) {
      const mon = new Intl.DateTimeFormat("en-US", {
        timeZone: "America/Chicago", month: "short",
      }).format(date);
      const yy = new Intl.DateTimeFormat("en-US", {
        timeZone: "America/Chicago", year: "2-digit",
      }).format(date);
      return `${mon} '${yy}`;
    }
    if (tickMarkType === 2) {
      return new Intl.DateTimeFormat("en-US", {
        timeZone: "America/Chicago", month: "short", day: "numeric",
      }).format(date);
    }
    // Intraday ticks: 3 = Time, 4 = TimeWithSeconds
    return _fmtChartTime(time, false, tickMarkType === 4);
  } catch {
    return "";
  }
}

const DARK_THEME = {
  layout: {
    background: { type: ColorType.Solid, color: "#0e1116" },
    textColor: "#7d8590",
    fontFamily: 'ui-monospace, "SF Mono", Menlo, Consolas, monospace',
  },
  grid: { vertLines: { color: "#1c222b" }, horzLines: { color: "#1c222b" } },
  crosshair: { mode: CrosshairMode.Normal },
  timeScale: {
    borderColor: "#2a323e", timeVisible: true, secondsVisible: false,
    tickMarkFormatter: (time: unknown, tickMarkType: number, locale: string) =>
      _fmtAxisTick(time, tickMarkType, locale),
  },
  rightPriceScale: { borderColor: "#2a323e" },
  localization: {
    timeFormatter: (time: unknown) => {
      const formatted = _fmtChartTime(time, true, true);
      return formatted ? `${formatted} CT` : "";
    },
  },
};

const CANDLE_OPTIONS = {
  upColor: "#3fb950", downColor: "#f85149",
  borderUpColor: "#3fb950", borderDownColor: "#f85149",
  wickUpColor: "#3fb950", wickDownColor: "#f85149",
};

// Ambient price-line colors. [RENAMED + TRIMMED 2026-08-25] was WALL_COLORS,
// which also carried GEX_CALL / GEX_PUT / DEX_CALL / DEX_PUT / MAX_PAIN. Those
// five drove QuantData-sourced overlays; the QuantData API and data
// subscription were CANCELLED and the overlays removed with them. What is left
// is derived from the loaded bars, not from any subscription.
const LEVEL_COLORS = {
  DAY_HIGH: "#3fb950", DAY_LOW: "#f85149",
  VWAP: "#58a6ff",
};

const TIMEFRAMES = [
  { label: "1m", value: "1 min" },
  { label: "2m", value: "2 mins" },
  { label: "5m", value: "5 mins" },
  { label: "15m", value: "15 mins" },
  { label: "30m", value: "30 mins" },
  { label: "1h", value: "1 hour" },
  { label: "4h", value: "4 hours" },
  { label: "1D", value: "1 day" },
  { label: "1W", value: "1 week" },
];

// Tick-count resolutions. SPX has no trade tape, so tick candles are built
// from the ES futures AllLast stream the sidecar already subscribes to for
// microstructure — the pane is labeled "ES ticks" when one is active.
const TICK_TIMEFRAMES = [
  { label: "1000T", value: "1000T", n: 1000 },
  { label: "2000T", value: "2000T", n: 2000 },
];

// Smart default lookback per timeframe. Conservative — picks the largest
// window that's KNOWN to stay within IBKR's per-request max for ALL symbols
// (SPX index, ES futures, etc.). Empirically verified:
//   1h × 6M+ → IBKR times out for ES
//   4h × 1Y  → IBKR times out for ES
// So defaults stay below those caps. User can override via Lookback dropdown
// for individual fetches (which may then fail per IBKR rules).
const DEFAULT_LOOKBACK_BY_TIMEFRAME: Record<string, string> = {
  "1 min":  "1 D",
  "2 mins": "2 D",
  "5 mins": "1 W",
  "15 mins": "2 W",
  "30 mins": "1 M",
  "1 hour": "1 M",
  "4 hours": "3 M",
  "1 day":  "5 Y",
  "1 week": "10 Y",
};

// All lookback options the user can pick from. IBKR enforces per-bar-size
// maxes — if you pick a window larger than allowed, the request errors and
// the chart shows the error.
const LOOKBACK_OPTIONS = [
  { label: "1D", value: "1 D" },
  { label: "2D", value: "2 D" },
  { label: "1W", value: "1 W" },
  { label: "10D", value: "10 D" },  // Dale's 10D/30min regression preset window
  { label: "2W", value: "2 W" },
  { label: "1M", value: "1 M" },
  { label: "3M", value: "3 M" },
  { label: "6M", value: "6 M" },
  { label: "1Y", value: "1 Y" },
  { label: "2Y", value: "2 Y" },
  { label: "5Y", value: "5 Y" },
  { label: "10Y", value: "10 Y" },
  { label: "20Y", value: "20 Y" },
];

const MIN_Y_RANGE_PTS = 50;
const POLL_INTERVAL_MS = 3000;  // refresh historical bars every 3s
const TICK_POLL_INTERVAL_MS = 1000;     // tick candles are in-memory — poll fast
const PATTERN_POLL_INTERVAL_MS = 5000;  // pattern overlay refresh
const REGRESSION_POLL_INTERVAL_MS = 15000;  // regression channel refresh — the
// server caches bars ~25min so repeated polls are cheap; this just eventually
// picks up a freshly-closed bar into the fit.
const LEVELS_POLL_INTERVAL_MS = 30000;  // structure levels refresh — the server's 1-min
// bars are cached 30s, history 25min; levels move at most once a bar.

// Pattern overlay payload (GET /chart/pattern_overlay). Times are epoch SECONDS.
// We now consume ONLY the auto pivot trendlines — the candlestick-pattern
// markers and measured-move flag target lines were dropped from the chart
// (backend still returns them; the frontend just ignores those fields).
interface TrendlineOverlayPayload {
  trendlines: { kind: string; points: [number, number][]; projected: [number, number] | null }[];
}

// Regression channel payload (GET /chart/regression_channel). Times are epoch
// SECONDS. The independent StdDev-channel OVERLAY draws the full ±1σ/2σ/3σ nest
// (same bands as the chart-following StdDev channel, just fit over its own
// bar_size/lookback window).
interface RegressionChannelPayload {
  center: { time: number; value: number }[];
  upper_1: { time: number; value: number }[];
  upper_2: { time: number; value: number }[];
  upper_3: { time: number; value: number }[];
  lower_1: { time: number; value: number }[];
  lower_2: { time: number; value: number }[];
  lower_3: { time: number; value: number }[];
  bar_count: number;
  status: string;
}

// Apply a regression-channel payload (center + ±1/2/3σ arrays, epoch-second
// times) to the overlay's seven line series. Pure fn of its args — module
// scope so it isn't recreated each render / needed in effect deps.
type RegRefs = {
  center: ISeriesApi<"Line"> | null;
  up1: ISeriesApi<"Line"> | null; up2: ISeriesApi<"Line"> | null; up3: ISeriesApi<"Line"> | null;
  dn1: ISeriesApi<"Line"> | null; dn2: ISeriesApi<"Line"> | null; dn3: ISeriesApi<"Line"> | null;
};
function _applyChannel(refs: RegRefs, data: RegressionChannelPayload): void {
  const toLine = (arr: { time: number; value: number }[] | undefined): LineData[] =>
    (arr ?? []).map((p) => ({ time: p.time as UTCTimestamp, value: p.value }));
  try {
    refs.center?.setData(toLine(data.center));
    refs.up1?.setData(toLine(data.upper_1));
    refs.up2?.setData(toLine(data.upper_2));
    refs.up3?.setData(toLine(data.upper_3));
    refs.dn1?.setData(toLine(data.lower_1));
    refs.dn2?.setData(toLine(data.lower_2));
    refs.dn3?.setData(toLine(data.lower_3));
  } catch { /* chart may be tearing down */ }
}

// Fit-window (lookback) options for the overlay's lookback dropdown.
const REG_LOOKBACKS = [
  { label: "5D", value: "5 D" },
  { label: "10D", value: "10 D" },
  { label: "1M", value: "1 M" },
  { label: "3M", value: "3 M" },
  { label: "6M", value: "6 M" },
  { label: "1Y", value: "1 Y" },
];
// Bar-size options for the INDEPENDENT overlay (mode B), decoupled from the
// chart timeframe.
const REG_BAR_SIZES = [
  { label: "5m", value: "5 mins" },
  { label: "30m", value: "30 mins" },
  { label: "1h", value: "1 hour" },
  { label: "1D", value: "1 day" },
];
// Dale's one-click regression setups: [label, duration, bar_size].
const REG_PRESETS: { label: string; duration: string; barSize: string }[] = [
  { label: "6mo·1D", duration: "6 M", barSize: "1 day" },
  { label: "10D·30m", duration: "10 D", barSize: "30 mins" },
  { label: "5D·5m", duration: "5 D", barSize: "5 mins" },
];

// [REMOVED 2026-08-25] `AggregatedWall` + `aggregateWalls()`. They merged the
// top-5 GEX and top-5 DEX rows from IterEvent.snapshot into one price line per
// strike and carried NTM max pain alongside. Every input came from QuantData,
// whose API and data subscription were CANCELLED, so the whole overlay was
// removed rather than left drawing a frozen last snapshot on a live chart.

interface FetchedBar {
  timestamp_ms: number; open: number; high: number; low: number; close: number; volume: number;
}

export function ChartPane() {
  const containerRef = useRef<HTMLDivElement | null>(null);
  const chartRef = useRef<IChartApi | null>(null);
  const seriesRef = useRef<ISeriesApi<"Candlestick"> | null>(null);
  const ema8SeriesRef = useRef<ISeriesApi<"Line"> | null>(null);
  const ema21SeriesRef = useRef<ISeriesApi<"Line"> | null>(null);
  // Standard deviation channels — 1σ / 2σ / 3σ on both sides of the
  // 20-period mean. Stored as a single object so toggle + setData is simpler.
  const sdRefs = useRef<{
    mid: ISeriesApi<"Line"> | null;
    up1: ISeriesApi<"Line"> | null; up2: ISeriesApi<"Line"> | null; up3: ISeriesApi<"Line"> | null;
    dn1: ISeriesApi<"Line"> | null; dn2: ISeriesApi<"Line"> | null; dn3: ISeriesApi<"Line"> | null;
  }>({ mid: null, up1: null, up2: null, up3: null, dn1: null, dn2: null, dn3: null });
  const [showEMA, setShowEMA] = useState(true);
  const [showSD, setShowSD] = useState(false);
  // Auto-trendline overlay (sidecar-detected support/resistance pivot rays).
  // NOTE: the candlestick-pattern markers + measured-move flag target lines
  // were removed — this toggle now draws ONLY the trendline rays.
  const [showTrendlines, setShowTrendlines] = useState(false);
  const trendSupportRef = useRef<ISeriesApi<"Line"> | null>(null);
  const trendResistRef = useRef<ISeriesApi<"Line"> | null>(null);
  // Independent StdDev-channel OVERLAY — a SECOND channel, server-fit via
  // /chart/regression_channel at its OWN bar_size/lookback, decoupled from the
  // chart timeframe (e.g. sit on a 5-min chart, overlay the 6mo-daily channel).
  // Draws the full ±1σ/2σ/3σ nest, same as the chart-following StdDev channel.
  const overlayRef = useRef<RegRefs>({
    center: null, up1: null, up2: null, up3: null, dn1: null, dn2: null, dn3: null,
  });
  const [showOverlay, setShowOverlay] = useState(false);
  const [overlayBarSize, setOverlayBarSize] = useState("1 day");
  const [overlayLookback, setOverlayLookback] = useState("6 M");
  // Multi-timeframe STRUCTURE LEVELS (/chart/structure_levels): swings, zones, midpoints, fibs,
  // pivots, prior day/week, round numbers + MIDENTRY setups. Display only — see structureLevels.ts.
  const [showLevels, setShowLevels] = useState(false);
  const [levelsSettings, setLevelsSettings] = useState<StructureSettings>(() => loadStructureSettings());
  const [levelsMenuOpen, setLevelsMenuOpen] = useState(false);
  const [levelsNote, setLevelsNote] = useState<string | null>(null);
  const levelLinesRef = useRef<IPriceLine[]>([]);
  const levelZonesRef = useRef<StructureZonesPrimitive | null>(null);
  const levelsMenuRef = useRef<HTMLSpanElement | null>(null);
  const lastCloseRef = useRef<number | null>(null);
  // [REMOVED 2026-08-25] priceLinesRef — held the GEX/DEX/max-pain price-line
  // handles. Its only writer was the wall-overlay effect, deleted with the
  // QuantData subscription. ambientLinesRef (Day H/L) is unrelated and stays.
  const ambientLinesRef = useRef<IPriceLine[]>([]);
  const renderedBarCountRef = useRef(0);

  // [REMOVED 2026-08-25] `lastIter` was destructured here only to feed the wall
  // overlay. QuantData subscription CANCELLED; overlay gone.
  const { lastTick, activeSymbol } = useSidecar();
  const symbol = activeSymbol ?? "SPX";  // dropdown in TopBar is single source of truth
  const [timeframe, setTimeframe] = useState<string>("2 mins");
  const [bars, setBars] = useState<FetchedBar[]>([]);
  // Tick-candle mode: "1000T" / "2000T" resolutions chart ES trade prints
  // instead of time bars for the selected symbol.
  const tickTf = TICK_TIMEFRAMES.find((t) => t.value === timeframe) ?? null;
  const isTickTf = tickTf !== null;
  const [tickFeedNote, setTickFeedNote] = useState<string | null>(null);
  const [hasFittedOnce, setHasFittedOnce] = useState(false);
  // YYYY-MM-DD or "" (today/live)
  const [historicalDate, setHistoricalDate] = useState<string>("");
  const [availableDates, setAvailableDates] = useState<string[]>([]);
  // "" = use smart default per timeframe; explicit value = user override.
  const [lookbackOverride, setLookbackOverride] = useState<string>("");
  const effectiveLookback = lookbackOverride
    || DEFAULT_LOOKBACK_BY_TIMEFRAME[timeframe]
    || "1 D";

  // Create chart once
  useEffect(() => {
    if (!containerRef.current) return;
    const chart = createChart(containerRef.current, { ...DARK_THEME, autoSize: true });
    const series = chart.addSeries(CandlestickSeries, {
      ...CANDLE_OPTIONS,
      autoscaleInfoProvider: (original: () => AutoscaleInfo | null): AutoscaleInfo | null => {
        const r = original();
        if (!r || !r.priceRange) return r;
        const range = r.priceRange.maxValue - r.priceRange.minValue;
        if (range < MIN_Y_RANGE_PTS) {
          const mid = (r.priceRange.maxValue + r.priceRange.minValue) / 2;
          return {
            priceRange: { minValue: mid - MIN_Y_RANGE_PTS / 2, maxValue: mid + MIN_Y_RANGE_PTS / 2 },
            margins: r.margins,
          };
        }
        return r;
      },
    });
    // EMA 8/21 — classic fast/slow momentum.
    // EMA8 = orange (faster), EMA21 = cyan (slower). Cross = trend shift signal.
    const ema8 = chart.addSeries(LineSeries, {
      color: "#f0883e", lineWidth: 2,
      priceLineVisible: false, lastValueVisible: false,
    });
    const ema21 = chart.addSeries(LineSeries, {
      color: "#58a6ff", lineWidth: 2,
      priceLineVisible: false, lastValueVisible: false,
    });
    // Standard deviation channels — 20-period mean ± 1σ / 2σ / 3σ both sides.
    // 1σ = light gray (dotted), 2σ = amber (dashed), 3σ = red (solid).
    // Outer bands are stronger visual = "extreme" zones; closer = "normal".
    const sdLine = (color: string, width: 1 | 2, style: LineStyle) =>
      chart.addSeries(LineSeries, {
        color, lineWidth: width, lineStyle: style,
        priceLineVisible: false, lastValueVisible: false, visible: false,
      });
    const mid = sdLine("#7d8590", 1, LineStyle.Dotted);
    const up1 = sdLine("#a0a8b3", 1, LineStyle.Dotted);
    const dn1 = sdLine("#a0a8b3", 1, LineStyle.Dotted);
    const up2 = sdLine("#d29922", 1, LineStyle.Dashed);
    const dn2 = sdLine("#d29922", 1, LineStyle.Dashed);
    const up3 = sdLine("#f85149", 2, LineStyle.Solid);
    const dn3 = sdLine("#f85149", 2, LineStyle.Solid);
    // Pattern-overlay auto trendlines — support (green) / resistance (red)
    // rays through the last two pivot lows/highs, projected to the newest bar.
    const trendSupport = chart.addSeries(LineSeries, {
      color: "#3fb950", lineWidth: 2,
      priceLineVisible: false, lastValueVisible: false,
    });
    const trendResist = chart.addSeries(LineSeries, {
      color: "#f85149", lineWidth: 2,
      priceLineVisible: false, lastValueVisible: false,
    });
    // Independent StdDev-channel OVERLAY — full ±1σ/2σ/3σ nest in a PINK family
    // (center magenta, 1σ light pink, 2σ pink, 3σ deep magenta), all DASHED so
    // it's never confused with the chart-following StdDev channel (gray/amber/
    // red, dotted/dashed/solid) or the EMAs (orange/cyan).
    const ovLine = (color: string, width: 1 | 2) =>
      chart.addSeries(LineSeries, {
        color, lineWidth: width, lineStyle: LineStyle.Dashed,
        priceLineVisible: false, lastValueVisible: false, visible: false,
      });
    const ovCenter = ovLine("#f778ba", 2);
    const ovUp1 = ovLine("#f0b3d4", 1);
    const ovDn1 = ovLine("#f0b3d4", 1);
    const ovUp2 = ovLine("#db61a2", 1);
    const ovDn2 = ovLine("#db61a2", 1);
    const ovUp3 = ovLine("#bf4b8a", 2);
    const ovDn3 = ovLine("#bf4b8a", 2);

    chartRef.current = chart;
    seriesRef.current = series;
    ema8SeriesRef.current = ema8;
    ema21SeriesRef.current = ema21;
    sdRefs.current = { mid, up1, up2, up3, dn1, dn2, dn3 };
    trendSupportRef.current = trendSupport;
    trendResistRef.current = trendResist;
    overlayRef.current = {
      center: ovCenter, up1: ovUp1, up2: ovUp2, up3: ovUp3, dn1: ovDn1, dn2: ovDn2, dn3: ovDn3,
    };

    const resize = () => {
      if (containerRef.current && chartRef.current) {
        chartRef.current.applyOptions({
          width: containerRef.current.clientWidth,
          height: containerRef.current.clientHeight,
        });
      }
    };
    const observer = new ResizeObserver(resize);
    observer.observe(containerRef.current);
    return () => {
      observer.disconnect();
      chart.remove();
      chartRef.current = null;
      seriesRef.current = null;
      ema8SeriesRef.current = null;
      ema21SeriesRef.current = null;
      sdRefs.current = { mid: null, up1: null, up2: null, up3: null, dn1: null, dn2: null, dn3: null };
      trendSupportRef.current = null;
      trendResistRef.current = null;
      overlayRef.current = {
        center: null, up1: null, up2: null, up3: null, dn1: null, dn2: null, dn3: null,
      };
      ambientLinesRef.current = [];
      renderedBarCountRef.current = 0;
    };
  }, []);

  // Poll /bars every 3s for live. For historical (non-empty date), one-shot
  // fetch from /history/bars (no need to re-poll since the day is closed).
  const [fetchError, setFetchError] = useState<string | null>(null);
  useEffect(() => {
    let cancelled = false;
    const fetchBars = async () => {
      try {
        if (tickTf) {
          // Tick-candle mode — ES trade prints aggregated sidecar-side.
          // In-memory endpoint; no IBKR request per poll.
          const r = await fetch(
            `${API_BASE}/chart/tick_candles?symbol=ES&n=${tickTf.n}`
          );
          if (cancelled) return;
          if (!r.ok) {
            setFetchError(`HTTP ${r.status}`);
            return;
          }
          const data = (await r.json()) as {
            candles: FetchedBar[];
            status: { streaming: boolean; source: string; detail: string };
          };
          if (cancelled) return;
          setBars(data.candles);
          setFetchError(null);
          setTickFeedNote(
            data.status.streaming
              ? `ES ticks (${data.status.source})`
              : `no tick feed — ${data.status.detail}`
          );
          return;
        }
        let url: string;
        if (historicalDate) {
          const dayStart = new Date(`${historicalDate}T00:00:00`).getTime();
          const dayEnd = dayStart + 86_400_000;
          url = `${API_BASE}/history/bars?symbol=${encodeURIComponent(symbol)}&bar_size=${encodeURIComponent(timeframe)}&from_ms=${dayStart}&to_ms=${dayEnd}&limit=5000`;
        } else {
          url = `${API_BASE}/bars?symbol=${encodeURIComponent(symbol)}&bar_size=${encodeURIComponent(timeframe)}&duration=${encodeURIComponent(effectiveLookback)}`;
        }
        const r = await fetch(url);
        if (cancelled) return;
        if (!r.ok) {
          setFetchError(`HTTP ${r.status}`);
          return;
        }
        const data = (await r.json()) as FetchedBar[];
        if (!cancelled) {
          setBars(data);
          setFetchError(null);
        }
      } catch (e) {
        setFetchError(`fetch failed: ${(e as Error).message}`);
        console.error("bars fetch failed:", e);
      }
    };
    // Reset render state on symbol/timeframe/date change. CRITICAL: also
    // clear the chart's data + all overlay lines + the bars state so we
    // don't show stale candles/H-L/walls from the previous symbol while
    // the new fetch is in flight.
    renderedBarCountRef.current = 0;
    setHasFittedOnce(false);
    setBars([]);
    setTickFeedNote(null);
    try {
      seriesRef.current?.setData([]);
      ema8SeriesRef.current?.setData([]);
      ema21SeriesRef.current?.setData([]);
      const sd = sdRefs.current;
      sd.mid?.setData([]); sd.up1?.setData([]); sd.up2?.setData([]); sd.up3?.setData([]);
      sd.dn1?.setData([]); sd.dn2?.setData([]); sd.dn3?.setData([]);
      // Trendline rays are stale on any symbol/timeframe/date change — clear
      // them. (The regression channels A/B manage their own clear/refetch in
      // their dedicated effects.)
      trendSupportRef.current?.setData([]);
      trendResistRef.current?.setData([]);
      if (seriesRef.current) {
        // (The priceLinesRef sweep that stood here cleared the QuantData
        // GEX/DEX/max-pain lines — removed 2026-08-25 with that subscription.)
        for (const line of ambientLinesRef.current) seriesRef.current.removePriceLine(line);
        ambientLinesRef.current = [];
      }
      // Re-enable price-axis autoscale. Without this, the Y-axis stays
      // pinned to the previous symbol's range (e.g. SMH ~270) when you
      // switch to a different price regime (ES ~7400), forcing the user
      // to scroll/zoom the price axis manually. fitContent() only fits
      // the time scale; price autoscale gets disabled the moment the user
      // wheel-zooms on the Y-axis, so we explicitly re-enable it here.
      chartRef.current?.priceScale("right").applyOptions({ autoScale: true });
    } catch {}
    void fetchBars();
    // Historical mode: no polling (the day is closed). Live mode: poll every
    // 3s (time bars) / 1s (in-memory tick candles).
    if (historicalDate && !tickTf) return () => { cancelled = true; };
    const id = window.setInterval(
      fetchBars, tickTf ? TICK_POLL_INTERVAL_MS : POLL_INTERVAL_MS,
    );
    return () => {
      cancelled = true;
      window.clearInterval(id);
    };
  }, [symbol, timeframe, historicalDate, effectiveLookback]);  // tickTf derives from timeframe

  // Refresh available historical dates whenever symbol changes
  useEffect(() => {
    let cancelled = false;
    void (async () => {
      try {
        const r = await fetch(`${API_BASE}/history/dates?symbol=${encodeURIComponent(symbol)}`);
        if (!r.ok || cancelled) return;
        setAvailableDates(await r.json());
      } catch {
        // ignore — dates list is optional
      }
    })();
    return () => { cancelled = true; };
  }, [symbol, timeframe]);

  // Auto-trendline overlay — poll the sidecar's detector endpoint and render
  // ONLY the two pivot trendline rays (support green / resistance red).
  // Candlestick-pattern markers and flag measured-move target lines were
  // intentionally dropped from the chart (the endpoint still returns them; we
  // just don't consume them). Live time-bars only: tick candles are ES prints
  // (different price basis) and historical replay would need a date-window
  // variant of the endpoint, so both clear the overlay instead.
  useEffect(() => {
    const clearOverlay = () => {
      try {
        trendSupportRef.current?.setData([]);
        trendResistRef.current?.setData([]);
      } catch { /* chart may be tearing down */ }
    };
    if (!showTrendlines || isTickTf || historicalDate) {
      clearOverlay();
      return;
    }
    let cancelled = false;
    const fetchOverlay = async () => {
      try {
        const url = `${API_BASE}/chart/pattern_overlay?symbol=${encodeURIComponent(symbol)}&bar_size=${encodeURIComponent(timeframe)}&lookback=400`;
        const r = await fetch(url);
        if (!r.ok || cancelled) return;
        const data = (await r.json()) as TrendlineOverlayPayload;
        if (cancelled || !seriesRef.current) return;

        // Trendlines — draw from the older pivot to the projection at the
        // newest bar (the sidecar computes the ray in bar-index space).
        const applyTrend = (kind: string, series: ISeriesApi<"Line"> | null) => {
          if (!series) return;
          const t = (data.trendlines ?? []).find((x) => x.kind === kind);
          if (!t || !t.projected || t.points.length < 2) {
            series.setData([]);
            return;
          }
          const pts: LineData[] = [
            { time: t.points[0][0] as UTCTimestamp, value: t.points[0][1] },
            { time: t.projected[0] as UTCTimestamp, value: t.projected[1] },
          ];
          // Times must be ascending and distinct for setData
          if ((pts[1].time as number) > (pts[0].time as number)) series.setData(pts);
          else series.setData([]);
        };
        applyTrend("support", trendSupportRef.current);
        applyTrend("resistance", trendResistRef.current);
      } catch (e) {
        console.error("trendline overlay fetch failed:", e);
      }
    };
    void fetchOverlay();
    const id = window.setInterval(fetchOverlay, PATTERN_POLL_INTERVAL_MS);
    return () => {
      cancelled = true;
      window.clearInterval(id);
      clearOverlay();
    };
  }, [showTrendlines, symbol, timeframe, historicalDate, isTickTf]);

  // Independent StdDev-channel OVERLAY — server-fit via /chart/regression_channel
  // at its OWN (bar_size, lookback), decoupled from the chart timeframe (e.g. sit
  // on a 5-min chart, overlay the 6mo-daily channel). Draws the full ±1/2/3σ nest
  // via _applyChannel. Live time-bars only.
  useEffect(() => {
    const refs = overlayRef.current;
    const clear = () => {
      try {
        refs.center?.setData([]);
        refs.up1?.setData([]); refs.up2?.setData([]); refs.up3?.setData([]);
        refs.dn1?.setData([]); refs.dn2?.setData([]); refs.dn3?.setData([]);
      } catch {}
    };
    if (!showOverlay || isTickTf || historicalDate) { clear(); return; }
    let cancelled = false;
    const fetchChannel = async () => {
      try {
        const url = `${API_BASE}/chart/regression_channel?symbol=${encodeURIComponent(symbol)}`
          + `&bar_size=${encodeURIComponent(overlayBarSize)}&duration=${encodeURIComponent(overlayLookback)}`;
        const r = await fetch(url);
        if (!r.ok || cancelled) return;
        const data = (await r.json()) as RegressionChannelPayload;
        if (cancelled) return;
        _applyChannel(refs, data);
      } catch (e) {
        console.error("stddev overlay fetch failed:", e);
      }
    };
    void fetchChannel();
    const id = window.setInterval(fetchChannel, REGRESSION_POLL_INTERVAL_MS);
    return () => { cancelled = true; window.clearInterval(id); clear(); };
  }, [showOverlay, symbol, overlayBarSize, overlayLookback, isTickTf, historicalDate]);

  // Structure levels — poll /chart/structure_levels every 30s while on (and at once on toggle,
  // symbol, timeframe or settings change); one price line per confluence group, zones as shaded
  // bands (series primitive), MIDENTRY setups as entry / stop / target lines. Live time-bars only,
  // like Trendlines. Everything is removed on toggle-off, symbol / timeframe / date change and
  // unmount. A response that arrives after its effect was torn down is dropped.
  useEffect(() => { saveStructureSettings(levelsSettings); }, [levelsSettings]);
  useEffect(() => {
    lastCloseRef.current = bars.length ? bars[bars.length - 1].close : null;
  }, [bars]);
  useEffect(() => {
    const removeAll = () => {
      const s = seriesRef.current;
      try {
        if (s) {
          for (const l of levelLinesRef.current) s.removePriceLine(l);
          if (levelZonesRef.current) s.detachPrimitive(levelZonesRef.current);
        }
      } catch { /* chart may be tearing down */ }
      levelLinesRef.current = [];
      levelZonesRef.current = null;
    };
    const series = seriesRef.current;
    if (!showLevels || isTickTf || historicalDate || !series) {
      removeAll();
      setLevelsNote(null);
      return;
    }
    let cancelled = false;
    let inFlight = false;
    const zones = new StructureZonesPrimitive();
    try {
      series.attachPrimitive(zones);
      levelZonesRef.current = zones;
    } catch { /* drawn without zones */ }
    const query = structureQuery(symbol, levelsSettings);
    const showSetups = levelsSettings.families.setups;
    const fetchLevels = async () => {
      if (inFlight) return;   // a slow IBKR fetch must not stack requests
      inFlight = true;
      try {
        const r = await fetch(`${API_BASE}/chart/structure_levels?${query}`);
        if (cancelled) return;
        if (!r.ok) { setLevelsNote(`levels: HTTP ${r.status}`); return; }
        const payload = parseStructurePayload(await r.json());
        if (cancelled) return;
        const s = seriesRef.current;
        if (!payload || !s) { setLevelsNote("levels: unreadable response"); return; }
        const plan = buildDrawPlan(payload, lastCloseRef.current, showSetups);
        for (const l of levelLinesRef.current) s.removePriceLine(l);
        levelLinesRef.current = plan.lines.map((ln) => s.createPriceLine({
          price: ln.price, color: ln.color, lineWidth: ln.lineWidth, lineStyle: ln.lineStyle,
          axisLabelVisible: ln.axisLabelVisible, title: ln.title,
        }));
        zones.setZones(plan.zones);
        setLevelsNote(structureNote(payload, plan.counts, showSetups));
      } catch (e) {
        if (!cancelled) setLevelsNote("levels: fetch failed");
        console.error("structure levels fetch failed:", e);
      } finally {
        inFlight = false;
      }
    };
    void fetchLevels();
    const id = window.setInterval(fetchLevels, LEVELS_POLL_INTERVAL_MS);
    return () => {
      cancelled = true;
      window.clearInterval(id);
      removeAll();
    };
  }, [showLevels, symbol, timeframe, historicalDate, isTickTf, levelsSettings]);

  // Close the levels settings popover on an outside click.
  useEffect(() => {
    if (!levelsMenuOpen) return;
    const onDown = (e: MouseEvent) => {
      const el = levelsMenuRef.current;
      if (el && e.target instanceof Node && !el.contains(e.target)) setLevelsMenuOpen(false);
    };
    document.addEventListener("mousedown", onDown);
    return () => document.removeEventListener("mousedown", onDown);
  }, [levelsMenuOpen]);

  // Toggle indicator visibility
  useEffect(() => {
    if (ema8SeriesRef.current) ema8SeriesRef.current.applyOptions({ visible: showEMA });
    if (ema21SeriesRef.current) ema21SeriesRef.current.applyOptions({ visible: showEMA });
  }, [showEMA]);
  useEffect(() => {
    const sd = sdRefs.current;
    for (const ref of [sd.mid, sd.up1, sd.up2, sd.up3, sd.dn1, sd.dn2, sd.dn3]) {
      ref?.applyOptions({ visible: showSD });
    }
  }, [showSD]);
  useEffect(() => {
    const r = overlayRef.current;
    for (const ref of [r.center, r.up1, r.up2, r.up3, r.dn1, r.dn2, r.dn3]) {
      ref?.applyOptions({ visible: showOverlay });
    }
  }, [showOverlay]);

  // Apply bars to the chart + ALWAYS recompute indicators when bars change.
  //
  // Previously the indicator recomputation was inside the 'count changed'
  // branch — which silently skipped recompute when a fresh /bars fetch
  // returned the same number of bars (very common when switching lookback).
  // That left stale EMA + StdDev channels on a fresh dataset.
  useEffect(() => {
    if (!seriesRef.current || bars.length === 0) return;
    const candleData: CandlestickData[] = bars.map((b) => ({
      time: Math.floor(b.timestamp_ms / 1000) as UTCTimestamp,
      open: b.open, high: b.high, low: b.low, close: b.close,
    }));
    // Dedupe by time (defense)
    const map = new Map<number, CandlestickData>();
    for (const c of candleData) map.set(c.time as number, c);
    const deduped = Array.from(map.values()).sort((a, b) => (a.time as number) - (b.time as number));

    if (deduped.length !== renderedBarCountRef.current) {
      seriesRef.current.setData(deduped);
      renderedBarCountRef.current = deduped.length;
    } else {
      // Same count — update the last bar in case its OHLC moved
      const last = deduped[deduped.length - 1];
      try { seriesRef.current.update(last); } catch {}
    }

    // --- Indicator recompute — ALWAYS, every time bars[] changes ---
    const closes = bars.map(b => b.close);
    const times = bars.map(b => Math.floor(b.timestamp_ms / 1000) as UTCTimestamp);

    // EMA 8/21 — recursive: EMA_t = α·close + (1-α)·EMA_{t-1}, α = 2/(N+1)
    const ema = (period: number): LineData[] => {
      const α = 2 / (period + 1);
      const out: LineData[] = [];
      let prev: number | null = null;
      for (let i = 0; i < closes.length; i++) {
        if (prev == null) {
          if (i >= period - 1) {
            const seed = closes.slice(0, period).reduce((s, x) => s + x, 0) / period;
            prev = seed;
            out.push({ time: times[i], value: seed });
          }
        } else {
          prev = α * closes[i] + (1 - α) * prev;
          out.push({ time: times[i], value: prev });
        }
      }
      return out;
    };
    if (ema8SeriesRef.current) ema8SeriesRef.current.setData(ema(8));
    if (ema21SeriesRef.current) ema21SeriesRef.current.setData(ema(21));

    // Standard Deviation Channel — TOS StandardDevChannel.
    //
    // y_i = close at bar i, x_i = i (relative index, 0..n-1)
    //
    // Centered OLS for numerical stability:
    //   xbar = mean(x), ybar = mean(y)
    //   sxx = Σ (x_i - xbar)²
    //   sxy = Σ (x_i - xbar) (y_i - ybar)
    //   slope m = sxy / sxx
    //   intercept b = ybar - m * xbar
    //
    // The textbook non-centered form (n·Σxy − Σx·Σy)/(n·Σx² − Σx²)
    // subtracts two large nearly-equal numbers for big n and loses ~6-10
    // significant digits — that's why we were seeing the regression slope
    // flip sign on near-flat charts. Centered form has full precision.
    //
    // σ = stddev of residuals (close - predicted) across ALL n bars.
    // Bands at ±1σ/±2σ/±3σ are parallel to the regression line.
    const channelData: {
      mid: LineData[]; up1: LineData[]; up2: LineData[]; up3: LineData[];
      dn1: LineData[]; dn2: LineData[]; dn3: LineData[];
    } = { mid: [], up1: [], up2: [], up3: [], dn1: [], dn2: [], dn3: [] };

    // ⚠ REPAINT FIX (2026-08-11). The original code fitted ONE line across all n
    // loaded bars and drew it over every bar, so each new bar moved every
    // historical band value. Measured by `tospane` (research archive), replaying
    // these exact presets bar by bar:
    //
    //   preset     ±2σ drift (median)  = % of band width   P(really at 2σ | shown at 2σ)
    //   5D·5m      31.85 pts           37%                 47.2%
    //   10D·30m    49.90 pts           38%                 49.5%
    //   6mo·1D     152.4 pts           35%                 51.1%
    //
    // The drawn slope disagreed in SIGN with the live one 21–28% of the time, the
    // live σ ran 0.68–0.74× the finished σ, and only 10–16% of the ±2σ touches that
    // actually happened stayed visible. Scoring signals off the finished line gave
    // +19..+85 pts/signal and t≥3 in 43 of 46 cells — 0 of 46 scored honestly.
    // Matched random walks reproduced ALL of it: the repaint was the estimator, not
    // the market. Server-side twin: engine/technical.stddev_channel_rolling().
    //
    // Now each bar is fitted on its OWN trailing window, so the invariant holds:
    //     band[i] depends only on closes[0..i]
    // Successive points are therefore NOT collinear — this is a curve, not a line.
    // Set SD_CHANNEL_FULL_RANGE = true to restore the old (repainting) look.
    const SD_CHANNEL_FULL_RANGE = false;
    const SD_CHANNEL_LOOKBACK = 0;  // 0 = expand to all prior bars; >0 = fixed trailing window

    if (closes.length >= 2) {
      const n = closes.length;
      const fitAt = (endIdx: number): { y: number; sd: number } => {
        // Fit closes[lo..endIdx] and evaluate at endIdx. Centered OLS for the same
        // numerical reason as before; population-N residual σ to match TOS.
        const lo = SD_CHANNEL_FULL_RANGE
          ? 0
          : SD_CHANNEL_LOOKBACK > 0
            ? Math.max(0, endIdx - SD_CHANNEL_LOOKBACK + 1)
            : 0;
        const hi = SD_CHANNEL_FULL_RANGE ? n - 1 : endIdx;
        const w = hi - lo + 1;
        if (w < 2) return { y: closes[endIdx], sd: 0 };
        const xbar = (w - 1) / 2;
        let ybar = 0;
        for (let j = 0; j < w; j++) ybar += closes[lo + j];
        ybar /= w;
        let sxx = 0, sxy = 0;
        for (let j = 0; j < w; j++) {
          const dx = j - xbar;
          const dy = closes[lo + j] - ybar;
          sxx += dx * dx;
          sxy += dx * dy;
        }
        const m = sxx !== 0 ? sxy / sxx : 0;
        const intercept = ybar - m * xbar;
        let ssq = 0;
        for (let j = 0; j < w; j++) {
          const e = closes[lo + j] - (m * j + intercept);
          ssq += e * e;
        }
        return { y: m * (endIdx - lo) + intercept, sd: Math.sqrt(ssq / w) };
      };

      for (let i = 0; i < n; i++) {
        const t = times[i];
        const { y, sd: sd_ } = fitAt(i);
        channelData.mid.push({ time: t, value: y });
        channelData.up1.push({ time: t, value: y + sd_ });
        channelData.up2.push({ time: t, value: y + 2 * sd_ });
        channelData.up3.push({ time: t, value: y + 3 * sd_ });
        channelData.dn1.push({ time: t, value: y - sd_ });
        channelData.dn2.push({ time: t, value: y - 2 * sd_ });
        channelData.dn3.push({ time: t, value: y - 3 * sd_ });
      }
    }
    const sd = sdRefs.current;
    sd.mid?.setData(channelData.mid);
    sd.up1?.setData(channelData.up1);
    sd.up2?.setData(channelData.up2);
    sd.up3?.setData(channelData.up3);
    sd.dn1?.setData(channelData.dn1);
    sd.dn2?.setData(channelData.dn2);
    sd.dn3?.setData(channelData.dn3);

    // Fit only ONCE per symbol/timeframe load
    if (!hasFittedOnce && chartRef.current) {
      chartRef.current.timeScale().fitContent();
      setHasFittedOnce(true);
    }
  }, [bars, hasFittedOnce]);

  // Live tick → extend the last bar's high/low and shift close to tick price.
  // No new candle is created; just modify the last in place. Skipped in
  // tick-candle mode — those candles are ES prints, not SPX index levels.
  useEffect(() => {
    if (!seriesRef.current || !lastTick || bars.length === 0) return;
    if (symbol !== "SPX" || isTickTf) return;
    const last = bars[bars.length - 1];
    const tickPrice = lastTick.spx_price;
    const candle: CandlestickData = {
      time: Math.floor(last.timestamp_ms / 1000) as UTCTimestamp,
      open: last.open,
      high: Math.max(last.high, tickPrice),
      low: Math.min(last.low, tickPrice),
      close: tickPrice,
    };
    try { seriesRef.current.update(candle); } catch {}
  }, [lastTick, symbol, bars, isTickTf]);

  // [REMOVED 2026-08-25] The wall-overlay effect. It drew one solid price line
  // per aggregated GEX/DEX strike plus a dashed NTM max-pain line, SPX-only,
  // refreshed on every IterEvent. The QuantData API and data subscription were
  // CANCELLED, so the inputs are gone; a chart that keeps drawing the last
  // snapshot's walls is worse than one that draws none. The `priceLinesRef`
  // that held the handles went with it — this effect was its only writer.
  // The Day H/L lines below are unrelated (computed from loaded bars).

  // Day H/L
  const dayHighLow = useMemo(() => {
    if (bars.length === 0) return null;
    let high = -Infinity, low = Infinity;
    for (const b of bars) {
      if (b.high > high) high = b.high;
      if (b.low < low) low = b.low;
    }
    return { high, low };
  }, [bars]);

  // Hidden while Levels is on: these are the high / low of the LOADED WINDOW (not the day) and
  // would read like — and clash with — the overlay's PDH / PDL.
  useEffect(() => {
    if (!seriesRef.current) return;
    const series = seriesRef.current;
    for (const line of ambientLinesRef.current) series.removePriceLine(line);
    ambientLinesRef.current = [];
    if (dayHighLow && !showLevels) {
      ambientLinesRef.current.push(series.createPriceLine({
        price: dayHighLow.high, color: LEVEL_COLORS.DAY_HIGH, lineWidth: 1, lineStyle: LineStyle.Dotted,
        axisLabelVisible: true, title: `H ${dayHighLow.high.toFixed(2)}`,
      }));
      ambientLinesRef.current.push(series.createPriceLine({
        price: dayHighLow.low, color: LEVEL_COLORS.DAY_LOW, lineWidth: 1, lineStyle: LineStyle.Dotted,
        axisLabelVisible: true, title: `L ${dayHighLow.low.toFixed(2)}`,
      }));
    }
  }, [dayHighLow, showLevels]);

  // Dale preset — one click sets the chart timeframe + lookback (the
  // chart-following StdDev channel fits that window automatically) AND points
  // the independent overlay at the same bar_size + lookback. To see two
  // timeframes at once (e.g. a 5-min chart with a 6mo-daily channel), set the
  // overlay's own dropdowns instead of clicking a preset (which moves the chart).
  const applyPreset = (duration: string, barSize: string) => {
    setTimeframe(barSize);
    setLookbackOverride(duration);
    setOverlayBarSize(barSize);
    setOverlayLookback(duration);
  };

  return (
    <div className="chart-pane">
      <div className="chart-toolbar">
        <div className="chart-toolbar-group">
          {TIMEFRAMES.map((tf) => (
            <button key={tf.value} className={`chart-toolbar-btn ${timeframe === tf.value ? "active" : ""}`}
              onClick={() => setTimeframe(tf.value)}>{tf.label}</button>
          ))}
          {TICK_TIMEFRAMES.map((tf) => (
            <button key={tf.value} className={`chart-toolbar-btn ${timeframe === tf.value ? "active" : ""}`}
              onClick={() => setTimeframe(tf.value)}
              title={`${tf.n}-tick candles from the ES futures trade stream (SPX has no tape). Requires the live ES tick feed.`}>
              {tf.label}
            </button>
          ))}
        </div>
        <div className="chart-toolbar-group">
          <select
            value={historicalDate}
            onChange={(e) => setHistoricalDate(e.target.value)}
            className="chart-date-select"
            title={isTickTf ? "Tick candles are live-only (in-memory ES trade stream)." : "Pick a past day to replay (from persisted bars). Empty = live."}
            disabled={isTickTf}
          >
            <option value="">Live ({symbol})</option>
            {availableDates.map((d) => (
              <option key={d} value={d}>{d}</option>
            ))}
          </select>
          <select
            value={lookbackOverride}
            onChange={(e) => setLookbackOverride(e.target.value)}
            className="chart-date-select"
            title={`How far back to fetch. Default for ${timeframe}: ${DEFAULT_LOOKBACK_BY_TIMEFRAME[timeframe]}. Larger windows may hit IBKR per-bar-size limits.`}
            disabled={!!historicalDate || isTickTf}
          >
            <option value="">auto ({DEFAULT_LOOKBACK_BY_TIMEFRAME[timeframe] ?? "1 D"})</option>
            {LOOKBACK_OPTIONS.map((o) => (
              <option key={o.value} value={o.value}>{o.label}</option>
            ))}
          </select>
        </div>
        <div className="chart-toolbar-group">
          <button className={`chart-toolbar-btn ${showEMA ? "active" : ""}`}
                  onClick={() => setShowEMA(s => !s)} title="EMA 8 (orange) + 21 (cyan)">
            EMA
          </button>
          <button className={`chart-toolbar-btn ${showSD ? "active" : ""}`}
                  onClick={() => setShowSD(s => !s)} title="StandardDevChannel — TOS-style linear regression across the full chart, ±1σ / 2σ / 3σ. Recomputes on timeframe / lookback change.">
            STD
          </button>
          <button className={`chart-toolbar-btn ${showTrendlines ? "active" : ""}`}
                  onClick={() => setShowTrendlines(s => !s)}
                  disabled={isTickTf || !!historicalDate}
                  title="Auto trendlines — support (green) / resistance (red) rays through the last two pivot lows/highs, projected to the newest bar. Detected sidecar-side; live time-bars only.">
            Trendlines
          </button>
          <span className="chart-levels-wrap" ref={levelsMenuRef}>
            <button className={`chart-toolbar-btn ${showLevels ? "active" : ""}`}
                    onClick={() => setShowLevels(s => !s)}
                    disabled={isTickTf || !!historicalDate}
                    title="Structure levels — multi-timeframe swings, zones, midpoints, fibs, pivots, prior day/week, round numbers and midpoint-entry setups (study mtflevels). Decision support only; live time-bars only.">
              Levels
            </button>
            <button className={`chart-toolbar-btn ${levelsMenuOpen ? "active" : ""}`}
                    onClick={() => setLevelsMenuOpen(o => !o)}
                    disabled={isTickTf || !!historicalDate}
                    aria-expanded={levelsMenuOpen}
                    title="Structure levels settings — timeframes and families">
              ▾
            </button>
            {levelsMenuOpen && (
              <div className="chart-levels-popover" role="dialog" aria-label="Structure levels settings">
                <div className="chart-levels-hdr">Timeframes</div>
                <div className="chart-levels-row">
                  {SL_TAUS.map((t) => (
                    <label key={t} style={{ color: TAU_COLORS[t] }}>
                      <input type="checkbox" checked={levelsSettings.taus[t]}
                             onChange={(e) => {
                               const on = e.target.checked;
                               setLevelsSettings(s => ({ ...s, taus: { ...s.taus, [t]: on } }));
                             }} />
                      {t}
                    </label>
                  ))}
                </div>
                <div className="chart-levels-hdr">Families</div>
                <div className="chart-levels-row">
                  {SL_FAMILIES.map((f) => (
                    <label key={f.key}>
                      <input type="checkbox" checked={levelsSettings.families[f.key]}
                             onChange={(e) => {
                               const on = e.target.checked;
                               setLevelsSettings(s => ({ ...s, families: { ...s.families, [f.key]: on } }));
                             }} />
                      {f.label}
                    </label>
                  ))}
                </div>
                <div className="chart-levels-note">
                  Decision support only — study mtflevels (QQQ 2022–26): no mechanical edge.
                </div>
                {showLevels && levelsNote && <div className="chart-levels-status">{levelsNote}</div>}
              </div>
            )}
          </span>
        </div>
        <div className="chart-toolbar-group">
          {/* Independent StdDev-channel OVERLAY — a SECOND ±1/2/3σ channel at
              its own bar size + lookback, decoupled from the chart timeframe.
              The chart-following StdDev channel is the "STD" button above. */}
          <button className={`chart-toolbar-btn ${showOverlay ? "active" : ""}`}
                  onClick={() => setShowOverlay(s => !s)}
                  disabled={isTickTf || !!historicalDate}
                  title="StdDev overlay — a SECOND ±1σ/2σ/3σ channel at its OWN bar size + lookback, independent of the chart timeframe (e.g. sit on a 5-min chart, overlay the 6-month daily channel). Pink dashed.">
            Overlay
          </button>
          {showOverlay && (
            <>
              <select value={overlayBarSize} onChange={(e) => setOverlayBarSize(e.target.value)}
                      className="chart-date-select" title="Overlay bar size (independent of the chart).">
                {REG_BAR_SIZES.map((o) => (
                  <option key={o.value} value={o.value}>bars: {o.label}</option>
                ))}
              </select>
              <select value={overlayLookback} onChange={(e) => setOverlayLookback(e.target.value)}
                      className="chart-date-select" title="Overlay lookback / fit window (independent of the chart).">
                {REG_LOOKBACKS.map((o) => (
                  <option key={o.value} value={o.value}>back: {o.label}</option>
                ))}
              </select>
            </>
          )}
          {REG_PRESETS.map((p) => (
            <button key={p.label} className="chart-toolbar-btn"
                    onClick={() => applyPreset(p.duration, p.barSize)}
                    title={`Dale preset — set the chart timeframe + lookback (the STD channel follows) and point the overlay at ${p.label}.`}>
              {p.label}
            </button>
          ))}
        </div>
        <div className="chart-toolbar-info muted small">
          {bars.length} {isTickTf ? "candle" : "bar"}{bars.length === 1 ? "" : "s"}
          {isTickTf && (
            <span style={{ marginLeft: 8, color: "var(--color-amber)" }}>
              {tickFeedNote ?? "ES ticks"}
            </span>
          )}
          {historicalDate && !isTickTf && <span style={{ marginLeft: 8, color: "var(--color-amber)" }}>HISTORICAL</span>}
          {showLevels && !isTickTf && !historicalDate && levelsNote && (
            <span style={{ marginLeft: 8 }} title="Structure levels overlay (display only)">{levelsNote}</span>
          )}
        </div>
      </div>
      <div ref={containerRef} className="chart-container" />
      {bars.length === 0 && (
        <div className="chart-overlay-empty">
          {fetchError ? (
            <>
              <p style={{ color: "var(--color-red)" }}>Chart fetch error</p>
              <p className="chart-placeholder-hint">{fetchError}</p>
              <p className="chart-placeholder-hint">
                Check the sidecar console + restart the app
              </p>
            </>
          ) : isTickTf ? (
            <>
              <p>Waiting for ES tick candles ({timeframe})…</p>
              <p className="chart-placeholder-hint">
                {tickFeedNote ?? "Candles form from the live ES trade stream — a 1000T candle needs 1000 prints."}
              </p>
            </>
          ) : (
            <p>Loading {symbol} {timeframe}…</p>
          )}
        </div>
      )}
    </div>
  );
}

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
import type { IterEvent, WallRow } from "../lib/events";
import { formatExposureM } from "../lib/format";

const API_BASE = "http://127.0.0.1:8765";

// Display chart times in America/Chicago (CST/CDT) so the bar axis matches
// the LiveQuote panel + the trader's local clock. lightweight-charts stores
// epoch seconds internally — the formatter only changes the DISPLAYED string.
const _CST_TIME_FMT = new Intl.DateTimeFormat("en-US", {
  timeZone: "America/Chicago",
  hour: "2-digit", minute: "2-digit", hour12: false,
});
const _CST_DATETIME_FMT = new Intl.DateTimeFormat("en-US", {
  timeZone: "America/Chicago",
  month: "short", day: "2-digit",
  hour: "2-digit", minute: "2-digit", second: "2-digit", hour12: false,
});

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
    tickMarkFormatter: (time: number) => _CST_TIME_FMT.format(new Date(time * 1000)),
  },
  rightPriceScale: { borderColor: "#2a323e" },
  localization: {
    timeFormatter: (time: number) => _CST_DATETIME_FMT.format(new Date(time * 1000)) + " CT",
  },
};

const CANDLE_OPTIONS = {
  upColor: "#3fb950", downColor: "#f85149",
  borderUpColor: "#3fb950", borderDownColor: "#f85149",
  wickUpColor: "#3fb950", wickDownColor: "#f85149",
};

const WALL_COLORS = {
  GEX_CALL: "#58a6ff", GEX_PUT: "#a371f7",
  DEX_CALL: "#3fb950", DEX_PUT: "#f85149",
  MAX_PAIN: "#d29922", DAY_HIGH: "#3fb950", DAY_LOW: "#f85149",
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

// Smart default lookback per timeframe — picks a window that:
//   (a) fits within IBKR's per-request max for that bar size, and
//   (b) gives a useful contextual view (e.g. 5m bars → 1 week of context)
// User can override via the Lookback dropdown.
const DEFAULT_LOOKBACK_BY_TIMEFRAME: Record<string, string> = {
  "1 min":  "1 D",
  "2 mins": "2 D",
  "5 mins": "1 W",
  "15 mins": "2 W",
  "30 mins": "1 M",
  "1 hour": "3 M",
  "4 hours": "1 Y",
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

interface AggregatedWall {
  strike: number; sources: string[]; dominantColor: string;
}

function aggregateWalls(iter: IterEvent | null): { walls: AggregatedWall[]; ntmMaxPain: number | null } {
  if (!iter) return { walls: [], ntmMaxPain: null };
  const byStrike = new Map<number, AggregatedWall>();
  const addWall = (w: WallRow, kind: "GEX" | "DEX") => {
    const existing = byStrike.get(w.strike);
    const label = `${kind} ${formatExposureM(w.net_m)}`;
    const color = kind === "GEX"
      ? (w.side === "CALL" ? WALL_COLORS.GEX_CALL : WALL_COLORS.GEX_PUT)
      : (w.side === "CALL" ? WALL_COLORS.DEX_CALL : WALL_COLORS.DEX_PUT);
    if (existing) {
      existing.sources.push(label);
      if (Math.abs(w.net_m) > 0) existing.dominantColor = color;
    } else {
      byStrike.set(w.strike, { strike: w.strike, sources: [label], dominantColor: color });
    }
  };
  for (const w of iter.snapshot.top_gex.slice(0, 5)) addWall(w, "GEX");
  for (const w of iter.snapshot.top_dex.slice(0, 5)) addWall(w, "DEX");
  return { walls: Array.from(byStrike.values()), ntmMaxPain: iter.snapshot.ntm_max_pain };
}

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
  const priceLinesRef = useRef<IPriceLine[]>([]);
  const ambientLinesRef = useRef<IPriceLine[]>([]);
  const renderedBarCountRef = useRef(0);

  const { lastIter, lastTick, activeSymbol } = useSidecar();
  const symbol = activeSymbol ?? "SPX";  // dropdown in TopBar is single source of truth
  const [timeframe, setTimeframe] = useState<string>("2 mins");
  const [bars, setBars] = useState<FetchedBar[]>([]);
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

    chartRef.current = chart;
    seriesRef.current = series;
    ema8SeriesRef.current = ema8;
    ema21SeriesRef.current = ema21;
    sdRefs.current = { mid, up1, up2, up3, dn1, dn2, dn3 };

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
      priceLinesRef.current = [];
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
    // Reset render state on symbol/timeframe/date change so we re-setData
    renderedBarCountRef.current = 0;
    setHasFittedOnce(false);
    void fetchBars();
    // Historical mode: no polling (the day is closed). Live mode: poll every 3s.
    if (historicalDate) return () => { cancelled = true; };
    const id = window.setInterval(fetchBars, POLL_INTERVAL_MS);
    return () => {
      cancelled = true;
      window.clearInterval(id);
    };
  }, [symbol, timeframe, historicalDate, effectiveLookback]);

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

    if (closes.length >= 2) {
      const n = closes.length;
      const xbar = (n - 1) / 2;  // mean of 0..n-1
      let ybar = 0;
      for (let i = 0; i < n; i++) ybar += closes[i];
      ybar /= n;
      let sxx = 0, sxy = 0;
      for (let i = 0; i < n; i++) {
        const dx = i - xbar;
        const dy = closes[i] - ybar;
        sxx += dx * dx;
        sxy += dx * dy;
      }
      const m = sxx !== 0 ? sxy / sxx : 0;
      const intercept = ybar - m * xbar;
      // Residual stddev — population N (matches TOS convention)
      let ssq = 0;
      for (let i = 0; i < n; i++) {
        const pred = m * i + intercept;
        const e = closes[i] - pred;
        ssq += e * e;
      }
      const sd_ = Math.sqrt(ssq / n);
      // Build the 7 parallel lines across every bar
      for (let i = 0; i < n; i++) {
        const t = times[i];
        const y = m * i + intercept;
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
  // No new candle is created; just modify the last in place.
  useEffect(() => {
    if (!seriesRef.current || !lastTick || bars.length === 0) return;
    if (symbol !== "SPX") return;
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
  }, [lastTick, symbol, bars]);

  // Wall overlays
  useEffect(() => {
    if (!seriesRef.current || symbol !== "SPX") return;
    const series = seriesRef.current;
    for (const line of priceLinesRef.current) series.removePriceLine(line);
    priceLinesRef.current = [];
    const { walls, ntmMaxPain } = aggregateWalls(lastIter);
    for (const w of walls) {
      priceLinesRef.current.push(series.createPriceLine({
        price: w.strike, color: w.dominantColor, lineWidth: 1, lineStyle: LineStyle.Solid,
        axisLabelVisible: true, title: w.sources.join(" | "),
      }));
    }
    if (ntmMaxPain && ntmMaxPain > 0) {
      priceLinesRef.current.push(series.createPriceLine({
        price: ntmMaxPain, color: WALL_COLORS.MAX_PAIN, lineWidth: 2, lineStyle: LineStyle.Dashed,
        axisLabelVisible: true, title: `NTM MP ${ntmMaxPain.toFixed(0)}`,
      }));
    }
  }, [lastIter, symbol]);

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

  useEffect(() => {
    if (!seriesRef.current) return;
    const series = seriesRef.current;
    for (const line of ambientLinesRef.current) series.removePriceLine(line);
    ambientLinesRef.current = [];
    if (dayHighLow) {
      ambientLinesRef.current.push(series.createPriceLine({
        price: dayHighLow.high, color: WALL_COLORS.DAY_HIGH, lineWidth: 1, lineStyle: LineStyle.Dotted,
        axisLabelVisible: true, title: `H ${dayHighLow.high.toFixed(2)}`,
      }));
      ambientLinesRef.current.push(series.createPriceLine({
        price: dayHighLow.low, color: WALL_COLORS.DAY_LOW, lineWidth: 1, lineStyle: LineStyle.Dotted,
        axisLabelVisible: true, title: `L ${dayHighLow.low.toFixed(2)}`,
      }));
    }
  }, [dayHighLow]);

  return (
    <div className="chart-pane">
      <div className="chart-toolbar">
        <div className="chart-toolbar-group">
          {TIMEFRAMES.map((tf) => (
            <button key={tf.value} className={`chart-toolbar-btn ${timeframe === tf.value ? "active" : ""}`}
              onClick={() => setTimeframe(tf.value)}>{tf.label}</button>
          ))}
        </div>
        <div className="chart-toolbar-group">
          <select
            value={historicalDate}
            onChange={(e) => setHistoricalDate(e.target.value)}
            className="chart-date-select"
            title="Pick a past day to replay (from persisted bars). Empty = live."
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
            disabled={!!historicalDate}
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
        </div>
        <div className="chart-toolbar-info muted small">
          {bars.length} bar{bars.length === 1 ? "" : "s"}
          {historicalDate && <span style={{ marginLeft: 8, color: "var(--color-amber)" }}>HISTORICAL</span>}
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
          ) : (
            <p>Loading {symbol} {timeframe}…</p>
          )}
        </div>
      )}
    </div>
  );
}

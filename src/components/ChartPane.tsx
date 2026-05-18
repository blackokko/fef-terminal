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

const DARK_THEME = {
  layout: {
    background: { type: ColorType.Solid, color: "#0e1116" },
    textColor: "#7d8590",
    fontFamily: 'ui-monospace, "SF Mono", Menlo, Consolas, monospace',
  },
  grid: { vertLines: { color: "#1c222b" }, horzLines: { color: "#1c222b" } },
  crosshair: { mode: CrosshairMode.Normal },
  timeScale: { borderColor: "#2a323e", timeVisible: true, secondsVisible: false },
  rightPriceScale: { borderColor: "#2a323e" },
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
          url = `${API_BASE}/bars?symbol=${encodeURIComponent(symbol)}&bar_size=${encodeURIComponent(timeframe)}`;
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
  }, [symbol, timeframe, historicalDate]);

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

  // Apply bars to the chart — full setData on count change, update() on the
  // last bar when count is same (in case its OHLC adjusted)
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
      // EMA 8/21 — recursive: EMA_t = α·close + (1-α)·EMA_{t-1}, α = 2/(N+1)
      const closes = bars.map(b => b.close);
      const times = bars.map(b => Math.floor(b.timestamp_ms / 1000) as UTCTimestamp);
      const ema = (period: number): LineData[] => {
        const α = 2 / (period + 1);
        const out: LineData[] = [];
        let prev: number | null = null;
        for (let i = 0; i < closes.length; i++) {
          if (prev == null) {
            // seed with SMA of first `period` closes
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

      // Standard deviation channels — 20-period SMA ± 1σ / 2σ / 3σ
      const period = 20;
      const mid: LineData[] = [];
      const up1: LineData[] = []; const up2: LineData[] = []; const up3: LineData[] = [];
      const dn1: LineData[] = []; const dn2: LineData[] = []; const dn3: LineData[] = [];
      for (let i = period - 1; i < closes.length; i++) {
        const window = closes.slice(i - period + 1, i + 1);
        const sma = window.reduce((s, x) => s + x, 0) / period;
        const variance = window.reduce((s, x) => s + (x - sma) ** 2, 0) / period;
        const sd = Math.sqrt(variance);
        const t = times[i];
        mid.push({ time: t, value: sma });
        up1.push({ time: t, value: sma + sd });
        up2.push({ time: t, value: sma + 2 * sd });
        up3.push({ time: t, value: sma + 3 * sd });
        dn1.push({ time: t, value: sma - sd });
        dn2.push({ time: t, value: sma - 2 * sd });
        dn3.push({ time: t, value: sma - 3 * sd });
      }
      const sd = sdRefs.current;
      sd.mid?.setData(mid);
      sd.up1?.setData(up1); sd.up2?.setData(up2); sd.up3?.setData(up3);
      sd.dn1?.setData(dn1); sd.dn2?.setData(dn2); sd.dn3?.setData(dn3);
      renderedBarCountRef.current = deduped.length;
      // Fit only ONCE per symbol/timeframe load
      if (!hasFittedOnce && chartRef.current) {
        chartRef.current.timeScale().fitContent();
        setHasFittedOnce(true);
      }
    } else {
      // Same count — just update the last bar in case its OHLC moved
      const last = deduped[deduped.length - 1];
      try { seriesRef.current.update(last); } catch {}
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
        </div>
        <div className="chart-toolbar-group">
          <button className={`chart-toolbar-btn ${showEMA ? "active" : ""}`}
                  onClick={() => setShowEMA(s => !s)} title="EMA 8 (orange) + 21 (cyan)">
            EMA
          </button>
          <button className={`chart-toolbar-btn ${showSD ? "active" : ""}`}
                  onClick={() => setShowSD(s => !s)} title="Std Dev channels 20-period (1σ gray · 2σ amber · 3σ red)">
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

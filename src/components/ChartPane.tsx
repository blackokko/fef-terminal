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
const SYMBOLS = ["SPX", "ES"];

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
    const sign = w.net_m > 0 ? "+" : "";
    const label = `${kind} ${sign}${w.net_m.toFixed(0)}M`;
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
  const vwapSeriesRef = useRef<ISeriesApi<"Line"> | null>(null);
  const priceLinesRef = useRef<IPriceLine[]>([]);
  const ambientLinesRef = useRef<IPriceLine[]>([]);
  const renderedBarCountRef = useRef(0);

  const { lastIter, lastTick } = useSidecar();
  const [symbol, setSymbol] = useState<string>("SPX");
  const [timeframe, setTimeframe] = useState<string>("2 mins");
  const [bars, setBars] = useState<FetchedBar[]>([]);
  const [hasFittedOnce, setHasFittedOnce] = useState(false);

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
    const vwap = chart.addSeries(LineSeries, {
      color: WALL_COLORS.VWAP, lineWidth: 1, lineStyle: LineStyle.Dotted,
      priceLineVisible: false, lastValueVisible: false,
    });
    chartRef.current = chart;
    seriesRef.current = series;
    vwapSeriesRef.current = vwap;

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
      vwapSeriesRef.current = null;
      priceLinesRef.current = [];
      ambientLinesRef.current = [];
      renderedBarCountRef.current = 0;
    };
  }, []);

  // Poll /bars every 3s. Refetches immediately when symbol/timeframe change.
  useEffect(() => {
    let cancelled = false;
    const fetchBars = async () => {
      try {
        const r = await fetch(
          `${API_BASE}/bars?symbol=${symbol}&bar_size=${encodeURIComponent(timeframe)}&duration=1 D`,
        );
        if (!r.ok || cancelled) return;
        const data = (await r.json()) as FetchedBar[];
        if (!cancelled) setBars(data);
      } catch {}
    };
    // Reset render state on symbol/timeframe change so we re-setData
    renderedBarCountRef.current = 0;
    setHasFittedOnce(false);
    void fetchBars();
    const id = window.setInterval(fetchBars, POLL_INTERVAL_MS);
    return () => {
      cancelled = true;
      window.clearInterval(id);
    };
  }, [symbol, timeframe]);

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
      // VWAP rebuild
      let cumPV = 0, cumV = 0;
      const vwapData: LineData[] = bars.map((b) => {
        const typical = (b.high + b.low + b.close) / 3;
        const vol = b.volume || 1;
        cumPV += typical * vol;
        cumV += vol;
        return { time: Math.floor(b.timestamp_ms / 1000) as UTCTimestamp, value: cumPV / cumV };
      });
      if (vwapSeriesRef.current) vwapSeriesRef.current.setData(vwapData);
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
          {SYMBOLS.map((s) => (
            <button key={s} className={`chart-toolbar-btn ${symbol === s ? "active" : ""}`}
              onClick={() => setSymbol(s)}>{s}</button>
          ))}
        </div>
        <div className="chart-toolbar-group">
          {TIMEFRAMES.map((tf) => (
            <button key={tf.value} className={`chart-toolbar-btn ${timeframe === tf.value ? "active" : ""}`}
              onClick={() => setTimeframe(tf.value)}>{tf.label}</button>
          ))}
        </div>
        <div className="chart-toolbar-info muted small">
          {bars.length} bar{bars.length === 1 ? "" : "s"}
        </div>
      </div>
      <div ref={containerRef} className="chart-container" />
      {bars.length === 0 && (
        <div className="chart-overlay-empty">
          <p>Loading {symbol} {timeframe}…</p>
        </div>
      )}
    </div>
  );
}

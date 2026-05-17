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
  MAX_PAIN: "#d29922", CURRENT: "#e6edf3",
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
const SYMBOLS = ["SPX", "ES"];

const MIN_Y_RANGE_PTS = 50; // always show at least 50pt vertical range

interface AggregatedWall {
  strike: number;
  sources: string[];
  dominantColor: string;
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
  timestamp_ms: number;
  open: number;
  high: number;
  low: number;
  close: number;
  volume: number;
}

export function ChartPane() {
  const containerRef = useRef<HTMLDivElement | null>(null);
  const chartRef = useRef<IChartApi | null>(null);
  const seriesRef = useRef<ISeriesApi<"Candlestick"> | null>(null);
  const vwapSeriesRef = useRef<ISeriesApi<"Line"> | null>(null);
  const priceLinesRef = useRef<IPriceLine[]>([]);
  const ambientLinesRef = useRef<IPriceLine[]>([]);
  const currentBarRef = useRef<CandlestickData | null>(null);
  const cumVolRef = useRef(0);
  const cumPVRef = useRef(0);

  const { recentTicks, lastIter, lastTick } = useSidecar();
  const [symbol, setSymbol] = useState<string>("SPX");
  const [timeframe, setTimeframe] = useState<string>("2 mins");
  const [bars, setBars] = useState<FetchedBar[]>([]);

  // Create chart once
  useEffect(() => {
    if (!containerRef.current) return;
    const chart = createChart(containerRef.current, { ...DARK_THEME, autoSize: true });
    const series = chart.addSeries(CandlestickSeries, {
      ...CANDLE_OPTIONS,
      // Enforce minimum visible y-range so the chart doesn't squish tightly
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
      color: WALL_COLORS.VWAP,
      lineWidth: 1,
      lineStyle: LineStyle.Dotted,
      priceLineVisible: false,
      lastValueVisible: false,
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
      currentBarRef.current = null;
    };
  }, []);

  // Fetch bars from backend on symbol/timeframe change
  useEffect(() => {
    void (async () => {
      try {
        const r = await fetch(`${API_BASE}/bars?symbol=${symbol}&bar_size=${encodeURIComponent(timeframe)}&duration=1 D`);
        if (r.ok) {
          const data = (await r.json()) as FetchedBar[];
          setBars(data);
          currentBarRef.current = null;
          cumVolRef.current = 0;
          cumPVRef.current = 0;
        }
      } catch (e) {
        console.error("Failed to fetch bars:", e);
      }
    })();
  }, [symbol, timeframe]);

  // Initial setData when bars first load (or symbol/timeframe changes)
  useEffect(() => {
    if (!seriesRef.current) return;
    if (bars.length === 0) {
      seriesRef.current.setData([]);
      if (vwapSeriesRef.current) vwapSeriesRef.current.setData([]);
      return;
    }
    const candleData: CandlestickData[] = bars.map((b) => ({
      time: Math.floor(b.timestamp_ms / 1000) as UTCTimestamp,
      open: b.open, high: b.high, low: b.low, close: b.close,
    }));
    // Dedupe by time (keep last)
    const map = new Map<number, CandlestickData>();
    for (const c of candleData) map.set(c.time as number, c);
    const deduped = Array.from(map.values()).sort((a, b) => (a.time as number) - (b.time as number));
    seriesRef.current.setData(deduped);

    // VWAP from bars
    cumVolRef.current = 0;
    cumPVRef.current = 0;
    const vwapData: LineData[] = bars.map((b) => {
      const typical = (b.high + b.low + b.close) / 3;
      const vol = b.volume || 1;
      cumPVRef.current += typical * vol;
      cumVolRef.current += vol;
      return { time: Math.floor(b.timestamp_ms / 1000) as UTCTimestamp, value: cumPVRef.current / cumVolRef.current };
    });
    if (vwapSeriesRef.current) vwapSeriesRef.current.setData(vwapData);

    currentBarRef.current = deduped[deduped.length - 1] ?? null;

    // Fit ONCE per data load — NOT on every tick
    if (chartRef.current) chartRef.current.timeScale().fitContent();
  }, [bars]);

  // Append NEW bars arriving via WS (TickEvents with bar payload)
  const lastBarCountRef = useRef(0);
  useEffect(() => {
    if (!seriesRef.current) return;
    // Only consider ticks with bars for THIS symbol — for v1 we assume WS
    // bars are SPX; if user is viewing ES we skip WS bar updates.
    if (symbol !== "SPX") return;

    const barTicks = recentTicks.filter((t) => t.bar);
    if (barTicks.length <= lastBarCountRef.current) return;

    const newOnes = barTicks.slice(lastBarCountRef.current);
    const lastTime = currentBarRef.current ? (currentBarRef.current.time as number) : 0;
    for (const t of newOnes) {
      const time = Math.floor(t.timestamp_ms / 1000) as UTCTimestamp;
      // Guard against time regression — lightweight-charts errors and the
      // chart goes black if we ever send a bar with an earlier timestamp
      if ((time as number) < lastTime) continue;
      const candle: CandlestickData = {
        time,
        open: t.bar!.open, high: t.bar!.high, low: t.bar!.low, close: t.bar!.close,
      };
      try {
        seriesRef.current.update(candle);
        currentBarRef.current = candle;

        // Update VWAP
        const typical = (t.bar!.high + t.bar!.low + t.bar!.close) / 3;
        const vol = t.bar!.volume || 1;
        cumPVRef.current += typical * vol;
        cumVolRef.current += vol;
        vwapSeriesRef.current?.update({
          time: candle.time,
          value: cumPVRef.current / cumVolRef.current,
        });
      } catch (err) {
        console.warn("chart update skipped:", err);
      }
    }
    lastBarCountRef.current = barTicks.length;
  }, [recentTicks, symbol]);

  // Live-tick update of the current candle (close/high/low) — no setData, no fitContent
  useEffect(() => {
    if (!seriesRef.current || !currentBarRef.current || !lastTick) return;
    if (symbol !== "SPX") return;  // ES doesn't get SPX ticks
    const tickPrice = lastTick.spx_price;
    const cb = currentBarRef.current;
    const updated: CandlestickData = {
      time: cb.time,
      open: cb.open,
      high: Math.max(cb.high, tickPrice),
      low: Math.min(cb.low, tickPrice),
      close: tickPrice,
    };
    try {
      seriesRef.current.update(updated);
      currentBarRef.current = updated;
    } catch (err) {
      console.warn("live tick update skipped:", err);
    }
  }, [lastTick, symbol]);

  // Wall overlays — redrawn on each iter (low frequency, OK to re-create lines)
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

  // Ambient: day H/L only (no current-price line — the live candle close already shows it)
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
            <button
              key={s}
              className={`chart-toolbar-btn ${symbol === s ? "active" : ""}`}
              onClick={() => setSymbol(s)}
            >{s}</button>
          ))}
        </div>
        <div className="chart-toolbar-group">
          {TIMEFRAMES.map((tf) => (
            <button
              key={tf.value}
              className={`chart-toolbar-btn ${timeframe === tf.value ? "active" : ""}`}
              onClick={() => setTimeframe(tf.value)}
            >{tf.label}</button>
          ))}
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

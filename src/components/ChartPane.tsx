import { useEffect, useMemo, useRef } from "react";
import {
  CandlestickSeries,
  ColorType,
  CrosshairMode,
  LineSeries,
  LineStyle,
  createChart,
  type CandlestickData,
  type IChartApi,
  type IPriceLine,
  type ISeriesApi,
  type LineData,
  type UTCTimestamp,
} from "lightweight-charts";
import { useSidecar } from "../context/WebSocketContext";
import type { Bar, IterEvent, WallRow } from "../lib/events";

const DARK_THEME = {
  layout: {
    background: { type: ColorType.Solid, color: "#0e1116" },
    textColor: "#7d8590",
    fontFamily:
      'ui-monospace, "SF Mono", Menlo, Consolas, "Liberation Mono", monospace',
  },
  grid: {
    vertLines: { color: "#1c222b" },
    horzLines: { color: "#1c222b" },
  },
  crosshair: { mode: CrosshairMode.Normal },
  timeScale: {
    borderColor: "#2a323e",
    timeVisible: true,
    secondsVisible: false,
  },
  rightPriceScale: { borderColor: "#2a323e" },
};

const CANDLE_OPTIONS = {
  upColor: "#3fb950",
  downColor: "#f85149",
  borderUpColor: "#3fb950",
  borderDownColor: "#f85149",
  wickUpColor: "#3fb950",
  wickDownColor: "#f85149",
};

const WALL_COLORS = {
  GEX_CALL: "#58a6ff",
  GEX_PUT: "#a371f7",
  DEX_CALL: "#3fb950",
  DEX_PUT: "#f85149",
  MAX_PAIN: "#d29922",
  CURRENT: "#e6edf3",
  DAY_HIGH: "#3fb950",
  DAY_LOW: "#f85149",
  VWAP: "#58a6ff",
};

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

function dayHighLow(bars: Bar[]): { high: number; low: number } | null {
  if (bars.length === 0) return null;
  let high = -Infinity;
  let low = Infinity;
  for (const b of bars) {
    if (b.high > high) high = b.high;
    if (b.low < low) low = b.low;
  }
  return { high, low };
}

export function ChartPane() {
  const containerRef = useRef<HTMLDivElement | null>(null);
  const chartRef = useRef<IChartApi | null>(null);
  const seriesRef = useRef<ISeriesApi<"Candlestick"> | null>(null);
  const vwapSeriesRef = useRef<ISeriesApi<"Line"> | null>(null);
  const priceLinesRef = useRef<IPriceLine[]>([]);
  const ambientLinesRef = useRef<IPriceLine[]>([]);  // current price, day H/L
  const { recentTicks, lastIter, lastTick } = useSidecar();

  useEffect(() => {
    if (!containerRef.current) return;
    const chart = createChart(containerRef.current, { ...DARK_THEME, autoSize: true });
    const series = chart.addSeries(CandlestickSeries, CANDLE_OPTIONS);
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
    };
  }, []);

  const bars = useMemo<Bar[]>(() => {
    return recentTicks
      .filter((t) => t.bar)
      .map((t) => ({
        open: t.bar!.open,
        high: t.bar!.high,
        low: t.bar!.low,
        close: t.bar!.close,
        volume: t.bar!.volume,
        timestamp_ms: t.timestamp_ms,
      } as Bar & { timestamp_ms: number }));
  }, [recentTicks]);

  const candleData = useMemo<CandlestickData[]>(() => {
    const map = new Map<number, CandlestickData>();
    for (const t of recentTicks) {
      if (!t.bar) continue;
      const time = Math.floor(t.timestamp_ms / 1000) as UTCTimestamp;
      map.set(time as number, {
        time,
        open: t.bar.open,
        high: t.bar.high,
        low: t.bar.low,
        close: t.bar.close,
      });
    }
    return Array.from(map.values()).sort((a, b) => (a.time as number) - (b.time as number));
  }, [recentTicks]);

  // VWAP data with proper timestamps
  const vwapData = useMemo<LineData[]>(() => {
    if (candleData.length === 0) return [];
    let cumPV = 0;
    let cumV = 0;
    return candleData.map((c, idx) => {
      const typical = (c.high + c.low + c.close) / 3;
      const vol = bars[idx]?.volume || 1;
      cumPV += typical * vol;
      cumV += vol;
      return { time: c.time, value: cumV > 0 ? cumPV / cumV : typical };
    });
  }, [candleData, bars]);

  useEffect(() => {
    if (!seriesRef.current) return;
    seriesRef.current.setData(candleData);
    if (chartRef.current && candleData.length > 0) {
      chartRef.current.timeScale().fitContent();
    }
  }, [candleData]);

  useEffect(() => {
    if (!vwapSeriesRef.current) return;
    vwapSeriesRef.current.setData(vwapData);
  }, [vwapData]);

  // Wall overlays — redrawn each iter
  useEffect(() => {
    if (!seriesRef.current) return;
    const series = seriesRef.current;
    for (const line of priceLinesRef.current) series.removePriceLine(line);
    priceLinesRef.current = [];
    const { walls, ntmMaxPain } = aggregateWalls(lastIter);
    for (const w of walls) {
      priceLinesRef.current.push(series.createPriceLine({
        price: w.strike,
        color: w.dominantColor,
        lineWidth: 1,
        lineStyle: LineStyle.Solid,
        axisLabelVisible: true,
        title: w.sources.join(" | "),
      }));
    }
    if (ntmMaxPain && ntmMaxPain > 0) {
      priceLinesRef.current.push(series.createPriceLine({
        price: ntmMaxPain,
        color: WALL_COLORS.MAX_PAIN,
        lineWidth: 2,
        lineStyle: LineStyle.Dashed,
        axisLabelVisible: true,
        title: `NTM MP ${ntmMaxPain.toFixed(0)}`,
      }));
    }
  }, [lastIter]);

  // Ambient lines: current price + day H/L (separate ref so they don't get wiped by wall updates)
  useEffect(() => {
    if (!seriesRef.current) return;
    const series = seriesRef.current;
    for (const line of ambientLinesRef.current) series.removePriceLine(line);
    ambientLinesRef.current = [];

    const currentPrice = lastTick?.spx_price ?? lastIter?.spx_price;
    if (currentPrice != null) {
      ambientLinesRef.current.push(series.createPriceLine({
        price: currentPrice,
        color: WALL_COLORS.CURRENT,
        lineWidth: 1,
        lineStyle: LineStyle.Dotted,
        axisLabelVisible: true,
        title: `SPX ${currentPrice.toFixed(2)}`,
      }));
    }

    const hl = dayHighLow(bars);
    if (hl) {
      ambientLinesRef.current.push(series.createPriceLine({
        price: hl.high,
        color: WALL_COLORS.DAY_HIGH,
        lineWidth: 1,
        lineStyle: LineStyle.Dotted,
        axisLabelVisible: true,
        title: `H ${hl.high.toFixed(2)}`,
      }));
      ambientLinesRef.current.push(series.createPriceLine({
        price: hl.low,
        color: WALL_COLORS.DAY_LOW,
        lineWidth: 1,
        lineStyle: LineStyle.Dotted,
        axisLabelVisible: true,
        title: `L ${hl.low.toFixed(2)}`,
      }));
    }
  }, [lastTick?.spx_price, lastIter, bars]);

  return (
    <div className="chart-pane">
      <div ref={containerRef} className="chart-container" />
      {candleData.length === 0 && (
        <div className="chart-overlay-empty">
          <p>Waiting for bar stream…</p>
          <p className="chart-placeholder-hint">
            Make sure the sidecar is running and IBKR / mock provider is connected.
          </p>
        </div>
      )}
    </div>
  );
}

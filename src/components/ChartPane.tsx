import { useEffect, useMemo, useRef } from "react";
import {
  CandlestickSeries,
  ColorType,
  CrosshairMode,
  LineStyle,
  createChart,
  type CandlestickData,
  type IChartApi,
  type IPriceLine,
  type ISeriesApi,
  type UTCTimestamp,
} from "lightweight-charts";
import { useSidecar } from "../context/WebSocketContext";
import type { IterEvent, WallRow } from "../lib/events";

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
};

interface AggregatedWall {
  strike: number;
  sources: string[];  // ["GEX 32.7M", "DEX 1336M"] etc.
  dominantColor: string;
}

function aggregateWalls(iter: IterEvent | null): {
  walls: AggregatedWall[];
  ntmMaxPain: number | null;
} {
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
      // Keep the strongest-abs color preference
      if (Math.abs(w.net_m) > 0) existing.dominantColor = color;
    } else {
      byStrike.set(w.strike, {
        strike: w.strike,
        sources: [label],
        dominantColor: color,
      });
    }
  };

  for (const w of iter.snapshot.top_gex.slice(0, 5)) addWall(w, "GEX");
  for (const w of iter.snapshot.top_dex.slice(0, 5)) addWall(w, "DEX");

  return {
    walls: Array.from(byStrike.values()),
    ntmMaxPain: iter.snapshot.ntm_max_pain,
  };
}

export function ChartPane() {
  const containerRef = useRef<HTMLDivElement | null>(null);
  const chartRef = useRef<IChartApi | null>(null);
  const seriesRef = useRef<ISeriesApi<"Candlestick"> | null>(null);
  const priceLinesRef = useRef<IPriceLine[]>([]);
  const { recentBars, recentTicks, lastIter } = useSidecar();

  // Create chart once on mount
  useEffect(() => {
    if (!containerRef.current) return;

    const chart = createChart(containerRef.current, {
      ...DARK_THEME,
      autoSize: true,
    });
    const series = chart.addSeries(CandlestickSeries, CANDLE_OPTIONS);

    chartRef.current = chart;
    seriesRef.current = series;

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
      priceLinesRef.current = [];
    };
  }, []);

  // Map bars onto chart whenever recentBars/recentTicks changes
  const candleData = useMemo<CandlestickData[]>(() => {
    const fromTicks = recentTicks
      .filter((t) => t.bar)
      .map((t) => ({
        time: Math.floor(t.timestamp_ms / 1000) as UTCTimestamp,
        open: t.bar!.open,
        high: t.bar!.high,
        low: t.bar!.low,
        close: t.bar!.close,
      }));

    const map = new Map<number, CandlestickData>();
    for (const c of fromTicks) map.set(c.time as number, c);
    return Array.from(map.values()).sort(
      (a, b) => (a.time as number) - (b.time as number),
    );
  }, [recentBars, recentTicks]);

  useEffect(() => {
    if (!seriesRef.current) return;
    seriesRef.current.setData(candleData);
    if (chartRef.current && candleData.length > 0) {
      chartRef.current.timeScale().fitContent();
    }
  }, [candleData]);

  // Redraw wall + max-pain overlays on each new IterEvent
  useEffect(() => {
    if (!seriesRef.current) return;
    const series = seriesRef.current;

    // Clear previous lines
    for (const line of priceLinesRef.current) {
      series.removePriceLine(line);
    }
    priceLinesRef.current = [];

    const { walls, ntmMaxPain } = aggregateWalls(lastIter);

    for (const w of walls) {
      const line = series.createPriceLine({
        price: w.strike,
        color: w.dominantColor,
        lineWidth: 1,
        lineStyle: LineStyle.Solid,
        axisLabelVisible: true,
        title: w.sources.join(" | "),
      });
      priceLinesRef.current.push(line);
    }

    if (ntmMaxPain && ntmMaxPain > 0) {
      const line = series.createPriceLine({
        price: ntmMaxPain,
        color: WALL_COLORS.MAX_PAIN,
        lineWidth: 2,
        lineStyle: LineStyle.Dashed,
        axisLabelVisible: true,
        title: `NTM MP ${ntmMaxPain.toFixed(0)}`,
      });
      priceLinesRef.current.push(line);
    }
  }, [lastIter]);

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

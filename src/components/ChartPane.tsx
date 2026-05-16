import { useEffect, useMemo, useRef } from "react";
import {
  CandlestickSeries,
  ColorType,
  CrosshairMode,
  createChart,
  type CandlestickData,
  type IChartApi,
  type ISeriesApi,
  type UTCTimestamp,
} from "lightweight-charts";
import { useSidecar } from "../context/WebSocketContext";

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
  crosshair: {
    mode: CrosshairMode.Normal,
  },
  timeScale: {
    borderColor: "#2a323e",
    timeVisible: true,
    secondsVisible: false,
  },
  rightPriceScale: {
    borderColor: "#2a323e",
  },
};

const CANDLE_OPTIONS = {
  upColor: "#3fb950",
  downColor: "#f85149",
  borderUpColor: "#3fb950",
  borderDownColor: "#f85149",
  wickUpColor: "#3fb950",
  wickDownColor: "#f85149",
};

export function ChartPane() {
  const containerRef = useRef<HTMLDivElement | null>(null);
  const chartRef = useRef<IChartApi | null>(null);
  const seriesRef = useRef<ISeriesApi<"Candlestick"> | null>(null);
  const { recentBars, recentTicks } = useSidecar();

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
    };
  }, []);

  // Map bars onto chart whenever recentBars changes
  const candleData = useMemo<CandlestickData[]>(() => {
    if (recentBars.length === 0 && recentTicks.length === 0) return [];

    // Bars come from TickEvent.bar — each tick may or may not have a bar
    // Reconstruct from recentTicks since those preserve the timestamp
    const fromTicks = recentTicks
      .filter((t) => t.bar)
      .map((t) => ({
        time: Math.floor(t.timestamp_ms / 1000) as UTCTimestamp,
        open: t.bar!.open,
        high: t.bar!.high,
        low: t.bar!.low,
        close: t.bar!.close,
      }));

    // Dedupe by time (last write wins), sort ascending
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

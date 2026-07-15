import { useEffect, useMemo, useRef, useState } from "react";
import {
  CandlestickSeries,
  CrosshairMode,
  HistogramSeries,
  LineSeries,
  createChart,
  createSeriesMarkers,
} from "lightweight-charts";

const DISPLAY_TIME_ZONE = "Africa/Johannesburg";
const DISPLAY_TZ_LABEL = "SAST";

function toTime(value) {
  const raw = String(value ?? "").trim();
  if (!raw) return null;
  if (/^\d+$/.test(raw)) {
    const numeric = Number(raw);
    return Number.isFinite(numeric) ? (numeric > 1e12 ? Math.floor(numeric / 1000) : numeric) : null;
  }
  const normalized = raw.replace(" ", "T");
  const parsed = Math.floor(new Date(/(?:Z|[+-]\d{2}:?\d{2})$/i.test(normalized) ? normalized : normalized + "Z").getTime() / 1000);
  return Number.isFinite(parsed) ? parsed : null;
}

function normalizeCandles(rows) {
  const seen = new Set();
  return (Array.isArray(rows) ? rows : [])
    .map((row) => ({
      time: toTime(row.ts ?? row.time ?? row.timestamp),
      open: Number(row.open),
      high: Number(row.high),
      low: Number(row.low),
      close: Number(row.close),
      volume: Number(row.volume || row.tick_volume || 0),
    }))
    .filter((row) => {
      if (!Number.isFinite(row.time) || seen.has(row.time)) return false;
      if (![row.open, row.high, row.low, row.close].every(Number.isFinite)) return false;
      seen.add(row.time);
      return true;
    })
    .sort((a, b) => a.time - b.time);
}

function normalizeTrades(rows) {
  return (Array.isArray(rows) ? rows : []).map((row) => ({
    ...row,
    direction: String(row.direction || row.side || "").toUpperCase(),
    entry: Number(row.entry),
    exit_px: Number(row.exit_px),
    opened_at: toTime(row.opened_at || row.open_time || row.time),
    closed_at: toTime(row.closed_at || row.close_time),
  }));
}

function formatTime(value) {
  const seconds = toTime(value);
  if (!Number.isFinite(seconds)) return "--";
  return new Intl.DateTimeFormat("en-GB", {
    timeZone: DISPLAY_TIME_ZONE,
    day: "2-digit",
    month: "short",
    hour: "2-digit",
    minute: "2-digit",
    hour12: false,
  }).format(new Date(seconds * 1000)) + " " + DISPLAY_TZ_LABEL;
}

function pricePrecision(symbol) {
  const text = String(symbol || "").toUpperCase();
  if (text.includes("JPY")) return 3;
  if (/^[A-Z]{6}$/.test(text)) return 5;
  if (text.startsWith("XAU") || text.startsWith("XAG")) return 2;
  return 2;
}

function formatPrice(symbol, value) {
  const number = Number(value);
  return Number.isFinite(number) ? number.toFixed(pricePrecision(symbol)) : "--";
}

function nearestTime(candles, timestamp) {
  if (!candles.length || !Number.isFinite(timestamp)) return null;
  return candles.reduce((closest, row) => (
    Math.abs(row.time - timestamp) < Math.abs(closest - timestamp) ? row.time : closest
  ), candles[0].time);
}

function calcOverlay(candles, period = 20) {
  let value = null;
  const alpha = 2 / (period + 1);
  return candles.map((row, index) => {
    value = value === null ? row.close : alpha * row.close + (1 - alpha) * value;
    return index < period - 1 ? null : value;
  });
}

export default function TradeReplayChart({
  sym,
  timeframe = "M5",
  candles = [],
  trades = [],
  cursor = 0,
  height = 480,
}) {
  const containerRef = useRef(null);
  const chartRef = useRef(null);
  const candleRef = useRef(null);
  const volumeRef = useRef(null);
  const emaRef = useRef(null);
  const markerRef = useRef(null);
  const priceLinesRef = useRef([]);
  const [hover, setHover] = useState(null);
  const rows = useMemo(() => normalizeCandles(candles), [candles]);
  const normalizedTrades = useMemo(() => normalizeTrades(trades), [trades]);
  const visibleRows = useMemo(() => rows.slice(0, Math.max(0, Math.min(cursor + 1, rows.length))), [rows, cursor]);
  const priceFormat = useMemo(() => ({
    type: "price",
    precision: pricePrecision(sym),
    minMove: 10 ** -pricePrecision(sym),
  }), [sym]);

  useEffect(() => {
    const container = containerRef.current;
    if (!container) return undefined;
    const chart = createChart(container, {
      autoSize: true,
      height,
      layout: { background: { color: "#070b10" }, textColor: "#b8c0ce", fontFamily: "IBM Plex Sans, Segoe UI, sans-serif" },
      grid: { vertLines: { color: "rgba(255,255,255,.05)" }, horzLines: { color: "rgba(255,255,255,.05)" } },
      crosshair: {
        mode: CrosshairMode.Normal,
        vertLine: { color: "rgba(244,179,23,.65)" },
        horzLine: { color: "rgba(244,179,23,.4)" },
      },
      rightPriceScale: { borderColor: "rgba(255,255,255,.12)", scaleMargins: { top: .08, bottom: .22 } },
      timeScale: { borderColor: "rgba(255,255,255,.12)", timeVisible: true, secondsVisible: false, rightOffset: 4, barSpacing: 9 },
      handleScroll: { mouseWheel: true, pressedMouseMove: true, horzTouchDrag: true },
      handleScale: { mouseWheel: true, pinch: true, axisPressedMouseMove: true },
      localization: {
        locale: "en-US",
        priceFormatter: (price) => formatPrice(sym, price),
        timeFormatter: (time) => formatTime(time),
      },
    });
    const candleSeries = chart.addSeries(CandlestickSeries, {
      upColor: "#19d78a",
      downColor: "#ff5965",
      borderUpColor: "#19d78a",
      borderDownColor: "#ff5965",
      wickUpColor: "#a5f6cf",
      wickDownColor: "#ff9aa2",
      priceFormat,
    });
    const volumeSeries = chart.addSeries(HistogramSeries, {
      priceFormat: { type: "volume" },
      priceScaleId: "",
      priceLineVisible: false,
      lastValueVisible: false,
    });
    volumeSeries.priceScale().applyOptions({ scaleMargins: { top: .82, bottom: 0 } });
    const emaSeries = chart.addSeries(LineSeries, {
      color: "#f4b317",
      lineWidth: 2,
      priceLineVisible: false,
      lastValueVisible: false,
      priceFormat,
    });
    const markerApi = createSeriesMarkers(candleSeries, []);
    candleRef.current = candleSeries;
    volumeRef.current = volumeSeries;
    emaRef.current = emaSeries;
    markerRef.current = markerApi;
    chartRef.current = chart;
    chart.subscribeCrosshairMove((param) => {
      const value = param.seriesData.get(candleSeries);
      setHover(value || null);
    });
    return () => {
      priceLinesRef.current.forEach((line) => candleSeries.removePriceLine(line));
      priceLinesRef.current = [];
      chart.remove();
      chartRef.current = null;
      candleRef.current = null;
      volumeRef.current = null;
      emaRef.current = null;
      markerRef.current = null;
    };
  }, [height, priceFormat, sym]);

  useEffect(() => {
    const chart = chartRef.current;
    const candleSeries = candleRef.current;
    const volumeSeries = volumeRef.current;
    const emaSeries = emaRef.current;
    const markerApi = markerRef.current;
    if (!chart || !candleSeries || !volumeSeries || !emaSeries || !markerApi) return;
    if (!visibleRows.length) {
      candleSeries.setData([]);
      volumeSeries.setData([]);
      emaSeries.setData([]);
      markerApi.setMarkers([]);
      return;
    }
    candleSeries.setData(visibleRows.map(({ time, open, high, low, close }) => ({ time, open, high, low, close })));
    volumeSeries.setData(visibleRows.map((row) => ({
      time: row.time,
      value: row.volume,
      color: row.close >= row.open ? "rgba(25,215,138,.3)" : "rgba(255,89,101,.3)",
    })));
    const ema = calcOverlay(visibleRows);
    emaSeries.setData(ema.flatMap((value, index) => value === null ? [] : [{ time: visibleRows[index].time, value }]));
    const lastTime = visibleRows[visibleRows.length - 1].time;
    const visibleTrades = normalizedTrades.filter((trade) => trade.opened_at && trade.opened_at <= lastTime);
    markerApi.setMarkers(visibleTrades.flatMap((trade) => {
      const entryTime = nearestTime(visibleRows, trade.opened_at);
      const exitTime = trade.closed_at && trade.closed_at <= lastTime ? nearestTime(visibleRows, trade.closed_at) : null;
      const buy = trade.direction === "BUY" || trade.direction === "LONG";
      const markers = entryTime ? [{
        time: entryTime,
        position: buy ? "belowBar" : "aboveBar",
        color: buy ? "#19d78a" : "#ff5965",
        shape: buy ? "arrowUp" : "arrowDown",
        text: (buy ? "BUY " : "SELL ") + (trade.trade_id || ""),
      }] : [];
      if (exitTime) markers.push({
        time: exitTime,
        position: buy ? "aboveBar" : "belowBar",
        color: trade.outcome === "win" ? "#19d78a" : trade.outcome === "loss" ? "#ff5965" : "#f4b317",
        shape: "circle",
        text: "EXIT",
      });
      return markers;
    }).sort((a, b) => a.time - b.time));
    priceLinesRef.current.forEach((line) => candleSeries.removePriceLine(line));
    priceLinesRef.current = visibleTrades
      .filter((trade) => Number.isFinite(trade.entry))
      .slice(0, 12)
      .map((trade) => candleSeries.createPriceLine({
        price: trade.entry,
        color: trade.direction === "BUY" || trade.direction === "LONG" ? "rgba(25,215,138,.55)" : "rgba(255,89,101,.55)",
        lineWidth: 1,
        lineStyle: 2,
        axisLabelVisible: false,
        title: trade.direction + " " + (trade.trade_id || ""),
      }));
    chart.timeScale().fitContent();
  }, [visibleRows, normalizedTrades]);

  const current = hover || visibleRows[visibleRows.length - 1] || null;
  return (
    <div className="trade-replay-chart">
      <div className="trade-replay-toolbar">
        <div>
          <strong>{sym}</strong>
          <span>{timeframe} · replay evidence · {current ? formatTime(current.time) : "No candles"}</span>
        </div>
        <div className="trade-replay-ohlc">
          <span>O {formatPrice(sym, current?.open)}</span>
          <span>H {formatPrice(sym, current?.high)}</span>
          <span>L {formatPrice(sym, current?.low)}</span>
          <span>C {formatPrice(sym, current?.close)}</span>
        </div>
      </div>
      <div ref={containerRef} className="trade-replay-canvas" />
      {!visibleRows.length && <div className="trade-replay-empty">No replay candles are available for this symbol and timeframe.</div>}
    </div>
  );
}

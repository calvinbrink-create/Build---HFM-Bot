import { useEffect, useMemo, useRef, useState } from "react";
import {
  CandlestickSeries,
  CrosshairMode,
  HistogramSeries,
  LineSeries,
  createChart,
} from "lightweight-charts";

const DISPLAY_TIME_ZONE = "Africa/Johannesburg";
const DISPLAY_TZ_LABEL = "SAST";

function calcBB(candles, period = 20, mult = 2) {
  return candles.map((_, index) => {
    if (index < period - 1) return null;
    const slice = candles.slice(index - period + 1, index + 1).map((candle) => candle.close);
    const mean = slice.reduce((sum, value) => sum + value, 0) / period;
    const variance = slice.reduce((sum, value) => sum + (value - mean) ** 2, 0) / period;
    const std = Math.sqrt(variance);
    return { upper: mean + mult * std, middle: mean, lower: mean - mult * std };
  });
}

function calcVWAP(candles) {
  let cumTPV = 0;
  let cumVolume = 0;
  return candles.map((candle) => {
    const typical = (candle.high + candle.low + candle.close) / 3;
    const volume = Number.isFinite(candle.volume) && candle.volume > 0 ? candle.volume : 1;
    cumTPV += typical * volume;
    cumVolume += volume;
    return cumVolume > 0 ? cumTPV / cumVolume : typical;
  });
}

function toTime(value) {
  const raw = String(value ?? "").trim();
  if (!raw) return null;
  if (/^\d+$/.test(raw)) {
    const numeric = Number(raw);
    if (!Number.isFinite(numeric)) return null;
    return numeric > 1e12 ? Math.floor(numeric / 1000) : numeric;
  }
  const normalized = raw
    .replace(" ", "T")
    .replace(/\.\d+(?=(Z|[+-]\d{2}:?\d{2})?$)/, "");
  const hasZone = /(?:Z|[+-]\d{2}:?\d{2})$/i.test(normalized);
  const parsed = Math.floor(new Date(hasZone ? normalized : `${normalized}Z`).getTime() / 1000);
  return Number.isFinite(parsed) ? parsed : null;
}

function secondsFromChartTime(value) {
  if (typeof value === "number") return value;
  if (typeof value === "string") return toTime(value);
  if (value && typeof value === "object" && "year" in value && "month" in value && "day" in value) {
    return Date.UTC(value.year, value.month - 1, value.day) / 1000;
  }
  return null;
}

function formatSastTime(value, options = {}) {
  const seconds = secondsFromChartTime(value);
  if (!Number.isFinite(seconds)) return "";
  const date = new Date(seconds * 1000);
  const formatOptions = {
    timeZone: DISPLAY_TIME_ZONE,
    month: options.month || "short",
    day: options.day || "numeric",
    hour12: false,
  };
  if (options.year) formatOptions.year = options.year;
  if (options.showTime !== false) {
    formatOptions.hour = options.hour || "2-digit";
    formatOptions.minute = options.minute || "2-digit";
  }
  const formatted = new Intl.DateTimeFormat("en-US", formatOptions).format(date);
  return options.withZone === false ? formatted : `${formatted} ${DISPLAY_TZ_LABEL}`;
}

function formatSastTick(value, timeframe) {
  const tf = String(timeframe || "").toUpperCase();
  const seconds = secondsFromChartTime(value);
  if (!Number.isFinite(seconds)) return "";
  if (tf === "D1" || tf === "W1" || tf === "MN") {
    return formatSastTime(seconds, { showTime: false, withZone: false });
  }
  return new Intl.DateTimeFormat("en-GB", {
    timeZone: DISPLAY_TIME_ZONE,
    hour: "2-digit",
    minute: "2-digit",
    hour12: false,
  }).format(new Date(seconds * 1000));
}

function decimalPlaces(symbol) {
  const sym = String(symbol || "").toUpperCase();
  if (sym.includes("JPY")) return 3;
  if (/^[A-Z]{6}$/.test(sym)) return 5;
  if (sym.startsWith("XAU") || sym.startsWith("XAG") || sym.startsWith("XPT") || sym.startsWith("XPD")) return 2;
  return 2;
}

function formatPrice(symbol, value) {
  const numeric = Number(value);
  if (!Number.isFinite(numeric)) return "--";
  return numeric.toFixed(decimalPlaces(symbol));
}

function normalizeRows(rows) {
  if (!Array.isArray(rows)) return [];
  const seen = new Set();
  return rows
    .map((row) => {
      const time = toTime(row.ts ?? row.time);
      const candle = {
        time,
        open: Number(row.open),
        high: Number(row.high),
        low: Number(row.low),
        close: Number(row.close),
        volume: Number(row.volume || 0),
        source: row.source || "mt5",
        fresh: row.fresh !== false,
        marketClosed: row.status === "MARKET_CLOSED",
      };
      return candle;
    })
    .filter((candle) => {
      if (
        candle.time === null ||
        !Number.isFinite(candle.open) ||
        !Number.isFinite(candle.high) ||
        !Number.isFinite(candle.low) ||
        !Number.isFinite(candle.close)
      ) {
        return false;
      }
      if (seen.has(candle.time)) return false;
      seen.add(candle.time);
      return true;
    })
    .sort((left, right) => left.time - right.time);
}

export default function CandleChart({
  sym,
  timeframe = "M15",
  candles = [],
  height = 420,
  fill = false,
  sourceLabel = "MT5 bars",
  emptyTitle = "No MT5 bars yet",
  emptyDetail,
  loadingTitle = "Loading MT5 chart",
  loadingDetail = "Connecting to the candle stream.",
}) {
  const containerRef = useRef(null);
  const chartRef = useRef(null);
  const candleRef = useRef(null);
  const volumeRef = useRef(null);
  const overlayRefs = useRef({});
  const didFitRef = useRef(false);
  const [status, setStatus] = useState("loading");
  const [hover, setHover] = useState(null);

  const rows = useMemo(() => normalizeRows(candles), [candles]);
  const priceFormat = useMemo(() => {
    const precision = decimalPlaces(sym);
    return { type: "price", precision, minMove: 10 ** -precision };
  }, [sym]);

  useEffect(() => {
    const container = containerRef.current;
    if (!container) return undefined;

    didFitRef.current = false;
    const chart = createChart(container, {
      autoSize: true,
      height,
      layout: {
        background: { color: "#05070a" },
        textColor: "#b8c0ce",
        fontFamily: "IBM Plex Sans, Segoe UI, ui-sans-serif, system-ui",
      },
      grid: {
        vertLines: { color: "rgba(255,255,255,0.055)" },
        horzLines: { color: "rgba(255,255,255,0.055)" },
      },
      crosshair: {
        mode: CrosshairMode.Normal,
        vertLine: { color: "rgba(244,179,23,0.55)", style: 0, width: 1 },
        horzLine: { color: "rgba(244,179,23,0.35)", style: 0, width: 1 },
      },
      handleScroll: {
        mouseWheel: true,
        pressedMouseMove: true,
        horzTouchDrag: true,
        vertTouchDrag: true,
      },
      handleScale: {
        axisPressedMouseMove: true,
        mouseWheel: true,
        pinch: true,
      },
      rightPriceScale: {
        borderColor: "rgba(255,255,255,0.12)",
        scaleMargins: { top: 0.08, bottom: 0.22 },
      },
      timeScale: {
        borderColor: "rgba(255,255,255,0.12)",
        timeVisible: true,
        secondsVisible: false,
        rightOffset: 8,
        barSpacing: 9,
        minBarSpacing: 2,
        tickMarkFormatter: (time) => formatSastTick(time, timeframe),
      },
      localization: {
        locale: "en-US",
        priceFormatter: (price) => formatPrice(sym, price),
        timeFormatter: (time) => formatSastTime(time),
      },
    });

    const candleSeries = chart.addSeries(CandlestickSeries, {
      upColor: "#18d680",
      downColor: "#ff5965",
      borderUpColor: "#18d680",
      borderDownColor: "#ff5965",
      wickUpColor: "#a6f7cf",
      wickDownColor: "#ff9aa2",
      priceFormat,
    });
    const volumeSeries = chart.addSeries(HistogramSeries, {
      priceFormat: { type: "volume" },
      priceScaleId: "",
      priceLineVisible: false,
      lastValueVisible: false,
    });
    try {
      volumeSeries.priceScale().applyOptions({ scaleMargins: { top: 0.82, bottom: 0 } });
    } catch {
      // Older chart builds may not expose overlay priceScale(). The histogram still renders.
    }

    const bbUpper = chart.addSeries(LineSeries, {
      color: "rgba(76, 151, 255, 0.9)",
      lineWidth: 1,
      priceLineVisible: false,
      lastValueVisible: false,
      priceFormat,
    });
    const bbMid = chart.addSeries(LineSeries, {
      color: "rgba(244, 179, 23, 0.86)",
      lineWidth: 1,
      priceLineVisible: false,
      lastValueVisible: false,
      priceFormat,
    });
    const bbLower = chart.addSeries(LineSeries, {
      color: "rgba(76, 151, 255, 0.9)",
      lineWidth: 1,
      priceLineVisible: false,
      lastValueVisible: false,
      priceFormat,
    });
    const vwap = chart.addSeries(LineSeries, {
      color: "rgba(18, 214, 214, 0.92)",
      lineWidth: 2,
      priceLineVisible: false,
      lastValueVisible: false,
      priceFormat,
    });

    chart.subscribeCrosshairMove((param) => {
      const data = param.seriesData.get(candleSeries);
      if (!data) {
        setHover(null);
        return;
      }
      setHover(data);
    });

    chartRef.current = chart;
    candleRef.current = candleSeries;
    volumeRef.current = volumeSeries;
    overlayRefs.current = { bbUpper, bbMid, bbLower, vwap };

    return () => {
      chartRef.current = null;
      candleRef.current = null;
      volumeRef.current = null;
      overlayRefs.current = {};
      chart.remove();
    };
  }, [height, fill, priceFormat, sym]);

  useEffect(() => {
    didFitRef.current = false;
  }, [sym, timeframe]);

  useEffect(() => {
    const chart = chartRef.current;
    const candleSeries = candleRef.current;
    const volumeSeries = volumeRef.current;
    const { bbUpper, bbMid, bbLower, vwap } = overlayRefs.current;
    if (!chart || !candleSeries || !volumeSeries || !bbUpper || !bbMid || !bbLower || !vwap) return;

    if (!rows.length) {
      setStatus("empty");
      candleSeries.setData([]);
      volumeSeries.setData([]);
      bbUpper.setData([]);
      bbMid.setData([]);
      bbLower.setData([]);
      vwap.setData([]);
      return;
    }

    candleSeries.setData(rows.map(({ time, open, high, low, close }) => ({ time, open, high, low, close })));
    volumeSeries.setData(rows.map((row) => ({
      time: row.time,
      value: row.volume,
      color: row.close >= row.open ? "rgba(24,214,128,0.28)" : "rgba(255,89,101,0.28)",
    })));

    const bb = calcBB(rows);
    const upper = [];
    const middle = [];
    const lower = [];
    bb.forEach((item, index) => {
      if (!item) return;
      upper.push({ time: rows[index].time, value: item.upper });
      middle.push({ time: rows[index].time, value: item.middle });
      lower.push({ time: rows[index].time, value: item.lower });
    });
    bbUpper.setData(upper);
    bbMid.setData(middle);
    bbLower.setData(lower);

    const vwapValues = calcVWAP(rows);
    vwap.setData(rows.map((row, index) => ({ time: row.time, value: vwapValues[index] })));

    if (!didFitRef.current) {
      chart.timeScale().fitContent();
      didFitRef.current = true;
    }
    setStatus(rows[rows.length - 1]?.marketClosed ? "closed" : "ok");
  }, [rows]);

  const last = rows[rows.length - 1] || null;
  const readout = hover || last;
  const readoutTime = readout?.time ? formatSastTime(readout.time) : `${DISPLAY_TZ_LABEL} chart time`;
  const marketClosed = status === "closed";

  return (
    <div className="live-chart-shell" style={{ height: fill ? "100%" : height }}>
      <div className="live-chart-toolbar">
        <div>
          <strong>{sym}</strong>
          <span>{timeframe} · {marketClosed ? "Last session" : sourceLabel} · {readoutTime}</span>
        </div>
        <div className="live-chart-ohlc">
          <span>O {formatPrice(sym, readout?.open)}</span>
          <span>H {formatPrice(sym, readout?.high)}</span>
          <span>L {formatPrice(sym, readout?.low)}</span>
          <span>C {formatPrice(sym, readout?.close)}</span>
        </div>
      </div>
      {marketClosed && (
        <div className="live-chart-closed-badge">
          <span className="live-chart-closed-dot" />
          Market closed - showing the last available session. Live ticking resumes automatically at the open.
        </div>
      )}
      <div ref={containerRef} className="live-chart-canvas" />
      {status === "empty" && (
        <div className="live-chart-empty">
          <strong>{emptyTitle}</strong>
          <span>{emptyDetail || `Waiting for ${sym} ${timeframe} candles.`}</span>
        </div>
      )}
      {status === "loading" && (
        <div className="live-chart-empty">
          <strong>{loadingTitle}</strong>
          <span>{loadingDetail}</span>
        </div>
      )}
    </div>
  );
}

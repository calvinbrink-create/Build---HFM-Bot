import React, { useEffect, useMemo, useState } from "react";
import {
  Activity,
  BarChart3,
  Briefcase,
  Calendar,
  ChevronDown,
  Clock3,
  ExternalLink,
  FileText,
  Globe,
  Grip,
  History,
  LineChart,
  List,
  MonitorSmartphone,
  Newspaper,
  PanelLeft,
  Plus,
  Search,
  Sparkles,
  TrendingDown,
  TrendingUp,
  Wallet,
  X,
} from "lucide-react";
import logoSrc from "./cipherfx-icon.png";
import CandleChart from "./CandleChart";
import {
  ALL_SYMBOLS,
  DEFAULT_ACTIVE_SYMBOLS,
  buildSymbolSnapshot,
} from "./mt5Universe";

const API = `${window.location.origin}/mt5-api`;

const DESKTOP_TABS = [
  { id: "market-hours", label: "Market Hours", icon: Clock3 },
  { id: "watchlist", label: "Watchlist", icon: List },
  { id: "chart", label: "Chart", icon: LineChart },
  { id: "scanner", label: "Scanner", icon: Activity },
  { id: "trade", label: "Trade", icon: Sparkles },
  { id: "positions", label: "Positions", icon: Briefcase },
  { id: "orders", label: "Orders", icon: FileText },
  { id: "history", label: "History", icon: History },
  { id: "calendar", label: "Calendar", icon: Calendar },
  { id: "news", label: "News", icon: Newspaper },
];

const MOBILE_TABS = [
  { id: "quotes", label: "Quotes", icon: TrendingUp },
  { id: "chart", label: "Chart", icon: BarChart3 },
  { id: "scanner", label: "Scan", icon: Activity },
  { id: "trade", label: "Trade", icon: Sparkles },
  { id: "history", label: "History", icon: Clock3 },
];

const TIMEFRAMES = ["M1", "M5", "M15", "H1", "H4", "D1"];
const SYMBOL_GROUP_NAMES = ["All", "Forex", "Metals", "CFDs", "Crypto"];
const DISPLAY_TIME_ZONE = "Africa/Johannesburg";
const DISPLAY_TZ_LABEL = "SAST";

const DESKTOP_EVENTS = [
  { time: "07:00", label: "London Open Liquidity Window", tag: "FX" },
  { time: "10:30", label: "Europe Mid-Session Rotation", tag: "Indices" },
  { time: "15:30", label: "New York Open Volatility", tag: "US" },
  { time: "22:00", label: "Daily Roll & Swap Check", tag: "Rollover" },
];

function fmtPrice(symbol, value) {
  const digits = symbol.includes("JPY") ? 3 : value > 1000 ? 2 : value > 10 ? 3 : 5;
  return Number(value).toFixed(digits);
}

function describeInstrument(symbol, group) {
  if (group === "Forex" && symbol.length >= 6) {
    return `${symbol.slice(0, 3)} vs ${symbol.slice(3, 6)}`;
  }
  if (group === "Indices") return `${symbol} cash index`;
  if (group === "Crypto") return `${symbol.replace("USD", "")} vs USD`;
  return group || "Market";
}

function formatNewsTime(value) {
  if (!value) return "Live";
  try {
    return new Date(Number(value) * 1000).toLocaleString([], {
      timeZone: DISPLAY_TIME_ZONE,
      month: "short",
      day: "numeric",
      hour: "2-digit",
      minute: "2-digit",
    }) + ` ${DISPLAY_TZ_LABEL}`;
  } catch {
    return "Live";
  }
}

function formatClock(value) {
  if (!value) return "Live";
  try {
    return parseDateValue(value).toLocaleTimeString([], {
      timeZone: DISPLAY_TIME_ZONE,
      hour: "2-digit",
      minute: "2-digit",
      second: "2-digit",
    }) + ` ${DISPLAY_TZ_LABEL}`;
  } catch {
    return "Live";
  }
}

function parseDateValue(value) {
  if (!value) return null;
  const raw = String(value).trim();
  if (!raw) return null;
  if (/^\d+(\.\d+)?$/.test(raw)) {
    const stamp = Number(raw);
    if (stamp > 100000000000) return new Date(stamp);
    if (stamp > 1000000000) return new Date(stamp * 1000);
  }
  const normalized = raw.replace(" ", "T");
  const hasZone = /(?:Z|[+-]\d{2}:?\d{2})$/i.test(normalized);
  const parsed = new Date(hasZone || /^\d{4}-\d{2}-\d{2}$/.test(normalized) ? normalized : `${normalized}Z`);
  return Number.isNaN(parsed.getTime()) ? null : parsed;
}

function formatDateTimeLabel(value) {
  const parsed = parseDateValue(value);
  if (!parsed) return "";
  return parsed.toLocaleString([], {
    timeZone: DISPLAY_TIME_ZONE,
    month: "short",
    day: "numeric",
    year: "numeric",
    hour: "2-digit",
    minute: "2-digit",
  }) + ` ${DISPLAY_TZ_LABEL}`;
}

function formatAgeLabel(value) {
  const parsed = parseDateValue(value);
  if (!parsed) return "waiting";
  const diffSeconds = Math.round((Date.now() - parsed.getTime()) / 1000);
  const future = diffSeconds < 0;
  const seconds = Math.abs(diffSeconds);
  const suffix = future ? "" : " ago";
  const prefix = future ? "in " : "";
  if (seconds < 60) return `${prefix}${seconds}s${suffix}`;
  const minutes = Math.floor(seconds / 60);
  if (minutes < 60) return `${prefix}${minutes}m${suffix}`;
  const hours = Math.floor(minutes / 60);
  if (hours < 24) return `${prefix}${hours}h${suffix}`;
  return `${prefix}${Math.floor(hours / 24)}d${suffix}`;
}

function compactCount(value) {
  const numeric = Number(value || 0);
  return numeric.toLocaleString(undefined, { maximumFractionDigits: 0 });
}

function formatMonthLabel(value) {
  const parsed = parseDateValue(value);
  if (!parsed) return "";
  return parsed.toLocaleString([], {
    timeZone: DISPLAY_TIME_ZONE,
    month: "short",
    year: "numeric",
  });
}

function openedLabel(row) {
  return row.openedLabel || formatDateTimeLabel(row.openedAt || row.time) || row.time || "Live";
}

function closedLabel(row) {
  return row.closedLabel || row.tradeLabel || formatDateTimeLabel(row.closedAt) || openedLabel(row);
}

function tradeMonthLabel(row) {
  return row.closedMonth || row.tradeMonth || row.openedMonth || formatMonthLabel(row.closedAt || row.openedAt) || "";
}

function tradeDateLine(row, mode = "position") {
  if (mode === "history") {
    const closed = closedLabel(row);
    const opened = row.openedLabel || formatDateTimeLabel(row.openedAt);
    return opened && opened !== closed ? `Closed ${closed} · Opened ${opened}` : `Closed ${closed}`;
  }
  if (mode === "orders") return row.time ? `Placed ${row.time}` : "Pending order";
  const duration = row.duration ? ` · ${row.duration}` : "";
  return `Opened ${openedLabel(row)}${duration}`;
}

function formatSignedUsd(value) {
  const numeric = Number(value || 0);
  const absolute = Math.abs(numeric).toLocaleString(undefined, {
    minimumFractionDigits: 2,
    maximumFractionDigits: 2,
  });
  return `${numeric >= 0 ? "+" : "-"}$${absolute}`;
}

function describeApiError(error, fallback) {
  const raw = String(error?.message || error || fallback || "Action failed");
  try {
    const parsed = JSON.parse(raw);
    if (parsed?.detail) return String(parsed.detail);
  } catch {
    // API errors are sometimes plain text.
  }
  return raw;
}

function normalizePositionRows(rows) {
  return (rows || []).map((row) => {
    const openedAt = row.opened_at || row.time || "";
    const label = row.opened_label || formatDateTimeLabel(openedAt) || "Live";
    return {
      id: String(row.ticket),
      symbol: row.sym || row.symbol,
      side: String(row.direction || "").toLowerCase(),
      volume: Number(row.qty || row.volume || 0),
      open: Number(row.entry || row.price_open || 0),
      current: Number(row.current || row.price_current || 0),
      profit: Number(row.unrealized || row.profit || 0),
      time: label,
      openedAt,
      openedLabel: label,
      openedDate: row.opened_date || "",
      openedDay: row.opened_day || "",
      openedMonth: row.opened_month_label || formatMonthLabel(openedAt),
      openedYear: row.opened_year || "",
      duration: row.duration_label || "",
    };
  });
}

function formatUsd(value) {
  const numeric = Number(value || 0);
  return `$${numeric.toLocaleString(undefined, {
    minimumFractionDigits: 2,
    maximumFractionDigits: 2,
  })}`;
}

function formatZar(value, rate, { signed = false, compact = false } = {}) {
  const numeric = Number(value || 0) * Number(rate || 0);
  const abs = Math.abs(numeric);
  const sign = signed ? (numeric >= 0 ? "+" : "-") : "";
  if (compact && abs >= 1000) {
    const suffix = abs >= 1000000 ? "m" : "k";
    const divisor = abs >= 1000000 ? 1000000 : 1000;
    return `${sign}R${(abs / divisor).toFixed(1)}${suffix}`;
  }
  return `${sign}R${abs.toLocaleString(undefined, {
    minimumFractionDigits: 2,
    maximumFractionDigits: 2,
  })}`;
}

function sameSymbols(a, b) {
  if (a.length !== b.length) return false;
  return a.every((item, index) => item === b[index]);
}

function toDeskGroup(group) {
  const raw = String(group || "").trim();
  const normalized = raw.toLowerCase();
  if (!normalized) return "Forex";
  if (normalized.includes("crypto") || normalized.includes("bitcoin") || normalized.includes("ethereum")) return "Crypto";
  if (normalized.includes("metal") || normalized.includes("gold") || normalized.includes("silver") || normalized.includes("xau") || normalized.includes("xag")) return "Metals";
  if (
    normalized.includes("forex")
    || normalized.includes("currency")
    || normalized.includes("dollar")
    || normalized.includes("euro")
    || normalized.includes("yen")
    || normalized.includes("pound")
    || normalized.includes("franc")
  ) return "Forex";
  if (
    normalized.includes("energie")
    || normalized.includes("oil")
    || normalized.includes("gas")
    || normalized.includes("indice")
    || normalized.includes("index")
    || normalized.includes("stock")
    || normalized.includes("share")
    || normalized.includes("etf")
  ) return "CFDs";
  if (["Energies", "Indices", "Stocks", "ETFs"].includes(raw)) return "CFDs";
  if (["Forex", "Metals", "Crypto", "CFDs"].includes(raw)) return raw;
  return raw || "Forex";
}

function groupBadge(group) {
  const bucket = toDeskGroup(group);
  if (!group || bucket === group) return bucket;
  return `${bucket} · ${group}`;
}

function buildCandles(seed = 1, scale = 1) {
  return Array.from({ length: 26 }, (_, index) => {
    const pivot = 40 + Math.sin((index + seed) / 2.8) * 18 + ((index % 6) - 3) * 2.5;
    const open = pivot + Math.sin(index * 1.2 + seed) * 4 * scale;
    const close = pivot + Math.cos(index * 1.15 + seed / 2) * 5 * scale;
    const high = Math.max(open, close) + 4 + (index % 4);
    const low = Math.min(open, close) - 4 - (index % 3);
    return { open, close, high, low };
  });
}

function priceToY(value, min, max, height) {
  const padding = 14;
  const range = max - min || 1;
  return height - padding - ((value - min) / range) * (height - padding * 2);
}

function MiniChart({ symbol, timeframe, mobile = false, candleData = [] }) {
  const symbolIndex = ALL_SYMBOLS.findIndex((item) => item.symbol === symbol);
  const tfIndex = TIMEFRAMES.indexOf(timeframe);
  const fallbackCandles = useMemo(
    () => buildCandles((symbolIndex + 2) * 1.7 + tfIndex, mobile ? 0.86 : 1),
    [mobile, symbolIndex, tfIndex]
  );
  const candles = candleData.length ? candleData.map((candle) => ({
    open: Number(candle.open || 0),
    close: Number(candle.close || 0),
    high: Number(candle.high || 0),
    low: Number(candle.low || 0),
  })) : fallbackCandles;
  const width = mobile ? 360 : 820;
  const height = mobile ? 470 : 430;
  const candleWidth = width / (candles.length + 4);
  const highs = candles.map((candle) => candle.high);
  const lows = candles.map((candle) => candle.low);
  const max = Math.max(...highs);
  const min = Math.min(...lows);
  const upperLine = candles.map((candle, index) => {
    const x = (index + 2) * candleWidth;
    const y = priceToY(candle.high + 8, min - 6, max + 10, height);
    return `${x},${y}`;
  }).join(" ");
  const lowerLine = candles.map((candle, index) => {
    const x = (index + 2) * candleWidth;
    const y = priceToY(candle.low - 8, min - 14, max + 2, height);
    return `${x},${y}`;
  }).join(" ");

  return (
    <div className={`chart-surface${mobile ? " mobile" : ""}`}>
      <svg viewBox={`0 0 ${width} ${height}`} className="chart-svg" preserveAspectRatio="none">
        <defs>
          <linearGradient id={`fade-${symbol}-${timeframe}-${mobile ? "m" : "d"}`} x1="0" x2="0" y1="0" y2="1">
            <stop offset="0%" stopColor="rgba(122,52,255,.18)" />
            <stop offset="100%" stopColor="rgba(122,52,255,0)" />
          </linearGradient>
        </defs>
        {Array.from({ length: 8 }).map((_, row) => (
          <line
            key={`h-${row}`}
            x1="0"
            x2={width}
            y1={(height / 8) * row}
            y2={(height / 8) * row}
            className="chart-grid"
          />
        ))}
        {Array.from({ length: 10 }).map((_, col) => (
          <line
            key={`v-${col}`}
            x1={(width / 10) * col}
            x2={(width / 10) * col}
            y1="0"
            y2={height}
            className="chart-grid"
          />
        ))}
        <polygon
          points={`${upperLine} ${lowerLine.split(" ").reverse().join(" ")}`}
          fill={`url(#fade-${symbol}-${timeframe}-${mobile ? "m" : "d"})`}
        />
        <polyline points={upperLine} className="chart-channel" />
        <polyline points={lowerLine} className="chart-channel alt" />
        {candles.map((candle, index) => {
          const x = (index + 2) * candleWidth;
          const isUp = candle.close >= candle.open;
          const openY = priceToY(candle.open, min, max, height);
          const closeY = priceToY(candle.close, min, max, height);
          const highY = priceToY(candle.high, min, max, height);
          const lowY = priceToY(candle.low, min, max, height);
          const bodyY = Math.min(openY, closeY);
          const bodyH = Math.max(Math.abs(closeY - openY), 2.4);
          return (
            <g key={`${symbol}-${timeframe}-${index}`} className={isUp ? "candle up" : "candle down"}>
              <line x1={x} x2={x} y1={highY} y2={lowY} className="wick" />
              <rect x={x - candleWidth * 0.27} y={bodyY} width={candleWidth * 0.54} height={bodyH} rx="1.5" />
            </g>
          );
        })}
        <line x1="0" x2={width} y1={height * 0.61} y2={height * 0.61} className="entry-line" />
      </svg>
      <div className="chart-callout">
        <div>30.08.2022 18:00 · 1 events</div>
        <strong>{DESKTOP_EVENTS[(symbolIndex + tfIndex + (mobile ? 1 : 0)) % DESKTOP_EVENTS.length]}</strong>
        <span>Actual: -9223372036854, Forecast: -, Previous: -</span>
      </div>
    </div>
  );
}

function WatchlistTable({ symbols, activeSymbols, selectedSymbol, onSelect, onToggle }) {
  return (
    <div className="watchlist-table">
      {symbols.map((item) => {
        const isActive = activeSymbols.includes(item.symbol);
        const isSelected = selectedSymbol === item.symbol;
        return (
          <button
            key={item.symbol}
            className={`watch-row${isSelected ? " selected" : ""}`}
            onClick={() => onSelect(item.symbol)}
          >
            <div className="watch-main">
              <span className={`triangle ${item.change >= 0 ? "up" : "down"}`} />
              <div>
                <div className="watch-symbol-line">
                  <strong>{item.symbol}</strong>
                  <span className="watch-group">{item.group}</span>
                </div>
                <div className="watch-meta">
                  Low: {fmtPrice(item.symbol, item.low)} · High: {fmtPrice(item.symbol, item.high)}
                </div>
              </div>
            </div>
            <div className="watch-prices">
              <span>{fmtPrice(item.symbol, item.bid)}</span>
              <span>{fmtPrice(item.symbol, item.ask)}</span>
              <span className={item.change >= 0 ? "pos" : "neg"}>{Math.abs(item.change).toFixed(2)}%</span>
              <span
                className={`watch-toggle${isActive ? " active" : ""}`}
                aria-label={isActive ? `Remove ${item.symbol} from active symbols` : `Add ${item.symbol} to active symbols`}
                title={isActive ? "Active symbol" : "Add symbol"}
                onClick={(event) => {
                  event.stopPropagation();
                  onToggle(item.symbol);
                }}
              >
                {isActive ? "On" : "Add"}
              </span>
            </div>
          </button>
        );
      })}
    </div>
  );
}

export function TradingDashboard() {
  const [desktopTab, setDesktopTab] = useState("chart");
  const [mobileTab, setMobileTab] = useState("chart");
  const [selectedSymbol, setSelectedSymbol] = useState("EURUSD");
  const [timeframe, setTimeframe] = useState("M15");
  const [mobileTimeframe, setMobileTimeframe] = useState("M15");
  const [searchTerm, setSearchTerm] = useState("");
  const [symbolGroupFilter, setSymbolGroupFilter] = useState("All");
  const [mobileSearchTerm, setMobileSearchTerm] = useState("");
  const [mobileGroupFilter, setMobileGroupFilter] = useState("All");
  const [quickSearchTerm, setQuickSearchTerm] = useState("");
  const [quickGroupFilter, setQuickGroupFilter] = useState("All");
  const [watchlistMode, setWatchlistMode] = useState("active");
  const [watchlistOpen, setWatchlistOpen] = useState(true);
  const [symbolPickerOpen, setSymbolPickerOpen] = useState(false);
  const [timeframePickerOpen, setTimeframePickerOpen] = useState(false);
  const [mobileQuotesMode, setMobileQuotesMode] = useState("advanced");
  const [mobileHistoryTab, setMobileHistoryTab] = useState("deals");
  const [volume, setVolume] = useState(0.01);
  const [stopLoss, setStopLoss] = useState("0.99683");
  const [takeProfit, setTakeProfit] = useState("1.00399");
  const [activeSymbols, setActiveSymbols] = useState(() => [...DEFAULT_ACTIVE_SYMBOLS]);
  const [token, setToken] = useState(() => localStorage.getItem("cipherfx_mt5_token") || "");
  const [status, setStatus] = useState(null);
  const [terminal, setTerminal] = useState(null);
  const [symbolsFeed, setSymbolsFeed] = useState([]);
  const [signalsFeed, setSignalsFeed] = useState([]);
  const [scanner, setScanner] = useState(null);
  const [audit, setAudit] = useState(null);
  const [marketHours, setMarketHours] = useState(null);
  const [scannerExpanded, setScannerExpanded] = useState(null);
  const [desktopCandles, setDesktopCandles] = useState([]);
  const [mobileCandles, setMobileCandles] = useState([]);
  const [newsFeed, setNewsFeed] = useState([]);
  const [apiError, setApiError] = useState("");
  const [actionMessage, setActionMessage] = useState("");
  const [busyAction, setBusyAction] = useState("");
  const [pendingClose, setPendingClose] = useState(null);
  const [positions, setPositions] = useState([]);
  const [orders, setOrders] = useState([]);
  const [deals, setDeals] = useState([]);

  async function hRaw(method, path, body, bearer = token, timeoutMs = 15000) {
    const controller = new AbortController();
    const timeoutId = window.setTimeout(() => controller.abort(), timeoutMs);
    try {
      const response = await fetch(`${API}${path}`, {
        method,
        headers: {
          "Content-Type": "application/json",
          ...(bearer ? { Authorization: `Bearer ${bearer}` } : {}),
        },
        body: body ? JSON.stringify(body) : undefined,
        signal: controller.signal,
      });
      if (!response.ok) {
        const raw = await response.text();
        const error = new Error(raw || response.statusText);
        error.status = response.status;
        throw error;
      }
      return response.json();
    } catch (error) {
      if (error?.name === "AbortError") {
        throw new Error(`MT5 API timed out after ${Math.round(timeoutMs / 1000)} seconds`);
      }
      throw error;
    } finally {
      window.clearTimeout(timeoutId);
    }
  }

  async function ensureToken(force = false) {
    const nextToken = force ? "" : (token || localStorage.getItem("cipherfx_mt5_token") || "");
    if (nextToken) {
      if (nextToken !== token) setToken(nextToken);
      return nextToken;
    }
    window.dispatchEvent(new CustomEvent("cipherfx:auth-expired", { detail: { dashboard: "mt5" } }));
    throw new Error("Login required");
  }

  async function h(method, path, body, bearer = token, timeoutMs = 15000) {
    try {
      return await hRaw(method, path, body, bearer, timeoutMs);
    } catch (error) {
      if (
        error?.status === 401
        || error?.status === 403
        || /401|403|Not authenticated|expired session|Invalid or expired session/i.test(String(error?.message || ""))
      ) {
        localStorage.removeItem("cipherfx_mt5_token");
        setToken("");
        window.dispatchEvent(new CustomEvent("cipherfx:auth-expired", { detail: { dashboard: "mt5" } }));
      }
      throw error;
    }
  }

  useEffect(() => {
    let cancelled = false;
    async function loadLive() {
      try {
        setApiError("");
        const sessionToken = await ensureToken();
        const [nextStatus, nextTerminal, nextSymbols, nextTrades, nextOrders, nextScanner, nextAudit, nextMarketHours] = await Promise.all([
          h("GET", "/status", null, sessionToken),
          h("GET", "/terminal", null, sessionToken),
          h("GET", "/symbols", null, sessionToken).catch(() => []),
          h("GET", "/trades?limit=100&include_history=true", null, sessionToken).catch(() => []),
          h("GET", "/orders", null, sessionToken).catch(() => []),
          h("GET", "/scanner", null, sessionToken).catch(() => null),
          h("GET", "/audit/summary", null, sessionToken).catch(() => null),
          h("GET", "/market-hours", null, sessionToken).catch(() => null),
        ]);
        const nextSignals = await h("GET", "/signals", null, sessionToken).catch(() => []);
        if (cancelled) return;
        setStatus(nextStatus);
        setTerminal(nextTerminal);
        setSymbolsFeed(nextSymbols);
        setSignalsFeed(nextSignals);
        setScanner(nextScanner);
        setAudit(nextAudit);
        setMarketHours(nextMarketHours);
        setOrders((nextOrders || []).map((row) => ({
          id: String(row.ticket),
          symbol: row.symbol,
          type: row.order_type || row.side || "pending",
          volume: Number(row.volume || 0),
          price: Number(row.price_open || 0),
          sl: Number(row.sl || 0),
          tp: Number(row.tp || 0),
          status: row.state || "placed",
          time: formatDateTimeLabel(row.time_setup || row.time) || row.time_setup || row.time || "Pending",
        })));
        setPositions(normalizePositionRows(nextTerminal?.positions));
        setDeals((nextTrades || []).map((row, index) => {
          const openedAt = row.opened_at || "";
          const closedAt = row.closed_at || row.trade_date || "";
          return {
            id: String(row.id || row.trade_id || row.ticket || row.closed_at || index),
            symbol: row.sym || row.symbol || selectedSymbol,
            side: String(row.direction || row.side || "").toLowerCase(),
            type: row.outcome || row.status || "deal",
            volume: Number(row.qty || row.volume || 0),
            entry: Number(row.entry_price || row.entry || row.price || 0),
            exit: Number(row.exit_price || row.exit || row.price || 0),
            pnl: Number(row.realized || row.pnl || row.profit || 0),
            openedAt,
            closedAt,
            openedLabel: row.opened_label || formatDateTimeLabel(openedAt),
            closedLabel: row.closed_label || formatDateTimeLabel(closedAt),
            tradeLabel: row.trade_label || formatDateTimeLabel(row.trade_date),
            tradeDate: row.trade_date || row.closed_date || row.opened_date || "",
            closedDate: row.closed_date || "",
            openedDate: row.opened_date || "",
            closedDay: row.closed_day || "",
            openedDay: row.opened_day || "",
            closedMonth: row.closed_month_label || row.trade_month_label || formatMonthLabel(closedAt),
            openedMonth: row.opened_month_label || formatMonthLabel(openedAt),
            duration: row.duration_label || "",
          };
        }));
      } catch (error) {
        if (!cancelled) setApiError(describeApiError(error, "MT5 API load failed"));
      }
    }
    loadLive();
    const id = window.setInterval(loadLive, 5000);
    return () => {
      cancelled = true;
      window.clearInterval(id);
    };
  }, [selectedSymbol, token]);

  useEffect(() => {
    let cancelled = false;
    async function loadCandles(targetTf, setter) {
      try {
        const sessionToken = await ensureToken();
        const rows = await h(
          "GET",
          `/candles?sym=${encodeURIComponent(selectedSymbol)}&timeframe=${encodeURIComponent(targetTf)}&limit=140`,
          null,
          sessionToken
        );
        if (!cancelled) setter(Array.isArray(rows) ? rows : []);
      } catch {
        if (!cancelled) setter([]);
      }
    }
    loadCandles(timeframe, setDesktopCandles);
    const id = window.setInterval(() => loadCandles(timeframe, setDesktopCandles), 1000);
    return () => {
      cancelled = true;
      window.clearInterval(id);
    };
  }, [selectedSymbol, timeframe, token]);

  useEffect(() => {
    let cancelled = false;
    async function loadCandles(targetTf, setter) {
      try {
        const sessionToken = await ensureToken();
        const rows = await h(
          "GET",
          `/candles?sym=${encodeURIComponent(selectedSymbol)}&timeframe=${encodeURIComponent(targetTf)}&limit=110`,
          null,
          sessionToken
        );
        if (!cancelled) setter(Array.isArray(rows) ? rows : []);
      } catch {
        if (!cancelled) setter([]);
      }
    }
    loadCandles(mobileTimeframe, setMobileCandles);
    const id = window.setInterval(() => loadCandles(mobileTimeframe, setMobileCandles), 1000);
    return () => {
      cancelled = true;
      window.clearInterval(id);
    };
  }, [selectedSymbol, mobileTimeframe, token]);

  useEffect(() => {
    let cancelled = false;
    async function loadNews() {
      try {
        const sessionToken = await ensureToken();
        const rows = await h("GET", `/news?sym=${encodeURIComponent(selectedSymbol)}&limit=8`, null, sessionToken);
        if (!cancelled) setNewsFeed(Array.isArray(rows) ? rows : []);
      } catch {
        if (!cancelled) setNewsFeed([]);
      }
    }
    loadNews();
    return () => { cancelled = true; };
  }, [selectedSymbol, token]);

  const liveSymbols = useMemo(() => {
    const tickMap = new Map((terminal?.ticks || []).map((row) => [row.symbol, row]));
    const signalMap = new Map((signalsFeed || []).map((row) => [row.sym, row]));
    const apiRows = new Map((symbolsFeed || []).map((row) => [row.symbol, row]));
    const resolvedAliasSymbols = new Set(
      (symbolsFeed || [])
        .filter((row) => row.resolved_symbol && row.resolved_symbol !== row.symbol)
        .map((row) => String(row.resolved_symbol).toUpperCase())
    );
    const merged = ALL_SYMBOLS.map((fallback) => {
      const row = apiRows.get(fallback.symbol) || {};
      const resolvedSymbol = row.resolved_symbol || fallback.symbol;
      const tick = tickMap.get(fallback.symbol) || tickMap.get(resolvedSymbol) || {};
      const signal = signalMap.get(fallback.symbol) || {};
      const group = row.description || fallback.group;
      return {
        symbol: fallback.symbol,
        resolvedSymbol,
        group,
        bucket: toDeskGroup(row.path || group || fallback.group),
        bid: Number(tick.bid ?? fallback.bid),
        ask: Number(tick.ask ?? fallback.ask),
        change: Number(signal.chg ?? fallback.change ?? 0),
        low: Number(signal.low ?? fallback.low ?? Math.min(fallback.bid || 0, fallback.ask || 0)),
        high: Number(signal.high ?? fallback.high ?? Math.max(fallback.bid || 0, fallback.ask || 0)),
        spread: Number(tick.spread ?? fallback.spread ?? 0),
        visible: row.visible ?? activeSymbols.includes(fallback.symbol) ?? DEFAULT_ACTIVE_SYMBOLS.includes(fallback.symbol),
      };
    });

    for (const row of symbolsFeed || []) {
      if (merged.find((item) => item.symbol === row.symbol)) continue;
      if (resolvedAliasSymbols.has(String(row.symbol || "").toUpperCase())) continue;
      const fallback = buildSymbolSnapshot(row.symbol, row.description || "Custom");
      const resolvedSymbol = row.resolved_symbol || row.symbol;
      const tick = tickMap.get(row.symbol) || tickMap.get(resolvedSymbol) || {};
      const signal = signalMap.get(row.symbol) || {};
      const group = row.description || fallback.group;
      merged.push({
        symbol: row.symbol,
        resolvedSymbol,
        group,
        bucket: toDeskGroup(row.path || group || fallback.group),
        bid: Number(tick.bid ?? fallback.bid),
        ask: Number(tick.ask ?? fallback.ask),
        change: Number(signal.chg ?? fallback.change ?? 0),
        low: Number(signal.low ?? fallback.low),
        high: Number(signal.high ?? fallback.high),
        spread: Number(tick.spread ?? fallback.spread ?? 0),
        visible: row.visible ?? activeSymbols.includes(row.symbol),
      });
    }

    return merged.sort((left, right) => {
      if (left.visible !== right.visible) return left.visible ? -1 : 1;
      if (left.group !== right.group) return left.group.localeCompare(right.group);
      return left.symbol.localeCompare(right.symbol);
    });
  }, [terminal, symbolsFeed, signalsFeed, activeSymbols]);

  useEffect(() => {
    if (!liveSymbols.length) return;
    setActiveSymbols((current) => {
      const known = new Set(liveSymbols.map((item) => item.symbol));
      const kept = current.filter((symbol) => known.has(symbol));
      const seeded = kept.length ? kept : DEFAULT_ACTIVE_SYMBOLS.filter((symbol) => known.has(symbol));
      return sameSymbols(current, seeded) ? current : seeded;
    });
    if (!liveSymbols.find((item) => item.symbol === selectedSymbol)) {
      setSelectedSymbol(liveSymbols[0].symbol);
    }
  }, [liveSymbols, selectedSymbol]);

  const filteredSymbols = useMemo(() => {
    const query = searchTerm.trim().toUpperCase();
    return liveSymbols.filter((item) => {
      const groupMatch = symbolGroupFilter === "All" || item.bucket === symbolGroupFilter;
      const queryMatch = !query
        || item.symbol.includes(query)
        || String(item.group || "").toUpperCase().includes(query)
        || String(item.bucket || "").toUpperCase().includes(query);
      return groupMatch && queryMatch;
    });
  }, [searchTerm, liveSymbols, symbolGroupFilter]);

  const quickSymbols = useMemo(() => {
    const query = quickSearchTerm.trim().toUpperCase();
    return liveSymbols.filter((item) => {
      const groupMatch = quickGroupFilter === "All" || item.bucket === quickGroupFilter;
      const queryMatch = !query
        || item.symbol.includes(query)
        || String(item.group || "").toUpperCase().includes(query)
        || String(item.bucket || "").toUpperCase().includes(query);
      return groupMatch && queryMatch;
    });
  }, [liveSymbols, quickGroupFilter, quickSearchTerm]);

  const mobilePanelSymbols = useMemo(() => {
    const query = mobileSearchTerm.trim().toUpperCase();
    const base = watchlistMode === "market"
      ? liveSymbols
      : liveSymbols.filter((item) => activeSymbols.includes(item.symbol));
    return base.filter((item) => {
      const groupMatch = mobileGroupFilter === "All" || item.bucket === mobileGroupFilter;
      const queryMatch = !query
        || item.symbol.includes(query)
        || String(item.group || "").toUpperCase().includes(query)
        || String(item.bucket || "").toUpperCase().includes(query);
      return groupMatch && queryMatch;
    });
  }, [activeSymbols, liveSymbols, mobileGroupFilter, mobileSearchTerm, watchlistMode]);

  const selected = useMemo(
    () => liveSymbols.find((item) => item.symbol === selectedSymbol) || liveSymbols[0] || ALL_SYMBOLS[0],
    [selectedSymbol, liveSymbols]
  );

  const activeWatchlist = useMemo(
    () => liveSymbols.filter((item) => activeSymbols.includes(item.symbol)),
    [activeSymbols, liveSymbols]
  );
  const filteredWatchlist = useMemo(
    () => activeWatchlist.filter((item) => filteredSymbols.some((row) => row.symbol === item.symbol)),
    [activeWatchlist, filteredSymbols]
  );
  const panelSymbols = watchlistMode === "market" ? filteredSymbols : filteredWatchlist;
  const selectedPositions = positions.filter((row) => row.symbol === selected.symbol);
  const selectedOrders = orders.filter((row) => row.symbol === selected.symbol);
  const recentDeals = deals.filter((row) => row.symbol === selected.symbol).slice(0, 8);
  const marketPulse = useMemo(
    () => [...(signalsFeed || [])].sort((left, right) => Math.abs(Number(right.chg || 0)) - Math.abs(Number(left.chg || 0))).slice(0, 8),
    [signalsFeed]
  );
  const scannerSummary = scanner?.summary || {};
  const scannerScoreBands = scanner?.score_bands_today?.length
    ? scanner.score_bands_today
    : scanner?.score_bands_week || [];
  const scannerRecentRows = scanner?.recent_events?.length
    ? scanner.recent_events
    : scanner?.current_signals || [];
  const scannerBlockers = scanner?.blockers || [];
  const scoreBandMax = Math.max(1, ...scannerScoreBands.map((row) => Number(row.count || 0)));
  const scannerState = status?.halt
    ? "Blocked"
    : Number(scannerSummary.current_actionable || 0) > 0
      ? "Signal found"
      : "Scanning";
  const heartbeatSource = status?.last_heartbeat || terminal?.updated_at;
  const quoteClock = formatClock(terminal?.updated_at || status?.last_heartbeat);
  const heartbeatLabel = formatDateTimeLabel(heartbeatSource) || "Live feed";
  const serverClockLabel = formatClock(heartbeatSource) || "Live";

  function cycleDesktopTimeframe() {
    const index = TIMEFRAMES.indexOf(timeframe);
    setTimeframe(TIMEFRAMES[(index + 1) % TIMEFRAMES.length]);
  }

  function selectDesktopSymbol(symbol) {
    setSelectedSymbol(symbol);
    setSymbolPickerOpen(false);
    setDesktopTab("chart");
  }

  function selectDesktopTimeframe(nextTimeframe) {
    setTimeframe(nextTimeframe);
    setTimeframePickerOpen(false);
  }

  function selectMobileSymbol(symbol) {
    setSelectedSymbol(symbol);
    setMobileTab("chart");
  }

  const totals = useMemo(() => {
    const openPnl = positions.reduce((sum, item) => sum + item.profit, 0);
    const closedPnl = deals.reduce((sum, item) => sum + item.pnl, 0);
    const balance = Number(terminal?.account?.balance ?? status?.balance ?? 0);
    const equity = Number(terminal?.account?.equity ?? status?.equity ?? balance + openPnl);
    const margin = Number(terminal?.account?.margin ?? 0);
    const free = Number(terminal?.account?.free_margin ?? Math.max(equity - margin, 0));
    return {
      balance: Number.isFinite(balance) ? balance : 0,
      equity: Number.isFinite(equity) ? equity : 0,
      margin: Number.isFinite(margin) ? margin : 0,
      free: Number.isFinite(free) ? free : 0,
      openPnl,
      closedPnl,
    };
  }, [deals, positions, status, terminal]);
  const positionTracker = useMemo(() => {
    const bySymbol = new Map();
    positions.forEach((row) => {
      const symbol = row.symbol || "MT5";
      const existing = bySymbol.get(symbol) || { symbol, profit: 0, volume: 0, count: 0, winners: 0, losers: 0 };
      const profit = Number(row.profit || 0);
      existing.profit += profit;
      existing.volume += Number(row.volume || 0);
      existing.count += 1;
      if (profit >= 0) existing.winners += 1;
      else existing.losers += 1;
      bySymbol.set(symbol, existing);
    });
    const rows = Array.from(bySymbol.values()).sort((left, right) => Math.abs(right.profit) - Math.abs(left.profit));
    const maxAbs = Math.max(1, ...rows.map((row) => Math.abs(row.profit)));
    return {
      rows,
      maxAbs,
      winners: positions.filter((row) => Number(row.profit || 0) >= 0).length,
      losers: positions.filter((row) => Number(row.profit || 0) < 0).length,
      best: rows.length ? [...rows].sort((left, right) => right.profit - left.profit)[0] : null,
      worst: rows.length ? [...rows].sort((left, right) => left.profit - right.profit)[0] : null,
    };
  }, [positions]);
  const usdZarRate = Number(status?.usd_zar_rate || terminal?.currency?.usd_zar_rate || 16.21);
  const usdZarLabel = `USD/ZAR ${usdZarRate.toFixed(2)}`;
  const accountCurrency = status?.currency || terminal?.currency?.account || terminal?.account?.currency || "USD";
  const mobileDailyPnl = Number(status?.daily_pnl ?? totals.closedPnl ?? 0);
  const mobilePnlClass = mobileDailyPnl >= 0 ? "profit" : "loss";

  function toggleSymbol(symbol) {
    const enabled = !activeSymbols.includes(symbol);
    setActiveSymbols((current) => (
      enabled ? [...current, symbol] : current.filter((item) => item !== symbol)
    ));
    h("POST", "/symbols/activate", { symbol, enabled }).catch((error) => {
      setApiError(describeApiError(error, "Failed to update symbol"));
    });
  }

  async function refreshTerminalSnapshot() {
    const nextTerminal = await h("GET", "/terminal");
    setTerminal(nextTerminal);
    setPositions(normalizePositionRows(nextTerminal?.positions));
    return nextTerminal;
  }

  async function submitTrade(side) {
    const actionKey = `trade-${side}`;
    const price = side === "buy" ? selected.ask : selected.bid;
    const newPosition = {
      id: `${Date.now()}`,
      symbol: selected.symbol,
      side,
      volume: Number(volume),
      open: price,
      current: price,
      profit: 0,
      time: "Now",
    };
    const newDeal = {
      id: `${Date.now()}-deal`,
      symbol: selected.symbol,
      side,
      volume: Number(volume),
      entry: price,
      exit: price,
      pnl: 0,
    };
    try {
      setBusyAction(actionKey);
      setActionMessage(`${side.toUpperCase()} order sending...`);
      await h("POST", "/orders/market", {
        symbol: selected.symbol,
        side: side.toUpperCase(),
        volume: Number(volume),
        sl: Number(stopLoss || 0),
        tp: Number(takeProfit || 0),
      });
      setPositions((current) => [newPosition, ...current]);
      setDeals((current) => [newDeal, ...current]);
      setDesktopTab("positions");
      setMobileTab("trade");
      setApiError("");
      setActionMessage(`${side.toUpperCase()} order sent to MT5.`);
      await refreshTerminalSnapshot().catch(() => null);
    } catch (error) {
      const message = describeApiError(error, "MT5 market order failed");
      setApiError(message);
      setActionMessage(`Order failed: ${message}`);
    } finally {
      setBusyAction("");
    }
  }

  async function submitPendingOrder() {
    try {
      setBusyAction("pending");
      setActionMessage("Pending order sending...");
      await h("POST", "/orders/pending", {
        symbol: selected.symbol,
        side: "BUY",
        volume: Number(volume),
        price: Number(selected.bid),
        sl: Number(stopLoss || 0),
        tp: Number(takeProfit || 0),
        pending_type: "LIMIT",
      });
      setOrders((current) => [
        {
          id: `${Date.now()}`,
          symbol: selected.symbol,
          type: "buy limit",
          volume: Number(volume),
          price: Number(selected.bid),
          sl: Number(stopLoss),
          tp: Number(takeProfit),
          status: "placed",
        },
        ...current,
      ]);
      setDesktopTab("orders");
      setApiError("");
      setActionMessage("Pending order sent to MT5.");
    } catch (error) {
      const message = describeApiError(error, "MT5 pending order failed");
      setApiError(message);
      setActionMessage(`Pending order failed: ${message}`);
    } finally {
      setBusyAction("");
    }
  }

  const renderLedgerRows = (rows, type) => (
    rows.map((row) => {
      const month = tradeMonthLabel(row);
      const dateText = type === "history"
        ? closedLabel(row)
        : type === "positions"
          ? openedLabel(row)
          : row.time || "Pending";
      const dateSub = type === "history"
        ? `${month}${row.duration ? ` · ${row.duration}` : ""}`
        : type === "positions"
          ? `${month}${row.duration ? ` · ${row.duration}` : ""}`
          : row.status;
      return (
        <div className="ledger-row" key={row.id}>
          <span>{row.symbol}</span>
          <span>{type === "positions" ? row.id : row.type || row.side}</span>
          <span className="ledger-date"><strong>{dateText}</strong>{dateSub && <em>{dateSub}</em>}</span>
          <span>{type === "orders" ? row.price : row.side || row.type}</span>
          <span>{type === "history" ? `${row.entry} → ${row.exit}` : row.volume}</span>
          <span className={(row.profit ?? row.pnl ?? 0) >= 0 ? "pos" : "neg"}>
            {(row.profit ?? row.pnl ?? 0).toFixed(2)}
            {type === "positions" && (
              <button
                type="button"
                className="row-action danger"
                disabled={busyAction === `close-${row.id}`}
                onClick={() => closePosition(row.id)}
              >
                {busyAction === `close-${row.id}` ? "Closing..." : "Close"}
              </button>
            )}
            {type === "orders" && (
              <button
                type="button"
                className="row-action"
                disabled={busyAction === `cancel-${row.id}`}
                onClick={() => cancelOrder(row.id)}
              >
                {busyAction === `cancel-${row.id}` ? "Cancelling..." : "Cancel"}
              </button>
            )}
          </span>
        </div>
      );
    })
  );

  async function closePosition(ticket) {
    if (busyAction) return;
    const position = positions.find((row) => String(row.id) === String(ticket));
    const label = position ? `${position.symbol} ${position.side.toUpperCase()} ${position.volume}` : `ticket ${ticket}`;
    setPendingClose({
      ticket,
      label,
      symbol: position?.symbol || "",
      side: position?.side || "",
      volume: position?.volume || 0,
      profit: Number(position?.profit || 0),
    });
  }

  async function confirmClosePosition() {
    if (!pendingClose || busyAction) return;
    const { ticket, label } = pendingClose;
    setPendingClose(null);
    try {
      setBusyAction(`close-${ticket}`);
      setActionMessage(`Closing ${label}...`);
      await h("POST", `/positions/${ticket}/close`, null, token, 12000);
      setPositions((current) => current.filter((row) => String(row.id) !== String(ticket)));
      await refreshTerminalSnapshot().catch(() => null);
      setApiError("");
      setActionMessage(`Close request accepted for ${label}.`);
    } catch (error) {
      const message = describeApiError(error, "Failed to close position");
      setApiError(message);
      setActionMessage(`Close failed: ${message}`);
      await refreshTerminalSnapshot().catch(() => null);
    } finally {
      setBusyAction("");
    }
  }

  async function cancelOrder(ticket) {
    if (busyAction) return;
    try {
      setBusyAction(`cancel-${ticket}`);
      setActionMessage(`Cancelling order ${ticket}...`);
      await h("DELETE", `/orders/${ticket}`);
      setOrders((current) => current.filter((row) => String(row.id) !== String(ticket)));
      setApiError("");
      setActionMessage(`Cancel request accepted for order ${ticket}.`);
    } catch (error) {
      const message = describeApiError(error, "Failed to cancel order");
      setApiError(message);
      setActionMessage(`Cancel failed: ${message}`);
    } finally {
      setBusyAction("");
    }
  }

  const desktopLedgerTab = ["positions", "orders", "history"].includes(desktopTab) ? desktopTab : "positions";
  const showBottomPanel = false;


  function renderScannerWorkspace(mode = "desktop") {
    const compact = mode === "mobile";
    const rows = (audit?.scanner_rows?.length ? audit.scanner_rows : scannerRecentRows).slice(0, compact ? 10 : 18);
    const counts = audit?.counts || {};
    const reasonCounts = audit?.reason_counts || [];
    const tradeItems = [...(audit?.trades || [])].sort((left, right) => String(right.ts || "").localeCompare(String(left.ts || ""))).slice(0, compact ? 20 : 120);
    const blockItems = [...(audit?.blocks || [])].sort((left, right) => String(right.ts || "").localeCompare(String(left.ts || ""))).slice(0, compact ? 20 : 120);
    const marketClosed = Boolean(marketHours?.weekend_closed || audit?.market?.weekend_closed);
    const currentGate = marketClosed ? "MARKETS CLOSED" : (scannerState || "Scanning");
    return (
      <div className={"scanner-workspace" + (compact ? " mobile-scanner" : "")}>
        <div className="workspace-header scanner-header">
          <div><h3>Score Scan</h3><span>{audit?.score_source || "quality_v2"} - {audit?.window?.label || "Live decision audit"} - {compactCount(scannerSummary.tracked_symbols || liveSymbols.length)} MT5 symbols</span></div>
          <div className={"scanner-state " + (marketClosed ? "blocked" : Number(scannerSummary.current_actionable || 0) > 0 ? "active" : "")}><Activity size={16} /><strong>{currentGate}</strong></div>
        </div>
        {marketClosed && <div className="market-closed-inline"><Clock3 size={16} /><div><strong>MARKETS CLOSED - WEEKEND</strong><span>New scan decisions are blocked until the next broker session. The audit below remains available.</span></div></div>}
        <div className="scanner-metrics">
          <div className="scanner-card"><span>Score model</span><strong>quality_v2</strong><em>100-point diagnostic</em></div>
          <div className="scanner-card"><span>Current gate</span><strong>{currentGate}</strong><em>{audit?.market?.global_reason || "execution status"}</em></div>
          <div className="scanner-card"><span>Audit window</span><strong>{compactCount((audit?.blocks || []).length)}</strong><em>{compactCount((audit?.trades || []).length)} trade events</em></div>
          <div className="scanner-card"><span>Setups created</span><strong>{compactCount(counts.SETUP_CREATED || 0)}</strong><em>{compactCount(counts.SETUP_CONFIRMED_FRESH || 0)} fresh confirmations</em></div>
          <div className="scanner-card"><span>Orders filled</span><strong className={(counts.ORDER_FILLED || 0) > 0 ? "pos" : ""}>{compactCount(counts.ORDER_FILLED || 0)}</strong><em>{compactCount(counts.ORDER_SENT || 0)} sent - {compactCount(counts.ORDER_REJECTED || 0)} rejected</em></div>
          <div className="scanner-card"><span>Scan heartbeat</span><strong>{formatAgeLabel(status?.last_scan || scannerSummary.latest_signal_at)}</strong><em>{formatDateTimeLabel(status?.last_scan || scannerSummary.latest_signal_at) || "Audit live"}</em></div>
        </div>
        <div className="scanner-split">
          <div className="scanner-list">
            <div className="scanner-section-title"><strong>Current Score And Gate</strong><span>H4 / H1 / M15 / M5 / M1</span></div>
            {rows.length ? rows.map((row, index) => {
              const symbol = row.symbol || row.sym || "MT5";
              const id = symbol + "-" + (row.ts || index);
              const score = Number(row.score ?? row.score_metric?.total ?? 0);
              const statusLabel = marketClosed ? "WEEKEND CLOSED" : row.gate_ok ? "PASS" : (row.final_status || "NOT QUALIFIED");
              const reason = marketClosed ? "MARKETS_CLOSED_WEEKEND" : (row.reason || row.gate || row.status || "No block reason recorded");
              const context = "H4 " + (row.h4_bias || "-") + " " + (row.h4_score ?? "-") + " - H1 " + (row.h1_bias || "-") + " " + (row.h1_score ?? "-") + " - M15 " + (row.m15_bias || "-") + " " + (row.m15_score ?? "-") + " - M5 " + (row.m5_trigger_side || "-") + " - M1 " + (row.m1_status || "-");
              return (
                <React.Fragment key={id}>
                  <button type="button" className={"scanner-row" + (row.gate_ok && !marketClosed ? " ok" : " blocked")} onClick={() => setScannerExpanded((current) => current === id ? null : id)}>
                    <div className="scanner-row-main"><strong>{symbol} <span>{row.side || "WAIT"}</span></strong><em>{row.engine || "engine pending"} - {row.setup_type || "setup pending"}</em><small>{context}</small></div>
                    <div className="scanner-row-score"><strong>{score ? score.toFixed(0) : "--"}</strong><span>{row.score_band || "quality_v2"}</span><em className={row.gate_ok && !marketClosed ? "pos" : "neg"}>{statusLabel}</em></div>
                  </button>
                  {scannerExpanded === id && <div className="scanner-detail"><div className="scan-detail-grid"><span>H4 <b>{row.h4_bias || "-"} {row.h4_score ?? "-"}</b></span><span>H1 <b>{row.h1_bias || "-"} {row.h1_score ?? "-"}</b></span><span>M15 <b>{row.m15_bias || "-"} {row.m15_score ?? "-"}</b></span><span>M5 trigger <b>{row.m5_trigger_side || "-"}</b></span><span>M1 confirmation <b>{row.m1_status || "-"}</b></span><span>setup <b>{row.setup_type || "-"}</b></span></div><div className="scanner-detail-reason"><b>{statusLabel}</b> - {reason}</div>{Object.keys(row.score_metric?.components || {}).length > 0 && <div className="scanner-components">{Object.entries(row.score_metric.components).map(([key, value]) => <span key={key}>{key.replaceAll("_", " ")} <b>{Number(value).toFixed(1)}</b></span>)}</div>}</div>}
                </React.Fragment>
              );
            }) : <div className="empty-state">No score rows are available in the current audit window.</div>}
          </div>
          <div className="scanner-side">
            <div className="scanner-section-title"><strong>Block Reasons</strong><span>Audit window</span></div>
            <div className="scanner-blocks">{reasonCounts.length ? reasonCounts.slice(0, 10).map((row) => <div className="scanner-block" key={row.reason}><strong>{row.reason}</strong><span>{compactCount(row.count)} event(s)</span></div>) : <div className="empty-state">No recorded blocks.</div>}</div>
            <div className="scanner-section-title"><strong>Trades after 21:00 SAST</strong><span>{compactCount(tradeItems.length)} events shown</span></div>
            <div className="scanner-blocks audit-scroll">{tradeItems.length ? tradeItems.map((row, index) => <div className="scanner-block" key={row.event + "-" + row.ts + "-" + index}><strong>{row.symbol || "MT5"} <span>TRADE</span></strong><span>{row.event} - {row.reason || row.raw_status || "recorded"}</span><em>{formatDateTimeLabel(row.ts) || row.ts || "Live"}</em></div>) : <div className="empty-state">No trade events in the window.</div>}</div>
            <div className="scanner-section-title"><strong>Blocks and no-trade reasons</strong><span>{compactCount((audit?.blocks || []).length)} recorded</span></div>
            <div className="scanner-blocks audit-scroll">{blockItems.length ? blockItems.map((row, index) => <div className="scanner-block" key={row.event + "-" + row.ts + "-" + index}><strong>{row.symbol || "MT5"} <span>{row.reason_group || row.event}</span></strong><span>{row.reason || row.event}</span><em>{formatDateTimeLabel(row.ts) || row.ts || "Live"}</em></div>) : <div className="empty-state">No block events in the window.</div>}</div>
          </div>
        </div>
      </div>
    );
  }
  function renderMarketHoursWorkspace() {
    const market = marketHours || audit?.market || {};
    const sessions = market.sessions || [];
    return (
      <div className="market-hours-workspace">
        <div className="workspace-header"><div><h3>Market Hours</h3><span>Live session status - SAST</span></div><div className={"market-hours-state " + (market.weekend_closed ? "closed" : "open")}><span className="market-status-dot" />{market.weekend_closed ? "WEEKEND CLOSED" : ((market.global_status || "CLOSED") + " NOW")}</div></div>
        <div className={"market-closed-banner " + (market.weekend_closed ? "closed" : "open")}><Clock3 size={18} /><div><strong>{market.weekend_closed ? "MARKETS CLOSED - WEEKEND" : "MARKET SESSION STATUS"}</strong><span>{market.weekend_closed ? "No new MT5 trades are expected until the broker reopens. This is the current block reason." : "Session status is calculated from local exchange hours and the VPS clock."}</span></div><b>{market.global_reason || "LIVE"}</b></div>
        <div className="market-session-grid">{sessions.map((session) => <div className={"market-session-row " + (session.open ? "open" : "closed")} key={session.id}><span className="market-status-dot" /><div><strong>{session.label}</strong><em>{session.region}</em></div><span className="market-session-hours">{session.hours_sast}</span><span className="market-session-status">{session.open ? "OPEN" : "CLOSED"}<small>{session.open ? "live" : session.reason}</small></span><span className="market-next-open">Next: {session.next_open_sast}</span></div>)}</div>
        <div className="market-hours-foot">Current VPS time: {formatDateTimeLabel(market.now_sast)} - Schedule adjusts for regional daylight time and is displayed in SAST.</div>
      </div>
    );
  }


  function renderDeskUtilityPanel() {
    if (desktopTab === "calendar") {
      return (
        <div className="info-panel">
          <h3>Market Pulse</h3>
          <p>Live signal rotation and desk timing for {selected.symbol}.</p>
          <div className="info-list">
            {DESKTOP_EVENTS.map((event) => (
              <div className="info-row" key={event.label}>
                <strong>{event.time} · {event.tag}</strong>
                <span>{event.label}</span>
              </div>
            ))}
          </div>
          <div className="info-grid pulse-grid">
            {(marketPulse.length ? marketPulse : signalsFeed.slice(0, 6)).map((row) => (
              <button
                key={row.sym}
                className="info-card info-button"
                onClick={() => {
                  setSelectedSymbol(row.sym);
                  setDesktopTab("chart");
                }}
              >
                <strong>{row.sym}</strong>
                <span>{row.regime || "SCAN"} · RSI {Number(row.rsi || 0).toFixed(1)} · ATR {Number(row.atr || 0).toFixed(5)}</span>
                <span className={Number(row.chg || 0) >= 0 ? "pos" : "neg"}>{Number(row.chg || 0).toFixed(2)}%</span>
              </button>
            ))}
          </div>
        </div>
      );
    }
    if (desktopTab === "news") {
      return (
        <div className="info-panel">
          <h3>News</h3>
          <p>Live market headlines for {selected.symbol}.</p>
          <div className="info-list news-list">
            {newsFeed.length ? newsFeed.map((item, index) => (
              <a key={`${item.link || item.title}-${index}`} className="info-row news-link" href={item.link || "#"} target="_blank" rel="noreferrer">
                <strong>{item.publisher || "Market Feed"} <ExternalLink size={14} /></strong>
                <span>{item.title}</span>
                <span>{formatNewsTime(item.ts)}</span>
              </a>
            )) : <div className="empty-state">No live headlines available for this instrument right now.</div>}
          </div>
        </div>
      );
    }
    return null;
  }

  function renderTradeWorkspace(mode = "chart") {
    return (
      <div className={`chart-and-trade${mode === "watchlist" ? " watchlist-focus" : ""}${mode === "trade" ? " trade-focus" : ""}`}>
        <div className="chart-panel">
          <div className="order-banner">
            <button type="button" className="price-box sell" disabled={busyAction === "trade-sell"} onClick={() => submitTrade("sell")}>
              <span>SELL</span>
              <strong>{busyAction === "trade-sell" ? "Sending..." : fmtPrice(selected.symbol, selected.bid)}</strong>
            </button>
            <div className="volume-box">
              <span>Volume</span>
              <div>
                <button type="button" onClick={() => setVolume((v) => Math.max(0.01, Number((v - 0.01).toFixed(2))))}>-</button>
                <strong>{volume.toFixed(2)}</strong>
                <button type="button" onClick={() => setVolume((v) => Number((v + 0.01).toFixed(2)))}>+</button>
              </div>
            </div>
            <button type="button" className="price-box buy" disabled={busyAction === "trade-buy"} onClick={() => submitTrade("buy")}>
              <span>BUY</span>
              <strong>{busyAction === "trade-buy" ? "Sending..." : fmtPrice(selected.symbol, selected.ask)}</strong>
            </button>
          </div>
          <div className="chart-stage">
            <CandleChart
              sym={selected.symbol}
              token={token}
              timeframe={timeframe}
              candles={desktopCandles}
              height={420}
              fill
              apiBase={API}
            />
          </div>
          <div className="chart-readout">
            <div className="detail-card"><strong>Session</strong><span>{describeInstrument(selected.symbol, selected.group)}</span></div>
            <div className="detail-card"><strong>Low / High</strong><span>{fmtPrice(selected.symbol, selected.low)} / {fmtPrice(selected.symbol, selected.high)}</span></div>
            <div className="detail-card"><strong>Execution</strong><span>{status?.dry_run ? "DRY RUN" : "LIVE MT5"} · daily {status?.daily_trade_count ?? 0}/{status?.max_daily_trades || 30} · scan {status?.scan_seconds || 890}s</span></div>
            <div className="detail-card"><strong>Heartbeat</strong><span>{heartbeatLabel}</span></div>
          </div>
          <div className="timeframe-bar">
            {TIMEFRAMES.map((item) => (
              <button
                key={item}
                className={`tf-btn${timeframe === item ? " active" : ""}`}
                onClick={() => setTimeframe(item)}
              >
                {item}
              </button>
            ))}
          </div>
        </div>

        <div className="ticket-panel">
          <div className="ticket-header">
            <strong>{selected.symbol}</strong>
            <span className="subtle">One-click trading</span>
          </div>
          <label className="field">
            <span>Volume</span>
            <div className="field-stepper">
              <button type="button" onClick={() => setVolume((v) => Math.max(0.01, Number((v - 0.01).toFixed(2))))}>-</button>
              <input value={volume.toFixed(2)} readOnly />
              <button type="button" onClick={() => setVolume((v) => Number((v + 0.01).toFixed(2)))}>+</button>
            </div>
          </label>
          <label className="field">
            <span>Stop Loss</span>
            <input value={stopLoss} onChange={(event) => setStopLoss(event.target.value)} />
          </label>
          <label className="field">
            <span>Take Profit</span>
            <input value={takeProfit} onChange={(event) => setTakeProfit(event.target.value)} />
          </label>
          <div className="ticket-actions">
            <button type="button" className="cta sell" disabled={busyAction === "trade-sell"} onClick={() => submitTrade("sell")}>
              <span>SELL</span>
              <strong>{busyAction === "trade-sell" ? "Sending..." : fmtPrice(selected.symbol, selected.bid)}</strong>
            </button>
            <button type="button" className="cta buy" disabled={busyAction === "trade-buy"} onClick={() => submitTrade("buy")}>
              <span>BUY</span>
              <strong>{busyAction === "trade-buy" ? "Sending..." : fmtPrice(selected.symbol, selected.ask)}</strong>
            </button>
          </div>
          <button type="button" className="ghost-btn" disabled={busyAction === "pending"} onClick={submitPendingOrder}>
            {busyAction === "pending" ? "Sending..." : "Place Pending Order"}
          </button>
          <div className="ticket-footer">Spread: {selected.spread} · Equity Protection: ON · AI Trading: ACTIVE</div>
        </div>
      </div>
    );
  }

  function renderWatchlistWorkspace() {
    return (
      <div className="workspace-card">
        <div className="workspace-header">
          <h3>Market Watch</h3>
          <span>{activeWatchlist.length} active / {liveSymbols.length} available</span>
        </div>
        <div className="workspace-grid">
          <div className="workspace-stack">
            {panelSymbols.length ? panelSymbols.map((item) => (
              <button key={item.symbol} className={`workspace-row${item.symbol === selected.symbol ? " active" : ""}`} onClick={() => selectDesktopSymbol(item.symbol)}>
                <div>
                  <strong>{item.symbol}</strong>
                  <span>{item.group}</span>
                </div>
                <div className="workspace-quote">
                  <span>{fmtPrice(item.symbol, item.bid)}</span>
                  <span>{fmtPrice(item.symbol, item.ask)}</span>
                </div>
              </button>
            )) : <div className="empty-state">No symbols match the current market filter.</div>}
          </div>
          <div className="workspace-detail">
            <div className="detail-grid">
              <div className="detail-card"><strong>Bid</strong><span>{fmtPrice(selected.symbol, selected.bid)}</span></div>
              <div className="detail-card"><strong>Ask</strong><span>{fmtPrice(selected.symbol, selected.ask)}</span></div>
              <div className="detail-card"><strong>Spread</strong><span>{selected.spread}</span></div>
              <div className="detail-card"><strong>Status</strong><span>{activeSymbols.includes(selected.symbol) ? "Active" : "Hidden"}</span></div>
            </div>
            <div className="workspace-actions">
              <button className={`watch-toggle workspace-toggle${activeSymbols.includes(selected.symbol) ? " active" : ""}`} onClick={() => toggleSymbol(selected.symbol)}>
                {activeSymbols.includes(selected.symbol) ? "Remove from watchlist" : "Add to watchlist"}
              </button>
              <button className="ghost-btn" onClick={() => setDesktopTab("chart")}>Open chart</button>
              <button className="ghost-btn" onClick={() => setDesktopTab("trade")}>Open ticket</button>
            </div>
            <div className="workspace-chart">
              <CandleChart
                sym={selected.symbol}
                token={token}
                timeframe={timeframe}
                candles={desktopCandles}
                height={360}
                fill
                apiBase={API}
              />
            </div>
          </div>
        </div>
      </div>
    );
  }

  function renderPositionsWorkspace() {
    return (
      <div className="workspace-card positions-workspace">
        <div className="workspace-header">
          <div>
            <h3>Open Positions</h3>
            <span>{positions.length} live position(s) · tracking MT5 open P&L by symbol</span>
          </div>
          <strong className={totals.openPnl >= 0 ? "pnl-profit" : "pnl-loss"}>{formatSignedUsd(totals.openPnl)}</strong>
        </div>
        <div className="position-tracker-grid">
          <div className="position-metric">
            <span>Open P&L</span>
            <strong className={totals.openPnl >= 0 ? "pos" : "neg"}>{formatSignedUsd(totals.openPnl)}</strong>
            <em>{formatZar(totals.openPnl, usdZarRate, { signed: true })}</em>
          </div>
          <div className="position-metric">
            <span>Daily P&L</span>
            <strong className={mobileDailyPnl >= 0 ? "pos" : "neg"}>{formatSignedUsd(mobileDailyPnl)}</strong>
            <em>{formatZar(mobileDailyPnl, usdZarRate, { signed: true })}</em>
          </div>
          <div className="position-metric">
            <span>Winners / Losers</span>
            <strong>{positionTracker.winners} / {positionTracker.losers}</strong>
            <em>{positions.length} live tickets</em>
          </div>
          <div className="position-metric">
            <span>Equity</span>
            <strong>{formatUsd(totals.equity)}</strong>
            <em>free {formatUsd(totals.free)}</em>
          </div>
        </div>
        <div className="positions-split">
          <div className="position-pnl-panel">
            <div className="scanner-section-title">
              <strong>P&L By Symbol</strong>
              <span>{positionTracker.best?.symbol || "Waiting"} best · {positionTracker.worst?.symbol || "Waiting"} worst</span>
            </div>
            <div className="position-pnl-bars">
              {positionTracker.rows.length ? positionTracker.rows.map((row) => {
                const width = Math.max(8, (Math.abs(row.profit) / positionTracker.maxAbs) * 100);
                return (
                  <div className="position-pnl-row" key={row.symbol}>
                    <div>
                      <strong>{row.symbol}</strong>
                      <span>{row.count} trade{row.count === 1 ? "" : "s"} · vol {row.volume.toFixed(2)}</span>
                    </div>
                    <div className="position-pnl-track">
                      <div className={row.profit >= 0 ? "profit" : "loss"} style={{ width: `${width}%` }} />
                    </div>
                    <strong className={row.profit >= 0 ? "pos" : "neg"}>{formatSignedUsd(row.profit)}</strong>
                  </div>
                );
              }) : <div className="empty-state compact">No open P&L to track.</div>}
            </div>
          </div>
          <div className="workspace-stack positions-list">
            {positions.length ? positions.map((row) => (
              <div className="position-card" key={row.id}>
                <div><strong>{row.symbol}</strong><span>{row.side} {row.volume}</span></div>
                <div className="trade-date-line"><span>{tradeDateLine(row)}</span><span>{tradeMonthLabel(row) || row.openedDate}</span></div>
                <div><span>{row.open} → {row.current}</span><strong className={row.profit >= 0 ? "pos" : "neg"}>{formatSignedUsd(row.profit)}</strong></div>
                <button
                  type="button"
                  className="row-action danger"
                  disabled={busyAction === `close-${row.id}`}
                  onClick={() => closePosition(row.id)}
                >
                  {busyAction === `close-${row.id}` ? "Closing..." : "Close"}
                </button>
              </div>
            )) : <div className="empty-state">No live positions from MT5.</div>}
          </div>
        </div>
      </div>
    );
  }

  function renderOrdersWorkspace() {
    return (
      <div className="workspace-card">
        <div className="workspace-header">
          <h3>Pending Orders</h3>
          <span>{orders.length} order(s)</span>
        </div>
        <div className="workspace-stack">
          {orders.length ? orders.map((row) => (
            <div className="position-card" key={row.id}>
              <div><strong>{row.symbol}</strong><span>{row.type} {row.volume}</span></div>
              <div><span>Price {row.price}</span><strong>{row.status}</strong></div>
              <button
                type="button"
                className="row-action"
                disabled={busyAction === `cancel-${row.id}`}
                onClick={() => cancelOrder(row.id)}
              >
                {busyAction === `cancel-${row.id}` ? "Cancelling..." : "Cancel order"}
              </button>
            </div>
          )) : <div className="empty-state">No pending MT5 orders.</div>}
        </div>
      </div>
    );
  }

  function renderHistoryWorkspace() {
    return (
      <div className="workspace-card">
        <div className="workspace-header">
          <h3>Deals History</h3>
          <span>{deals.length} deal row(s)</span>
        </div>
        <div className="workspace-stack">
          {deals.length ? deals.map((row) => (
            <div className="position-card" key={row.id}>
              <div><strong>{row.symbol}</strong><span>{row.side} {row.volume}</span></div>
              <div className="trade-date-line"><span>{tradeDateLine(row, "history")}</span><span>{tradeMonthLabel(row) || row.tradeDate}</span></div>
              <div><span>{row.entry} → {row.exit}</span><strong className={row.pnl >= 0 ? "pos" : "neg"}>{row.pnl.toFixed(2)}</strong></div>
            </div>
          )) : <div className="empty-state">No deal history from MT5.</div>}
        </div>
      </div>
    );
  }

  const mobileHistoryRows = mobileHistoryTab === "positions"
    ? positions
    : mobileHistoryTab === "orders"
      ? orders
      : deals;

  return (
    <div className="mt5-shell">
      {pendingClose && (
        <div className="mt5-confirm-backdrop" role="presentation">
          <div className="mt5-confirm-card" role="dialog" aria-modal="true" aria-labelledby="mt5-close-title">
            <div>
              <span className="confirm-kicker">Live MT5 action</span>
              <strong id="mt5-close-title">Close {pendingClose.label}?</strong>
              <p>
                Current floating P&amp;L{" "}
                <span className={pendingClose.profit >= 0 ? "pos" : "neg"}>
                  {formatSignedUsd(pendingClose.profit)}
                </span>
              </p>
            </div>
            <div className="mt5-confirm-actions">
              <button type="button" className="ghost-btn" onClick={() => setPendingClose(null)}>
                Keep open
              </button>
              <button type="button" className="row-action danger" disabled={Boolean(busyAction)} onClick={confirmClosePosition}>
                Close position
              </button>
            </div>
          </div>
        </div>
      )}
      <div className="desktop-layout">
        <header className="desktop-topbar">
          <div className="brand-block">
            <img src={logoSrc} alt="Cipher FX" className="brand-logo" />
          </div>
          <div className="topbar-tools">
            <button className="toolbar-btn" title="Open chart" aria-label="Open chart" onClick={() => setDesktopTab("chart")}><BarChart3 size={16} /></button>
            <button className="toolbar-btn" title="Open market watch" aria-label="Open market watch" onClick={() => { setWatchlistOpen(true); setDesktopTab("watchlist"); }}><Globe size={16} /></button>
            <button className="toolbar-btn active" title="Cycle timeframe" onClick={cycleDesktopTimeframe}>{timeframe}</button>
            <button className="toolbar-btn" title="Open trade ticket" aria-label="Open trade ticket" onClick={() => setDesktopTab("trade")}><Grip size={16} /></button>
            <button className="toolbar-btn" title="Place pending order" aria-label="Place pending order" disabled={busyAction === "pending"} onClick={submitPendingOrder}><Plus size={16} /></button>
            <button className="toolbar-btn" title="Open calendar" aria-label="Open calendar" onClick={() => setDesktopTab("calendar")}><Calendar size={16} /></button>
            <button className="toolbar-btn" title="Open orders" aria-label="Open orders" onClick={() => setDesktopTab("orders")}><FileText size={16} /></button>
          </div>
          <div className="account-strip">
            <span className="live-chip">{status?.dry_run ? "DRY" : status?.mt5_connected ? "LIVE" : "SYNC"}</span>
            <div>
              <div>Account: {status?.account_id || terminal?.account?.login || "--"}</div>
              <strong>Balance: {formatUsd(terminal?.account?.balance ?? totals.balance)} <span className="zar-hint">{formatZar(terminal?.account?.balance ?? totals.balance, usdZarRate, { compact: true })}</span></strong>
              <div className="subtle">{accountCurrency} account · {usdZarLabel}</div>
            </div>
          </div>
        </header>

        <div className={`desktop-main${watchlistOpen ? "" : " watchlist-collapsed"}`}>
          <aside className="desktop-sidebar">
            {DESKTOP_TABS.map((tab) => {
              const Icon = tab.icon;
              return (
                <button
                  key={tab.id}
                  className={`side-tab${desktopTab === tab.id ? " active" : ""}`}
                  onClick={() => setDesktopTab(tab.id)}
                >
                  <Icon size={18} />
                  <span>{tab.label}</span>
                </button>
              );
            })}
          </aside>

          <section className="center-column">
            <div className="symbol-header">
              <div className="symbol-meta">
                <div className="picker-anchor">
                  <button
                    className="symbol-chip"
                    aria-expanded={symbolPickerOpen}
                    title="Choose symbol"
                    onClick={() => {
                      setSymbolPickerOpen((current) => !current);
                      setTimeframePickerOpen(false);
                    }}
                  >
                    {selected.symbol} <ChevronDown size={14} />
                  </button>
                  {symbolPickerOpen && (
                    <div className="picker-menu symbol-picker-menu">
                      <div className="picker-search">
                        <Search size={15} />
                        <input
                          aria-label="Search desktop symbols"
                          placeholder="Search symbol"
                          value={quickSearchTerm}
                          onChange={(event) => setQuickSearchTerm(event.target.value)}
                        />
                        {quickSearchTerm && (
                          <button className="search-clear" title="Clear search" onClick={() => setQuickSearchTerm("")}>
                            <X size={14} />
                          </button>
                        )}
                      </div>
                      <div className="group-filter-row compact">
                        {SYMBOL_GROUP_NAMES.map((group) => (
                          <button
                            key={group}
                            className={`group-chip${quickGroupFilter === group ? " active" : ""}`}
                            onClick={() => setQuickGroupFilter(group)}
                          >
                            {group}
                          </button>
                        ))}
                      </div>
                      <div className="picker-list">
                        {quickSymbols.length ? quickSymbols.slice(0, 120).map((item) => (
                          <button
                            key={item.symbol}
                            className={`picker-row${item.symbol === selected.symbol ? " active" : ""}`}
                            onClick={() => selectDesktopSymbol(item.symbol)}
                          >
                            <span><strong>{item.symbol}</strong><small>{groupBadge(item.group)}</small></span>
                            <span>{fmtPrice(item.symbol, item.bid)} / {fmtPrice(item.symbol, item.ask)}</span>
                          </button>
                        )) : <div className="empty-state compact">No symbols match this search.</div>}
                      </div>
                      <div className="picker-footer">{quickSymbols.length} symbol(s)</div>
                    </div>
                  )}
                </div>
                <div className="picker-anchor">
                  <button
                    className="symbol-chip active"
                    aria-expanded={timeframePickerOpen}
                    title="Choose timeframe"
                    onClick={() => {
                      setTimeframePickerOpen((current) => !current);
                      setSymbolPickerOpen(false);
                    }}
                  >
                    {timeframe} <ChevronDown size={14} />
                  </button>
                  {timeframePickerOpen && (
                    <div className="picker-menu timeframe-picker-menu">
                      {TIMEFRAMES.map((item) => (
                        <button
                          key={item}
                          className={`picker-row timeframe-row${timeframe === item ? " active" : ""}`}
                          onClick={() => selectDesktopTimeframe(item)}
                        >
                          <span><strong>{item}</strong><small>MT5 candle interval</small></span>
                        </button>
                      ))}
                    </div>
                  )}
                </div>
                <div>
                  <strong>{describeInstrument(selected.symbol, selected.group)}</strong>
                  <div className="subtle">{selected.group} · {selected.resolvedSymbol && selected.resolvedSymbol !== selected.symbol ? `${selected.resolvedSymbol} · ` : ""}Spread {selected.spread}</div>
                </div>
              </div>
              <div className="quick-stats">
                <span>Bid {fmtPrice(selected.symbol, selected.bid)}</span>
                <span>Ask {fmtPrice(selected.symbol, selected.ask)}</span>
                <span className={selected.change >= 0 ? "pos" : "neg"}>{selected.change.toFixed(2)}%</span>
              </div>
            </div>
            <div className="mt5-status-line">
              <span>{status?.mt5_connected ? "MT5 CONNECTED" : "MT5 OFFLINE"}</span>
              <span>{status?.server || terminal?.account?.server || "XMGlobal-MT5"}</span>
              <span className={apiError ? "status-error" : actionMessage ? "status-action" : ""}>{apiError || actionMessage || `Heartbeat ${heartbeatLabel}`}</span>
            </div>

            <div className="center-workspace">
              {desktopTab === "watchlist" && renderWatchlistWorkspace()}
              {desktopTab === "chart" && renderTradeWorkspace("chart")}
              {desktopTab === "scanner" && renderScannerWorkspace("desktop")}
              {desktopTab === "market-hours" && renderMarketHoursWorkspace()}
              {desktopTab === "trade" && renderTradeWorkspace("trade")}
              {desktopTab === "positions" && renderPositionsWorkspace()}
              {desktopTab === "orders" && renderOrdersWorkspace()}
              {desktopTab === "history" && renderHistoryWorkspace()}
              {["calendar", "news"].includes(desktopTab) && renderDeskUtilityPanel()}
            </div>

            {showBottomPanel && (
              <div className="bottom-panel">
                <div className="bottom-tabs">
                  {["positions", "orders", "history"].map((tab) => (
                    <button
                      key={tab}
                      className={`bottom-tab${desktopLedgerTab === tab ? " active" : ""}`}
                      onClick={() => setDesktopTab(tab)}
                    >
                      {tab}
                    </button>
                  ))}
                </div>
                <div className="ledger-header">
                  <span>Symbol</span>
                  <span>Ticket / Type</span>
                  <span>Time / Volume</span>
                  <span>Price</span>
                  <span>Details</span>
                  <span>P&L</span>
                </div>
                <div className="ledger-body">
                  {desktopLedgerTab === "orders" && renderLedgerRows(orders, "orders")}
                  {desktopLedgerTab === "history" && renderLedgerRows(deals, "history")}
                  {desktopLedgerTab === "positions" && renderLedgerRows(positions, "positions")}
                </div>
                <div className="ledger-summary">
                  Balance {formatUsd(totals.balance)} <span className="zar-hint">{formatZar(totals.balance, usdZarRate, { compact: true })}</span> · Equity {formatUsd(totals.equity)} · Margin {formatUsd(totals.margin)} · Free {formatUsd(totals.free)} · Open P&L
                  <span className={totals.openPnl >= 0 ? "pos" : "neg"}> {formatSignedUsd(totals.openPnl)}</span>
                  <span className="zar-hint"> {formatZar(totals.openPnl, usdZarRate, { signed: true })}</span>
                </div>
              </div>
            )}
          </section>

          <aside className="watchlist-panel">
            <div className="watchlist-head">
              <strong>Watchlist</strong>
              <div className="watch-actions">
                <button title="Toggle watchlist/all symbols" aria-label="Toggle watchlist/all symbols" onClick={() => setWatchlistMode((current) => (current === "active" ? "market" : "active"))}><Plus size={15} /></button>
                <button title="Collapse watchlist panel" aria-label="Collapse watchlist panel" onClick={() => setWatchlistOpen(false)}><PanelLeft size={15} /></button>
              </div>
            </div>
            <div className="watchlist-modes">
              <button className={`mode-btn${watchlistMode === "active" ? " active" : ""}`} onClick={() => setWatchlistMode("active")}>Watchlist</button>
              <button className={`mode-btn${watchlistMode === "market" ? " active" : ""}`} onClick={() => setWatchlistMode("market")}>All symbols</button>
            </div>
            <label className="search-box">
              <Search size={15} />
              <input
                aria-label="Search watchlist symbols"
                placeholder="Search symbol"
                value={searchTerm}
                onChange={(event) => setSearchTerm(event.target.value)}
              />
              {searchTerm && (
                <button className="search-clear" title="Clear search" onClick={() => setSearchTerm("")}>
                  <X size={14} />
                </button>
              )}
            </label>
            <div className="group-filter-row">
              {SYMBOL_GROUP_NAMES.map((group) => (
                <button
                  key={group}
                  className={`group-chip${symbolGroupFilter === group ? " active" : ""}`}
                  onClick={() => setSymbolGroupFilter(group)}
                >
                  {group}
                </button>
              ))}
            </div>
            <div className="table-head">
              <span>Symbol</span>
              <span>Bid</span>
              <span>Ask</span>
              <span>Chg %</span>
              <span>Status</span>
            </div>
            <WatchlistTable
              symbols={panelSymbols}
              activeSymbols={activeSymbols}
              selectedSymbol={selectedSymbol}
              onSelect={selectDesktopSymbol}
              onToggle={toggleSymbol}
            />
            <div className="watchlist-foot">
              <div className="universe-stat">
                <strong>{activeSymbols.length}</strong>
                <span>Active symbols</span>
              </div>
              <div className="universe-stat">
                <strong>{liveSymbols.length}</strong>
                <span>Total available</span>
              </div>
              <div className="universe-stat">
                <strong>{scannerSummary.current_actionable ?? 0}</strong>
                <span>Signals now</span>
              </div>
            </div>
          </aside>
        </div>

        <footer className="desktop-footer">
          <span>Server: CipherFX-Live</span>
          <span>Ping: 12.4 ms</span>
          <span>SAST time: {serverClockLabel}</span>
          <span className="pos">Equity Protection: ON</span>
          <span className="pos">AI Trading: ACTIVE</span>
        </footer>
      </div>

      <div className="mobile-layout">
        <div className="phone-frame">
          <div className={`mobile-pl-strip ${mobilePnlClass}`} aria-label={`USD profit and loss ${formatSignedUsd(mobileDailyPnl)}`}>
            <div className="mobile-pl-main">
              <span>USD P/L</span>
              <strong>{formatSignedUsd(mobileDailyPnl)}</strong>
              <em>{formatZar(mobileDailyPnl, usdZarRate, { signed: true })}</em>
            </div>
            <div className="mobile-pl-metric">
              <span>Open</span>
              <strong className={totals.openPnl >= 0 ? "pnl-profit" : "pnl-loss"}>{formatSignedUsd(totals.openPnl)}</strong>
              <em>{formatZar(totals.openPnl, usdZarRate, { signed: true })}</em>
            </div>
            <div className="mobile-pl-metric">
              <span>Equity</span>
              <strong>{formatUsd(totals.equity)}</strong>
              <em>{formatZar(totals.equity, usdZarRate, { compact: true })}</em>
            </div>
          </div>

          {mobileTab === "quotes" && (
            <div className="mobile-screen">
              <div className="mobile-brand-row">
                <span className="mobile-brand-spacer" aria-hidden="true" />
                <img src={logoSrc} alt="" className="mobile-header-logo" aria-hidden="true" />
                <button className="icon-btn" title="Open trade ticket" aria-label="Open trade ticket" onClick={() => setMobileTab("trade")}><Plus size={18} /></button>
              </div>
              <div className="mobile-mode-row">
                <button className={`mode-btn${watchlistMode === "active" ? " active" : ""}`} onClick={() => setWatchlistMode("active")}>Watchlist</button>
                <button className={`mode-btn${watchlistMode === "market" ? " active" : ""}`} onClick={() => setWatchlistMode("market")}>All</button>
              </div>
              <div className="mobile-mode-row">
                <button className={`mode-btn${mobileQuotesMode === "simple" ? " active" : ""}`} onClick={() => setMobileQuotesMode("simple")}>Simple</button>
                <button className={`mode-btn${mobileQuotesMode === "advanced" ? " active" : ""}`} onClick={() => setMobileQuotesMode("advanced")}>Advanced</button>
              </div>
              <label className="search-box mobile">
                <Search size={16} />
                <input
                  aria-label="Search mobile symbols"
                  placeholder="Search mobile symbols"
                  value={mobileSearchTerm}
                  onChange={(event) => setMobileSearchTerm(event.target.value)}
                />
                {mobileSearchTerm && (
                  <button
                    className="search-clear"
                    title="Clear mobile symbol search"
                    aria-label="Clear mobile symbol search"
                    onClick={() => setMobileSearchTerm("")}
                  >
                    <X size={14} />
                  </button>
                )}
              </label>
              <div className="group-filter-row mobile-groups">
                {SYMBOL_GROUP_NAMES.map((group) => (
                  <button
                    key={group}
                    className={`group-chip${mobileGroupFilter === group ? " active" : ""}`}
                    onClick={() => setMobileGroupFilter(group)}
                  >
                    {group}
                  </button>
                ))}
              </div>
              <div className="mobile-quote-head">
                <span>Symbol</span>
                <span>Bid</span>
                <span>Ask</span>
                <span>Chg %</span>
              </div>
              <div className="mobile-quotes">
                {mobilePanelSymbols.length ? mobilePanelSymbols.map((item) => (
                  <div
                    key={item.symbol}
                    className="mobile-quote-row"
                    role="button"
                    tabIndex={0}
                    onClick={() => selectMobileSymbol(item.symbol)}
                    onKeyDown={(event) => {
                      if (event.key === "Enter" || event.key === " ") {
                        event.preventDefault();
                        selectMobileSymbol(item.symbol);
                      }
                    }}
                  >
                    <div>
                      <div className="mobile-symbol-line">
                        <span className={`triangle ${item.change >= 0 ? "up" : "down"}`} />
                        <strong>{item.symbol}</strong>
                        <button
                          type="button"
                          className={`watch-toggle mobile-toggle${activeSymbols.includes(item.symbol) ? " active" : ""}`}
                          onClick={(event) => {
                            event.stopPropagation();
                            toggleSymbol(item.symbol);
                          }}
                        >
                          {activeSymbols.includes(item.symbol) ? "On desk" : "Add"}
                        </button>
                      </div>
                      <div className="mobile-sub">{quoteClock} · Spread: {item.spread}</div>
                      {mobileQuotesMode === "advanced" && (
                        <div className="mobile-sub">Low: {fmtPrice(item.symbol, item.low)} · High: {fmtPrice(item.symbol, item.high)}</div>
                      )}
                    </div>
                    <div className="mobile-prices">
                      <span>{fmtPrice(item.symbol, item.bid)}</span>
                      <span>{fmtPrice(item.symbol, item.ask)}</span>
                      <span className={item.change >= 0 ? "pos" : "neg"}>{Math.abs(item.change).toFixed(2)}%</span>
                    </div>
                  </div>
                )) : <div className="empty-state">No symbols match this search.</div>}
              </div>
            </div>
          )}

          {mobileTab === "chart" && (
            <div className="mobile-screen">
              <div className="mobile-brand-row">
                <span className="mobile-brand-spacer" aria-hidden="true" />
                <img src={logoSrc} alt="" className="mobile-header-logo" aria-hidden="true" />
                <div className="phone-actions">
                  <button className="icon-btn" title="Open history" aria-label="Open history" onClick={() => setMobileTab("history")}><Clock3 size={17} /></button>
                  <button className="icon-btn" title="Open quotes" aria-label="Open quotes" onClick={() => setMobileTab("quotes")}><Globe size={17} /></button>
                </div>
              </div>
              <div className="mobile-chart-head">
                <div>
                  <strong>{selected.symbol}</strong>
                  <div className="subtle">{selected.resolvedSymbol || selected.symbol} · {mobileTimeframe}</div>
                </div>
                <div className="phone-actions">
                  <button className="icon-btn" title="Search symbols" aria-label="Search symbols" onClick={() => setMobileTab("quotes")}><Search size={16} /></button>
                  <button className="icon-btn" title="Open trade ticket" aria-label="Open trade ticket" onClick={() => setMobileTab("trade")}><MonitorSmartphone size={16} /></button>
                </div>
              </div>
              <div className="mobile-ticket-row">
                <button type="button" className="price-box sell" disabled={busyAction === "trade-sell"} onClick={() => submitTrade("sell")}>
                  <span>SELL</span>
                  <strong>{busyAction === "trade-sell" ? "Sending..." : fmtPrice(selected.symbol, selected.bid)}</strong>
                </button>
                <div className="volume-box compact">
                  <button type="button" onClick={() => setVolume((v) => Math.max(0.01, Number((v - 0.01).toFixed(2))))}>-</button>
                  <strong>{volume.toFixed(2)}</strong>
                  <button type="button" onClick={() => setVolume((v) => Number((v + 0.01).toFixed(2)))}>+</button>
                </div>
                <button type="button" className="price-box buy" disabled={busyAction === "trade-buy"} onClick={() => submitTrade("buy")}>
                  <span>BUY</span>
                  <strong>{busyAction === "trade-buy" ? "Sending..." : fmtPrice(selected.symbol, selected.ask)}</strong>
                </button>
              </div>
              <div className="mobile-chart-wrap">
                <CandleChart
                  sym={selected.symbol}
                  token={token}
                  timeframe={mobileTimeframe}
                  candles={mobileCandles}
                  height={304}
                  apiBase={API}
                />
              </div>
              <div className="mobile-timeframes">
                {TIMEFRAMES.map((item) => (
                  <button
                    key={item}
                    className={`tf-btn${mobileTimeframe === item ? " active" : ""}`}
                    onClick={() => setMobileTimeframe(item)}
                  >
                    {item}
                  </button>
                ))}
              </div>
            </div>
          )}

          {mobileTab === "scanner" && (
            <div className="mobile-screen scanner-screen">
              <div className="mobile-brand-row">
                <span className="mobile-brand-spacer" aria-hidden="true" />
                <img src={logoSrc} alt="" className="mobile-header-logo" aria-hidden="true" />
                <div className="phone-actions">
                  <button className="icon-btn" title="Open chart" aria-label="Open chart" onClick={() => setMobileTab("chart")}><BarChart3 size={17} /></button>
                  <button className="icon-btn" title="Open quotes" aria-label="Open quotes" onClick={() => setMobileTab("quotes")}><Search size={17} /></button>
                </div>
              </div>
              {renderScannerWorkspace("mobile")}
            </div>
          )}

          {mobileTab === "trade" && (
            <div className="mobile-screen">
              <div className="mobile-brand-row">
                <span className="mobile-brand-spacer" aria-hidden="true" />
                <img src={logoSrc} alt="" className="mobile-header-logo" aria-hidden="true" />
                <button className="icon-btn" title="Add pending order" aria-label="Add pending order" disabled={busyAction === "pending"} onClick={submitPendingOrder}><Wallet size={18} /></button>
              </div>
              <div className="mobile-trade-card">
                <strong>{selected.symbol}</strong>
                <span>Advanced trade ticket</span>
                <label className="field">
                  <span>Volume</span>
                  <div className="field-stepper">
                    <button type="button" onClick={() => setVolume((v) => Math.max(0.01, Number((v - 0.01).toFixed(2))))}>-</button>
                    <input value={volume.toFixed(2)} readOnly />
                    <button type="button" onClick={() => setVolume((v) => Number((v + 0.01).toFixed(2)))}>+</button>
                  </div>
                </label>
                <label className="field">
                  <span>Stop Loss</span>
                  <input value={stopLoss} onChange={(event) => setStopLoss(event.target.value)} />
                </label>
                <label className="field">
                  <span>Take Profit</span>
                  <input value={takeProfit} onChange={(event) => setTakeProfit(event.target.value)} />
                </label>
                <div className="ticket-actions">
                  <button type="button" className="cta sell" disabled={busyAction === "trade-sell"} onClick={() => submitTrade("sell")}>{busyAction === "trade-sell" ? "Sending..." : "Sell"}</button>
                  <button type="button" className="cta buy" disabled={busyAction === "trade-buy"} onClick={() => submitTrade("buy")}>{busyAction === "trade-buy" ? "Sending..." : "Buy"}</button>
                </div>
                <button type="button" className="ghost-btn" disabled={busyAction === "pending"} onClick={submitPendingOrder}>
                  {busyAction === "pending" ? "Sending..." : "Add pending order"}
                </button>
              </div>
              <div className="mobile-open-items">
                <strong>Open Positions</strong>
                {positions.length ? positions.slice(0, 8).map((row) => (
                  <div className="mobile-history-row" key={row.id}>
                    <div>
                      <strong>{row.symbol} <span className={row.side === "buy" ? "pos" : "neg"}>{row.side} {row.volume}</span></strong>
                      <div className="mobile-sub">{row.open} → {row.current}</div>
                      <div className="mobile-sub">{tradeDateLine(row)}</div>
                    </div>
                    <div className="mobile-row-actions">
                      <span className={row.profit >= 0 ? "pos" : "neg"}>{row.profit.toFixed(2)}</span>
                      <button
                        type="button"
                        className="row-action danger"
                        disabled={busyAction === `close-${row.id}`}
                        onClick={() => closePosition(row.id)}
                      >
                        {busyAction === `close-${row.id}` ? "Closing..." : "Close"}
                      </button>
                    </div>
                  </div>
                )) : <div className="empty-state">No open MT5 positions.</div>}
              </div>
            </div>
          )}

          {mobileTab === "history" && (
            <div className="mobile-screen">
              <div className="mobile-brand-row">
                <span className="mobile-brand-spacer" aria-hidden="true" />
                <img src={logoSrc} alt="" className="mobile-header-logo" aria-hidden="true" />
                <button className="icon-btn" title="Show deals" aria-label="Show deals" onClick={() => setMobileHistoryTab("deals")}><History size={18} /></button>
              </div>
              <div className="mobile-history-tabs">
                <button className={`mode-btn${mobileHistoryTab === "positions" ? " active" : ""}`} onClick={() => setMobileHistoryTab("positions")}>Positions</button>
                <button className={`mode-btn${mobileHistoryTab === "orders" ? " active" : ""}`} onClick={() => setMobileHistoryTab("orders")}>Orders</button>
                <button className={`mode-btn${mobileHistoryTab === "deals" ? " active" : ""}`} onClick={() => setMobileHistoryTab("deals")}>Deals</button>
              </div>
              <div className="mobile-history-list">
                {mobileHistoryRows.length ? mobileHistoryRows.map((row) => (
                  <div className="mobile-history-row" key={row.id}>
                    <div>
                      <strong>{row.symbol} <span className={(row.side || row.type || "").toString().toLowerCase().includes("buy") ? "pos" : "neg"}>{row.side || row.type} {row.volume}</span></strong>
                      <div className="mobile-sub">{row.entry != null ? `${row.entry} → ${row.exit}` : `${row.open} → ${row.current ?? row.price}`}</div>
                      <div className="mobile-sub">{tradeDateLine(row, mobileHistoryTab === "deals" ? "history" : mobileHistoryTab)}</div>
                      <div className="mobile-sub">{tradeMonthLabel(row) || row.tradeDate || row.openedDate || `Ticket ${row.id}`}</div>
                    </div>
                    <div className="mobile-row-actions">
                      <span className={Number(row.pnl ?? row.profit ?? 0) >= 0 ? "pos" : "neg"}>{Number(row.pnl ?? row.profit ?? 0).toFixed(2)}</span>
                      {mobileHistoryTab === "positions" && (
                        <button
                          type="button"
                          className="row-action danger"
                          disabled={busyAction === `close-${row.id}`}
                          onClick={() => closePosition(row.id)}
                        >
                          {busyAction === `close-${row.id}` ? "Closing..." : "Close"}
                        </button>
                      )}
                      {mobileHistoryTab === "orders" && (
                        <button
                          type="button"
                          className="row-action"
                          disabled={busyAction === `cancel-${row.id}`}
                          onClick={() => cancelOrder(row.id)}
                        >
                          {busyAction === `cancel-${row.id}` ? "Cancelling..." : "Cancel"}
                        </button>
                      )}
                    </div>
                  </div>
                )) : <div className="empty-state">No {mobileHistoryTab} rows from MT5.</div>}
              </div>
              <div className="mobile-summary">
                <div><span>Balance</span><strong>{formatUsd(totals.balance)}</strong><em>{formatZar(totals.balance, usdZarRate, { compact: true })}</em></div>
                <div><span>Equity</span><strong className={totals.equity >= totals.balance ? "pos" : "neg"}>{formatUsd(totals.equity)}</strong><em>{formatZar(totals.equity, usdZarRate, { compact: true })}</em></div>
                <div><span>Margin</span><strong>{formatUsd(totals.margin)}</strong><em>{formatZar(totals.margin, usdZarRate, { compact: true })}</em></div>
                <div><span>Free</span><strong className="gold">{formatUsd(totals.free)}</strong><em>{formatZar(totals.free, usdZarRate, { compact: true })}</em></div>
              </div>
            </div>
          )}

          <div className="mobile-nav">
            {MOBILE_TABS.map((tab) => {
              const Icon = tab.icon;
              return (
                <button
                  key={tab.id}
                  className={`mobile-nav-btn${mobileTab === tab.id ? " active" : ""}`}
                  title={tab.label}
                  aria-label={`Open ${tab.label}`}
                  onClick={() => setMobileTab(tab.id)}
                >
                  <Icon size={18} />
                  <span>{tab.label}</span>
                </button>
              );
            })}
          </div>
        </div>
      </div>
    </div>
  );
}

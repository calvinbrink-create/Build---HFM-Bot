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
  Pause,
  Play,
  Plus,
  Search,
  SkipBack,
  StepForward,
  Sparkles,
  TrendingDown,
  TrendingUp,
  Wallet,
  X,
} from "lucide-react";
import logoSrc from "./cipherfx-icon.png";
import CandleChart from "./CandleChart";
import TradeReplayChart from "./TradeReplayChart";
import { DEFAULT_ACTIVE_SYMBOLS } from "./mt5Universe";

const API = `${window.location.origin}/mt5-api`;

function volatileDashboardStorage() {
  try {
    if (!window.__cipherfxDashboardStorage) {
      window.__cipherfxDashboardStorage = Object.create(null);
    }
    return window.__cipherfxDashboardStorage;
  } catch {
    return Object.create(null);
  }
}

function safeStorageGet(key) {
  const memory = volatileDashboardStorage();
  try {
    const value = window.localStorage?.getItem(key);
    if (value) memory[key] = value;
    return value || memory[key] || "";
  } catch {
    return memory[key] || "";
  }
}

function safeStorageRemove(key) {
  delete volatileDashboardStorage()[key];
  try {
    window.localStorage?.removeItem(key);
  } catch {
    // Storage failure must not prevent API auth recovery.
  }
}

const DESKTOP_TABS = [
  { id: "market-hours", label: "Market Hours", icon: Clock3 },
  { id: "watchlist", label: "Watchlist", icon: List },
  { id: "chart", label: "Chart", icon: LineChart },
  { id: "replay", label: "Replay", icon: History },
  { id: "scanner", label: "Scanner", icon: Activity },
  { id: "intelligence", label: "Intelligence", icon: Sparkles },
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
  { id: "replay", label: "Replay", icon: History },
  { id: "scanner", label: "Scan", icon: Activity },
  { id: "intelligence", label: "Intel", icon: Sparkles },
  { id: "trade", label: "Trade", icon: Sparkles },
  { id: "history", label: "History", icon: Clock3 },
];

const TIMEFRAMES = ["M1", "M5", "M15", "H1", "H4", "D1"];
const SYMBOL_GROUP_NAMES = ["All", "Forex", "Metals", "CFDs", "Crypto"];
const DISPLAY_TIME_ZONE = "Africa/Johannesburg";
const DISPLAY_TZ_LABEL = "SAST";


function fmtPrice(symbol, value) {
  const numeric = Number(value);
  if (!Number.isFinite(numeric)) return "--";
  const digits = String(symbol || "").includes("JPY") ? 3 : numeric > 1000 ? 2 : numeric > 10 ? 3 : 5;
  return numeric.toFixed(digits);
}

function liveNumber(value) {
  const numeric = Number(value);
  return Number.isFinite(numeric) ? numeric : null;
}

function fmtNumber(value, decimals = 2, fallback = "--") {
  if (value === null || value === undefined || value === "") return fallback;
  const numeric = Number(value);
  return Number.isFinite(numeric) ? numeric.toFixed(decimals) : fallback;
}

function fmtAbsNumber(value, decimals = 2, fallback = "--") {
  if (value === null || value === undefined || value === "") return fallback;
  const numeric = Number(value);
  return Number.isFinite(numeric) ? Math.abs(numeric).toFixed(decimals) : fallback;
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
    return new Date(Number(value) * 1000).toLocaleString("en-US", {
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
    return parseDateValue(value).toLocaleTimeString("en-US", {
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
  return parsed.toLocaleString("en-US", {
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

const FRAME_SECONDS = {
  M1: 60,
  M5: 300,
  M15: 900,
  M30: 1800,
  H1: 3600,
  H4: 14400,
  D1: 86400,
};

function applyLiveQuoteToCandles(candles, timeframe, quote) {
  if (!Array.isArray(candles) || !candles.length) return candles;
  const bid = Number(quote?.bid);
  const ask = Number(quote?.ask);
  const price = Number.isFinite(bid) && Number.isFinite(ask)
    ? (bid + ask) / 2
    : Number.isFinite(bid) ? bid : Number.isFinite(ask) ? ask : null;
  if (!Number.isFinite(price)) return candles;
  const step = FRAME_SECONDS[String(timeframe || "M15").toUpperCase()] || 900;
  const tickDate = parseDateValue(quote?.tickTime);
  const referenceSeconds = tickDate ? Math.floor(tickDate.getTime() / 1000) : Math.floor(Date.now() / 1000);
  const bucketSeconds = Math.floor(referenceSeconds / step) * step;
  const last = candles[candles.length - 1];
  const lastDate = parseDateValue(last?.ts || last?.time);
  const lastSeconds = lastDate ? Math.floor(lastDate.getTime() / 1000) : 0;
  const rows = candles.slice();
  if (lastSeconds >= bucketSeconds) {
    const updated = {
      ...last,
      close: price,
      high: Math.max(Number(last.high) || price, price),
      low: Math.min(Number(last.low) || price, price),
      source: "mt5_bridge_tick_overlay",
      tick_time: quote?.tickTime || "",
      quote_age_seconds: quote?.quoteAgeSeconds ?? null,
    };
    rows[rows.length - 1] = updated;
    return rows;
  }
  const open = Number(last.close);
  if (!Number.isFinite(open)) return rows;
  rows.push({
    ts: new Date(bucketSeconds * 1000).toISOString(),
    time_label: "",
    time_zone: "SAST",
    open,
    high: Math.max(open, price),
    low: Math.min(open, price),
    close: price,
    volume: 0,
    source: "mt5_bridge_tick_overlay",
    tick_time: quote?.tickTime || "",
    quote_age_seconds: quote?.quoteAgeSeconds ?? null,
  });
  return rows;
}

function compactCount(value) {
  const numeric = Number(value || 0);
  return numeric.toLocaleString("en-US", { maximumFractionDigits: 0 });
}

function formatMonthLabel(value) {
  const parsed = parseDateValue(value);
  if (!parsed) return "";
  return parsed.toLocaleString("en-US", {
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
  const absolute = Math.abs(numeric).toLocaleString("en-US", {
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
  return `$${numeric.toLocaleString("en-US", {
    minimumFractionDigits: 2,
    maximumFractionDigits: 2,
  })}`;
}

function formatZar(value, rate, { signed = false, compact = false } = {}) {
  const numericRate = Number(rate);
  if (!Number.isFinite(numericRate) || numericRate <= 0) return "--";
  const numeric = Number(value || 0) * numericRate;
  const abs = Math.abs(numeric);
  const sign = signed ? (numeric >= 0 ? "+" : "-") : "";
  if (compact && abs >= 1000) {
    const suffix = abs >= 1000000 ? "m" : "k";
    const divisor = abs >= 1000000 ? 1000000 : 1000;
    return `${sign}R${(abs / divisor).toFixed(1)}${suffix}`;
  }
  return `${sign}R${abs.toLocaleString("en-US", {
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

function symbolMarket(symbol, group) {
  const code = String(symbol || "").toUpperCase();
  const bucket = toDeskGroup(group);
  if (bucket === "Metals" || /^(XAU|XAG)/.test(code)) return "Metals";
  if (bucket === "Crypto" || /^(BTC|ETH|LTC|XRP)/.test(code)) return "Crypto";
  if (bucket === "CFDs" || /^(NAS|US30|SPX|GER|EU50|FRA|UK100|JP225|AUS200|HK50)/.test(code)) return "Indices";
  return "Forex";
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
              <span className={item.change >= 0 ? "pos" : "neg"}>{fmtAbsNumber(item.change, 2)}%</span>
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
  const [token, setToken] = useState(() => safeStorageGet("cipherfx_mt5_token") || "");
  const [status, setStatus] = useState(null);
  const [terminal, setTerminal] = useState(null);
  const [marketFeed, setMarketFeed] = useState(null);
  const [symbolsFeed, setSymbolsFeed] = useState([]);
  const [signalsFeed, setSignalsFeed] = useState([]);
  const [scanner, setScanner] = useState(null);
  const [audit, setAudit] = useState(null);
  const [marketHours, setMarketHours] = useState(null);
  const [visualIntelligence, setVisualIntelligence] = useState(null);
  const [intelligenceOverview, setIntelligenceOverview] = useState(null);
  const [replayTimeframe, setReplayTimeframe] = useState("M5");
  const [replayData, setReplayData] = useState(null);
  const [replayCursor, setReplayCursor] = useState(0);
  const [replayPlaying, setReplayPlaying] = useState(false);
  const [replaySpeed, setReplaySpeed] = useState(2);
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
  const [dealSummaries, setDealSummaries] = useState({ today: null, week: null, month: null });

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
    const nextToken = force ? "" : (token || safeStorageGet("cipherfx_mt5_token") || "");
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
        safeStorageRemove("cipherfx_mt5_token");
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
        const [nextStatus, nextTerminal, nextSymbols, nextTrades, nextOrders, nextScanner, nextAudit, nextMarketHours, nextVisual, nextIntelligence, nextTodayDeals, nextWeekDeals, nextMonthDeals] = await Promise.all([
          h("GET", "/status", null, sessionToken).catch((error) => {
            setApiError(describeApiError(error, "Status MT5 feed unavailable"));
            return null;
          }),
          h("GET", "/terminal", null, sessionToken).catch((error) => {
            setApiError(describeApiError(error, "Terminal feed unavailable"));
            return null;
          }),
          h("GET", "/symbols", null, sessionToken).catch(() => []),
          h("GET", "/trades?limit=1&include_history=false", null, sessionToken).catch(() => []),
          h("GET", "/orders", null, sessionToken).catch(() => []),
          h("GET", "/scanner", null, sessionToken).catch(() => null),
          h("GET", "/audit/summary", null, sessionToken).catch(() => null),
          h("GET", "/market-hours", null, sessionToken).catch(() => null),
          h("GET", "/visual/status", null, sessionToken).catch(() => null),
          h("GET", "/intelligence/overview", null, sessionToken).catch(() => null),
          h("GET", "/deals?period=today&limit=50", null, sessionToken).catch(() => ({ deals: [], summary: null })),
          h("GET", "/deals?period=week&limit=50", null, sessionToken).catch(() => ({ deals: [], summary: null })),
          h("GET", "/deals?period=month&limit=50", null, sessionToken).catch(() => ({ deals: [], summary: null })),
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
        setVisualIntelligence(nextVisual);
        setIntelligenceOverview(nextIntelligence);
        setDealSummaries({
          today: nextTodayDeals?.summary || null,
          week: nextWeekDeals?.summary || null,
          month: nextMonthDeals?.summary || null,
        });
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
        const closedTradeRows = Array.isArray(nextMonthDeals?.deals) && nextMonthDeals.deals.length
          ? nextMonthDeals.deals
          : (nextTrades || []);
        setDeals(closedTradeRows.map((row, index) => {
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
    const id = window.setInterval(loadLive, 10000);
    return () => {
      cancelled = true;
      window.clearInterval(id);
    };
  }, [selectedSymbol, token]);

  // Quotes are a dashboard-only bridge feed. They do not depend on scanner,
  // setup, pending, or execution state and are refreshed independently.
  useEffect(() => {
    let cancelled = false;
    let inFlight = false;
    async function loadMarketFeed() {
      if (inFlight) return;
      inFlight = true;
      try {
        const sessionToken = await ensureToken();
        const symbols = activeSymbols.join(",");
        const payload = await h(
          "GET",
          "/live/market?sym=" + encodeURIComponent(selectedSymbol) + "&symbols=" + encodeURIComponent(symbols),
          null,
          sessionToken,
          5000,
        );
        if (!cancelled) {
          setMarketFeed(
            payload && payload.source === "mt5_bridge_live"
              ? payload
              : { source: "mt5_bridge_unavailable", ticks: [], generated_at: new Date().toISOString() },
          );
        }
      } catch {
        if (!cancelled) {
          setMarketFeed({ source: "mt5_bridge_unavailable", ticks: [], generated_at: new Date().toISOString() });
        }
      } finally {
        inFlight = false;
      }
    }
    loadMarketFeed();
    const id = window.setInterval(loadMarketFeed, 250);
    return () => {
      cancelled = true;
      window.clearInterval(id);
    };
  }, [activeSymbols, selectedSymbol, token]);

  useEffect(() => {
    let cancelled = false;
    let inFlight = false;
    let timerId = null;
    async function loadCandles(targetTf, setter) {
      if (cancelled || inFlight) return;
      inFlight = true;
      try {
        const sessionToken = await ensureToken();
        const rows = await h(
          "GET",
          `/candles?sym=${encodeURIComponent(selectedSymbol)}&timeframe=${encodeURIComponent(targetTf)}&limit=140`,
          null,
          sessionToken,
          5000,
        );
        if (!cancelled) setter(Array.isArray(rows) ? rows : []);
      } catch {
        if (!cancelled) setter([]);
      } finally {
        inFlight = false;
        if (!cancelled) timerId = window.setTimeout(() => loadCandles(targetTf, setter), 1000);
      }
    }
    loadCandles(timeframe, setDesktopCandles);
    return () => {
      cancelled = true;
      if (timerId !== null) window.clearTimeout(timerId);
    };
  }, [selectedSymbol, timeframe, token]);

  useEffect(() => {
    let cancelled = false;
    let inFlight = false;
    let timerId = null;
    async function loadCandles(targetTf, setter) {
      if (cancelled || inFlight) return;
      inFlight = true;
      try {
        const sessionToken = await ensureToken();
        const rows = await h(
          "GET",
          `/candles?sym=${encodeURIComponent(selectedSymbol)}&timeframe=${encodeURIComponent(targetTf)}&limit=110`,
          null,
          sessionToken,
          5000,
        );
        if (!cancelled) setter(Array.isArray(rows) ? rows : []);
      } catch {
        if (!cancelled) setter([]);
      } finally {
        inFlight = false;
        if (!cancelled) timerId = window.setTimeout(() => loadCandles(targetTf, setter), 1000);
      }
    }
    loadCandles(mobileTimeframe, setMobileCandles);
    return () => {
      cancelled = true;
      if (timerId !== null) window.clearTimeout(timerId);
    };
  }, [selectedSymbol, mobileTimeframe, token]);

  useEffect(() => {
    let cancelled = false;
    async function loadReplay() {
      try {
        const sessionToken = await ensureToken();
        const payload = await h(
          "GET",
          "/replay?sym=" + encodeURIComponent(selectedSymbol) + "&timeframe=" + encodeURIComponent(replayTimeframe) + "&limit=500",
          null,
          sessionToken
        );
        if (!cancelled) {
          setReplayData(payload && typeof payload === "object" ? payload : null);
          setReplayCursor(0);
          setReplayPlaying(false);
        }
      } catch {
        if (!cancelled) {
          setReplayData(null);
          setReplayCursor(0);
          setReplayPlaying(false);
        }
      }
    }
    loadReplay();
    return () => {
      cancelled = true;
    };
  }, [selectedSymbol, replayTimeframe, token]);

  useEffect(() => {
    if (!replayPlaying || !replayData?.candles?.length) return undefined;
    const last = replayData.candles.length - 1;
    if (replayCursor >= last) {
      setReplayPlaying(false);
      return undefined;
    }
    const interval = window.setInterval(() => {
      setReplayCursor((current) => {
        if (current >= last) {
          setReplayPlaying(false);
          return current;
        }
        return current + 1;
      });
    }, Math.max(80, Math.round(1000 / Number(replaySpeed || 1))));
    return () => window.clearInterval(interval);
  }, [replayPlaying, replaySpeed, replayCursor, replayData]);

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
    const liveTicks = Array.isArray(marketFeed?.ticks) ? marketFeed.ticks : [];
    const tickMap = new Map(liveTicks.map((row) => [row.symbol, row]));
    const signalMap = new Map((signalsFeed || []).map((row) => [row.sym, row]));
    const apiRows = new Map((symbolsFeed || []).map((row) => [row.symbol, row]));
    const resolvedAliasSymbols = new Set(
      (symbolsFeed || [])
        .filter((row) => row.resolved_symbol && row.resolved_symbol !== row.symbol)
        .map((row) => String(row.resolved_symbol).toUpperCase())
    );
    const catalog = Array.isArray(symbolsFeed) && symbolsFeed.length
      ? symbolsFeed
      : (Array.isArray(terminal?.symbols) ? terminal.symbols : []);
    const merged = catalog.map((row) => {
      const symbol = String(row.symbol || row.sym || "").toUpperCase();
      const resolvedSymbol = row.resolved_symbol || symbol;
      const tick = tickMap.get(symbol) || tickMap.get(resolvedSymbol) || {};
      const signal = signalMap.get(symbol) || {};
      const group = row.description || row.group || row.path || "Market";
      return {
        symbol,
        resolvedSymbol,
        group,
        bucket: toDeskGroup(row.path || group),
        market: symbolMarket(symbol, row.path || group),
        bid: liveNumber(tick.bid),
        ask: liveNumber(tick.ask),
        change: liveNumber(signal.chg),
        low: liveNumber(signal.low),
        high: liveNumber(signal.high),
        spread: liveNumber(tick.spread),
        tickTime: tick.time || tick.time_utc || tick.timestamp || "",
        quoteAgeSeconds: liveNumber(tick.age_seconds),
        fresh: tick.fresh !== false,
        visible: row.visible !== false && (row.visible === true || activeSymbols.includes(symbol) || DEFAULT_ACTIVE_SYMBOLS.includes(symbol)),
      };
    }).filter((row) => row.symbol);

    for (const row of liveTicks) {
      const symbol = String(row.symbol || "").toUpperCase();
      if (!symbol || merged.find((item) => item.symbol === symbol)) continue;
      merged.push({
        symbol,
        resolvedSymbol: symbol,
        group: "Live feed",
        bucket: "All",
        bid: liveNumber(row.bid),
        ask: liveNumber(row.ask),
        change: null,
        low: null,
        high: null,
        spread: liveNumber(row.spread),
        tickTime: row.time || row.time_utc || row.timestamp || "",
        quoteAgeSeconds: liveNumber(row.age_seconds),
        fresh: row.fresh !== false,
        visible: true,
      });
    }

    return merged.sort((left, right) => {
      if (left.visible !== right.visible) return left.visible ? -1 : 1;
      if (left.group !== right.group) return left.group.localeCompare(right.group);
      return left.symbol.localeCompare(right.symbol);
    });
  }, [marketFeed, terminal, symbolsFeed, signalsFeed, activeSymbols]);

  const selectedLiveQuote = useMemo(
    () => liveSymbols.find((row) => row.symbol === selectedSymbol) || null,
    [liveSymbols, selectedSymbol],
  );
  const liveDesktopCandles = useMemo(
    () => applyLiveQuoteToCandles(desktopCandles, timeframe, selectedLiveQuote),
    [desktopCandles, timeframe, selectedLiveQuote, marketFeed?.generated_at],
  );
  const liveMobileCandles = useMemo(
    () => applyLiveQuoteToCandles(mobileCandles, mobileTimeframe, selectedLiveQuote),
    [mobileCandles, mobileTimeframe, selectedLiveQuote, marketFeed?.generated_at],
  );

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
    () => liveSymbols.find((item) => item.symbol === selectedSymbol) || liveSymbols[0] || {
      symbol: "",
      resolvedSymbol: "",
      group: "Live feed",
      bucket: "All",
      bid: null,
      ask: null,
      low: null,
      high: null,
      spread: null,
      change: null,
    },
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
  const intelligence = intelligenceOverview || {};
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
  const quoteClock = formatClock(marketFeed?.generated_at || terminal?.updated_at || status?.last_heartbeat);
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
      const symbol = row.symbol || row.sym || "Symbol";
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
  const usdZarRate = Number(status?.usd_zar_rate ?? terminal?.currency?.usd_zar_rate);
  const usdZarLabel = Number.isFinite(usdZarRate) && usdZarRate > 0 ? "USD/ZAR " + usdZarRate.toFixed(2) : "USD/ZAR unavailable";
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
            {fmtNumber(row.profit ?? row.pnl, 2, "0.00")}
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


  function simpleReason(raw) {
    const value = String(raw || "").toLowerCase();
    if (value.includes("market_window_closed") || value.includes("markets_closed") || value.includes("market closed") || value.includes("weekend")) return "Markets closed";
    if (value.includes("extended") || value.includes("exhaust") || value.includes("vertical") || value.includes("danger")) return "Move may be finished";
    if (value.includes("spread") || value.includes("volatile")) return "Fast price movement";
    if (value.includes("m5_trigger_not_ready") || value.includes("m5_trigger_waiting") || value.includes("m5 trigger")) return "Waiting for M5 trigger";
    if (value.includes("m5_trigger_not_ready") || value.includes("m5_trigger_waiting") || value.includes("m5 trigger")) return "Waiting for M5 trigger";
    if (value.includes("h1_not_aligned") || value.includes("m15_not_aligned") || value.includes("no clear alignment") || value.includes("timeframes disagree")) return "Timeframes disagree";
    if (value.includes("m15") || value.includes("pullback") || value.includes("reclaim") || value.includes("continuation")) return "Choppy movement";
    if (value.includes("range")) return "Moving in a range";
    if (value.includes("no directional") || value.includes("flat")) return "No clear direction";
    if (value.includes("trend") || value.includes("htf") || value.includes("alignment")) return "Timeframes disagree";
    if (value.includes("stale") || value.includes("missing") || value.includes("data_")) return "Data updating";
    if (value.includes("1m") || value.includes("confirmation")) return "Waiting for a fresh trigger";
    if (value.includes("cost")) return "Trading cost is high";
    if (value.includes("profile") || value.includes("engine policy") || value.includes("final_gate")) return "Final checks in progress";
    if (value.includes("daily") || value.includes("loss") || value.includes("risk")) return "Risk limit reached";
    if (value.includes("rejected")) return "Order rejected";
    if (value.includes("not qualified")) return "No clear setup";
    return "No clear setup";
  }

  function simpleMarketState(row, raw) {
    const value = String(raw || "").toLowerCase();
    const regime = String(row?.regime || row?.market_regime || row?.regime_state || "").toLowerCase();
    if (value.includes("spread") || value.includes("volatile") || regime.includes("volatile")) return "Fast price movement";
    if (value.includes("extended") || value.includes("exhaust") || value.includes("vertical") || value.includes("danger")) return "Move may be finished";
    if (value.includes("h1_not_aligned") || value.includes("m15_not_aligned") || value.includes("no clear alignment") || value.includes("timeframes disagree")) return "Timeframes disagree";
    if (regime.includes("trend")) return "Trend";
    if (regime.includes("chop") || regime.includes("range") || regime.includes("consolidat")) return "Choppy movement";
    if (value.includes("range") || value.includes("chop") || value.includes("consolidat")) return "Choppy movement";
    if (value.includes("flat") || value.includes("no directional") || String(row?.h1_bias || "").toUpperCase() === "FLAT") return "No clear direction";
    if (row?.gate_ok && value.includes("trend")) return "Trend";
    if (value.includes("trend") || value.includes("htf") || value.includes("alignment")) return "Timeframes disagree";
    if (value.includes("m15") || value.includes("pullback") || value.includes("reclaim") || value.includes("continuation")) return "Choppy movement";
    return simpleReason(raw);
  }

  function simpleTradeEvent(row) {
    const event = String(row?.event || "").toUpperCase();
    if (event === "ORDER_FILLED") return "Trade filled";
    if (event === "ORDER_SENT") return "Order sent";
    if (event === "ORDER_REJECTED" || event === "ORDER_REJECTED_RECORDED") return "Order declined";
    return "Trade update";
  }

  function renderScannerWorkspace(mode = "desktop") {
    const compact = mode === "mobile";
    const rows = (Array.isArray(scanner?.current_signals) ? scanner.current_signals : []).slice(0, compact ? 17 : 18);
    const counts = audit?.counts || {};
    const tradeItems = [...(audit?.trades || [])].sort((left, right) => String(right.ts || "").localeCompare(String(left.ts || ""))).slice(0, compact ? 6 : 10);
    const blockItems = [...(scannerBlockers.length ? scannerBlockers : (audit?.blocks || []))].sort((left, right) => String(right.updated_at || right.created_at || right.ts || "").localeCompare(String(left.updated_at || left.created_at || left.ts || ""))).slice(0, 30);
    const reasonSummary = Object.entries(blockItems.reduce((summary, row) => {
      const label = simpleReason(row.reason || row.reason_group || row.event);
      summary[label] = (summary[label] || 0) + 1;
      return summary;
    }, {})).sort((left, right) => right[1] - left[1]).slice(0, 6);
    const market = marketHours || audit?.market || {};
    const sessions = market.sessions || [];
    const marketClosed = Boolean(marketHours?.weekend_closed || audit?.market?.weekend_closed);
    const missingRecent = Array.isArray(scannerSummary.missing_recent_symbols) ? scannerSummary.missing_recent_symbols : [];
    const actionable = Number(scannerSummary.current_actionable || 0);
    const currentGate = marketClosed ? "MARKETS CLOSED" : status?.halt ? simpleReason(status.halt).toUpperCase() : actionable > 0 ? `${actionable} READY` : "WATCHING MARKET";
    const marketSymbolGroups = ["Forex", "Indices", "Metals"].map((label) => ({
      label,
      items: liveSymbols.filter((item) => item.market === label),
    }));
    return (
      <div className={"scanner-workspace" + (compact ? " mobile-scanner" : "")}>
        <div className="workspace-header scanner-header">
          <div><h3>Live Scan</h3><span>Live MT5 symbols · {compactCount(scannerSummary.tracked_symbols || liveSymbols.length)}</span></div>
          <div className={"scanner-state " + (marketClosed ? "blocked" : actionable > 0 ? "active" : "")}><Activity size={16} /><strong>{currentGate}</strong></div>
        </div>
        <div className="scanner-session-strip" aria-label="Global market sessions">
          {sessions.map((session) => <div className={"scanner-session " + (session.open ? "open" : "closed")} key={session.id}><span className="market-status-dot" /><div><strong>{session.label}</strong><em>{session.open ? "OPEN" : "CLOSED"} · {session.hours_sast}</em></div></div>)}
        </div>
        <div className="scanner-symbol-groups" aria-label="Live symbols by market">
          {marketSymbolGroups.map((group) => {
            const active = !marketClosed && group.items.some((item) => item.fresh !== false && item.bid !== null && item.ask !== null);
            return (
              <div className={"scanner-symbol-group " + (active ? "active" : "closed")} key={group.label}>
                <div className="scanner-symbol-group-head"><span className="market-status-dot" /><strong>{group.label}</strong><em>{active ? "ACTIVE" : "OFF"}</em></div>
                <div className="scanner-symbol-list">
                  {group.items.length ? group.items.map((item) => <span className={"scanner-symbol-pill " + (item.fresh !== false && item.bid !== null ? "live" : "stale")} key={item.symbol}>{item.symbol}</span>) : <span className="scanner-symbol-empty">No live symbols</span>}
                </div>
              </div>
            );
          })}
        </div>
        <div className="scanner-metrics scanner-metrics-compact">
          <div className="scanner-card"><span>Live symbols</span><strong>{compactCount(scannerSummary.current_universe_rows || scannerSummary.tracked_symbols || liveSymbols.length)}</strong><em>{missingRecent.length ? missingRecent.length + " waiting for session or scan" : "Grouped by market above"}</em></div>
          <div className="scanner-card"><span>Setups found</span><strong>{compactCount(counts.SETUP_CREATED ?? scannerSummary.actionable_today ?? 0)}</strong><em>{compactCount(Math.max(0, Number(scannerSummary.tracked_symbols || liveSymbols.length) - actionable))} currently monitored</em></div>
          <div className="scanner-card"><span>Trades opened</span><strong className={(counts.ORDER_FILLED || 0) > 0 ? "pos" : ""}>{compactCount(counts.ORDER_FILLED || 0)}</strong><em>{compactCount(counts.ORDER_REJECTED || 0)} orders rejected</em></div>
          <div className="scanner-card"><span>Live market feed</span><strong>{formatAgeLabel(marketFeed?.generated_at)}</strong><em>{formatDateTimeLabel(marketFeed?.generated_at) || "Waiting for live bridge"}</em></div>
        </div>
        <div className="scanner-split scanner-story-layout">
          <div className="scanner-list">
            <div className="scanner-section-title">H4 direction -&gt; H1 confirmation -&gt; M15 POI / structure -&gt; M5 break / retest -&gt; order</div>
            {rows.length ? rows.map((row, index) => {
              const symbol = row.symbol || row.sym || "Symbol";
              const id = symbol + "-" + (row.updated_at || row.ts || index);
              const rawScore = row.score ?? row.score_metric?.total;
              const score = Number(rawScore);
              const scoreAvailable = rawScore !== null && rawScore !== undefined && Number.isFinite(score);
              const h4Bias = String(row.h4_bias || row.bias_4h || "").toUpperCase();
              const scoreContext = h4Bias === "BUY" || h4Bias === "SELL"
                ? "Context " + h4Bias + " · M5 entry"
                : "M5 entry";
              const side = String(row.side || row.direction || "").toUpperCase();
              const liveReason = String(row.reason || row.gate || row.status || "").toLowerCase();
              const m5Waiting = liveReason.includes("m5_trigger_not_ready") || liveReason.includes("m5_trigger_waiting") || liveReason.includes("m5 trigger");
              const setupState = String(row.setup_state || row.state || "").toUpperCase();
              const hasRealSetup = row.pending_setup_created === true || ["ARMED", "WAITING_M5", "CONFIRMED", "M5_TRIGGERED", "EXECUTION_PENDING", "PASS"].includes(setupState);
              const rowSession = row.market_session ? sessions.find((session) => session.id === row.market_session) : null;
              const rowMarketClosed = Boolean(rowSession && !rowSession.open);
              const scoreMinimum = Number(row.score_metric?.minimum ?? row.min_score ?? 70);
              const scorePass = scoreAvailable && Number.isFinite(scoreMinimum) && score >= scoreMinimum && row.gate_ok === true;
              const scanStatus = marketClosed || rowMarketClosed ? "CLOSED" : m5Waiting && !hasRealSetup ? "WATCHING M5" : m5Waiting ? "WAITING FOR M5" : String(row.scan_status || (scorePass ? "WAITING" : "WATCHING")).toUpperCase();
              const story = marketClosed
                ? "This market is closed; scanning resumes in its next session."
                : rowMarketClosed
                  ? symbol + " is configured and active; " + rowSession.label + " is closed until " + rowSession.next_open_sast + "."
                  : m5Waiting && !hasRealSetup
                    ? "No M5 setup: the required break or retest was not present on this scan."
                    : (row.scan_story || simpleMarketState(row, row.reason || row.gate || row.status));
              const usesM1Entry = !String(row.execution_1m_result || "").toUpperCase().includes("NOT_USED")
                && !String(row.execution_path || "").toUpperCase().includes("M5_DIRECT")
                && !String(row.entry_mode || "").toUpperCase().includes("M5");
              const rawProgress = Array.isArray(row.scan_progress) && row.scan_progress.length ? row.scan_progress : [
                { key: "H4", label: "Direction", state: scorePass ? "complete" : "current" },
                { key: "H1", label: "Check", state: scorePass ? "complete" : "inactive" },
                { key: "M15", label: "Structure", state: scorePass ? "complete" : "inactive" },
                { key: "M5", label: "Trigger", state: scorePass ? "complete" : "inactive" },
                { key: "ORDER", label: "Order", state: scorePass ? "current" : "inactive" },
              ];
              const progress = rawProgress.map((stage) => {
                const key = String(stage.key || "").toUpperCase();
                if (key === "ORDER" && !scorePass) return { ...stage, state: "inactive" };
                return stage;
              }).filter((stage) => {
                const key = String(stage.key || "").toUpperCase();
                const diagnostic = String(stage.label || "").toLowerCase().includes("diagnostic");
                return key !== "M1" || (usesM1Entry && !diagnostic);
              });
              return (
                <article className={"scanner-story-row " + scanStatus.toLowerCase()} key={id}>
                  <div className="scanner-story-head">
                    <div><strong>{symbol}</strong><span className={side === "BUY" ? "pos" : side === "SELL" ? "neg" : ""}>{side || "WATCH"}</span></div>
                    <div><b>{scoreAvailable ? `Score ${score.toFixed(0)}` : `Score unavailable`}</b><em>{scoreContext ? `${scoreContext} · ` : ""}{scanStatus}</em></div>
                  </div>
                  <p>{story}</p>
                  <div className="scanner-stage-track" aria-label={`${symbol} scan progress`}>
                    {progress.map((stage) => <div className={"scanner-stage " + stage.state} key={stage.key}><span>{stage.key === "ORDER" ? (stage.state === "current" ? "SEND" : "ORDER") : stage.key}</span><em>{stage.label}</em></div>)}
                  </div>
                  <time>{row.scan_trigger === "COMPLETED_M5" ? "Latest completed M5 · " + (formatDateTimeLabel(row.scan_candle_times?.M5_close || row.scan_cycle_at || row.updated_at || row.ts) || "Live") : (formatDateTimeLabel(row.updated_at || row.ts) || "Live")}</time>
                </article>
              );
            }) : <div className="empty-state">Waiting for the next completed market scan.</div>}
          </div>
          <aside className="scanner-side">
            <div className="scanner-section-title"><strong>Why symbols are waiting</strong><span>Latest checks</span></div>
            <div className="reason-summary-grid">{reasonSummary.length ? reasonSummary.map(([label, count]) => <div className="reason-summary-row" key={label}><strong>{label}</strong><span>{count}</span></div>) : <div className="empty-state">No current checks.</div>}</div>
            <div className="scanner-section-title"><strong>Recent trades</strong><span>{compactCount(tradeItems.length)} recent</span></div>
            <div className="scanner-event-list">{tradeItems.length ? tradeItems.map((row, index) => <div className="scanner-event-row" key={row.event + "-" + row.ts + "-" + index}><div><strong>{row.symbol || row.sym || "Symbol"}</strong><span>{simpleTradeEvent(row)}</span><time>{formatDateTimeLabel(row.ts) || row.ts || "Live"}</time></div><p>{row.event === "ORDER_FILLED" ? "Trade opened on MT5" : simpleReason(row.reason || row.raw_status || row.event)}</p></div>) : <div className="empty-state">No trades recorded in this session.</div>}</div>
          </aside>
        </div>
      </div>
    );
  }
  function renderIntelligenceWorkspace(mode = "desktop") {
    const compact = mode === "mobile";
    const overview = intelligenceOverview || {};
    const planning = overview.planning || {};
    const learning = overview.learning || {};
    const validation = overview.validation || {};
    const execution = overview.execution || {};
    const data = overview.data_status || {};
    const plans = Array.isArray(planning.latest_by_symbol) ? planning.latest_by_symbol : [];
    const adjustments = Array.isArray(learning.latest_adjustments) ? learning.latest_adjustments : [];
    const health = Array.isArray(data.health_counts) ? data.health_counts : [];
    const walk = validation.walk_forward || {};
    const drift = validation.drift || {};
    const deployment = execution.deployment || {};
    const latestDecision = execution.last_decision || {};
    const number = (value, decimals = 0) => {
      const n = Number(value);
      return Number.isFinite(n) ? n.toFixed(decimals) : "--";
    };
    const humanStatus = (value) => {
      const raw = String(value || "UNAVAILABLE").toUpperCase();
      const labels = { SHADOW_ONLY:"Shadow only", SHADOW:"Shadow", AVAILABLE:"Available", UNAVAILABLE:"Not available", PASS:"Ready", CLEAR:"Clear", REVIEW:"Review needed", DRIFT_DETECTED:"Change detected", NO_TRADE:"No trade", ACTIONABLE:"Ready", NOT_QUALIFIED:"Not ready", QUALIFIED:"Ready", LIVE:"Live", OFFLINE:"Offline", APPLIED:"Applied", NOT_APPLIED:"Not applied", UNKNOWN:"Not available" };
      if (labels[raw]) return labels[raw];
      return raw.toLowerCase().replaceAll("_"," ").replace(/(^|\s)\S/g,(letter)=>letter.toUpperCase());
    };
    const humanAlignment = (value) => {
      const raw = String(value || "").toUpperCase();
      if (raw === "ALIGNED" || raw === "FULL_ALIGNMENT") return "Timeframes agree";
      if (raw === "PARTIAL" || raw === "PARTIAL_ALIGNMENT") return "Some timeframes agree";
      if (raw === "NOT_ALIGNED") return "Timeframes disagree";
      return "No clear alignment";
    };
    const humanAsset = (value) => {
      const raw = String(value || "").toUpperCase();
      if (raw.includes("FOREX") || raw === "FX") return "Forex";
      if (raw.includes("METAL")) return "Metal";
      if (raw.includes("INDEX")) return "Index";
      return raw ? raw.toLowerCase().replaceAll("_"," ") : "Market";
    };
    const humanReason = (value) => simpleReason(value);
    const zoneLabel = (value) => {
      if (value && typeof value === "object") {
        const low = value.low ?? value.min ?? value.lower;
        const high = value.high ?? value.max ?? value.upper;
        if (low != null || high != null) return [low, high].filter((item) => item != null).join(" - ");
        return "Zone recorded";
      }
      return String(value || "No price area recorded");
    };
    return (
      <div className={"intelligence-workspace" + (compact ? " mobile-intelligence" : "")}>
        <div className="workspace-header intelligence-header">
          <div><h3>Planning & Learning</h3><span>What MT5 planned, learned and recorded</span></div>
          <div className="intelligence-source"><Sparkles size={16} /><strong>LIVE MT5</strong><span>Mode: {humanStatus(overview.mode || "SHADOW")}</span></div>
        </div>
        <div className="intelligence-metrics">
          <div className="intelligence-card"><span>Plans</span><strong>{compactCount(planning.plan_count || 0)}</strong><em>{humanStatus(planning.status)}</em></div>
          <div className="intelligence-card"><span>Learning samples</span><strong>{compactCount(learning.shadow_sample_count || 0)}</strong><em>Shadow data only</em></div>
          <div className="intelligence-card"><span>Changes applied</span><strong className={learning.applied_count ? "neg" : "pos"}>{compactCount(learning.applied_count || 0)}</strong><em>{humanStatus(learning.mode || "SHADOW_ONLY")}</em></div>
          <div className="intelligence-card"><span>Connection</span><strong className={data.mt5_connected ? "pos" : "neg"}>{data.mt5_connected ? "Connected" : "Offline"}</strong><em>{formatAgeLabel(data.last_scan) || "No recent update"}</em></div>
        </div>
        <div className="intelligence-story">
          <div className="scanner-section-title"><strong>What the data says</strong><span>Updated {formatDateTimeLabel(overview.generated_at) || "live"}</span></div>
          <p>{planning.story || "Planning data is not available yet."}</p>
          <p>{learning.story || "Learning data is not available yet."}</p>
          <div className="intelligence-boundary"><strong>What this means</strong><span>This tab explains the plan and data record. The Scan tab shows the live market decision.</span></div>
        </div>
        <div className="intelligence-grid">
          <section className="intelligence-panel">
            <div className="scanner-section-title"><strong>Current plans</strong><span>{compactCount(plans.length)} symbols</span></div>
            <div className="intelligence-list">
              {plans.length ? plans.map((plan, index) => (
                <div className="intelligence-row" key={(plan.symbol || "plan") + "-" + index}>
                  <div className="intelligence-row-main"><strong>{plan.symbol || "--"}</strong><span>{humanAsset(plan.asset_class)}</span><em>{humanStatus(plan.status)}</em></div>
                  <div className="intelligence-row-detail"><b className={plan.h4_bias === "BUY" ? "pos" : plan.h4_bias === "SELL" ? "neg" : ""}>{plan.h4_bias || "NO TRADE"}</b><span>H4 {number(plan.h4_score)} - H1 {number(plan.h1_score)} - M15 {number(plan.m15_score)}</span><span>{humanAlignment(plan.alignment)}</span><span>Confidence {number(plan.confidence)}%</span></div>
                  <small>{zoneLabel(plan.entry_zone)} - {formatDateTimeLabel(plan.created_at) || "time unavailable"}</small>
                </div>
              )) : <div className="empty-state">No plans recorded in MT5 SQLite.</div>}
            </div>
          </section>
          <section className="intelligence-panel">
            <div className="scanner-section-title"><strong>Recent learning</strong><span>{humanStatus(learning.mode || "SHADOW_ONLY")}</span></div>
            <div className="intelligence-list">
              {adjustments.length ? adjustments.slice(0, compact ? 6 : 10).map((item, index) => (
                <div className="intelligence-row" key={(item.symbol || "adjustment") + "-" + index}>
                  <div className="intelligence-row-main"><strong>{item.symbol || "--"}</strong><span>{humanAlignment(item.plan_alignment)}</span><em>{humanStatus(item.mode || "SHADOW_ONLY")}</em></div>
                  <div className="intelligence-row-detail"><b>{number(item.score_before)} {"->"} {number(item.score_after)}</b><span>Score change {number(item.score_adjustment, 1)}</span><span>{item.applied ? "Applied" : "Not applied"}</span></div>
                  <small>{humanReason(item.reason || "No clear setup")} - {formatDateTimeLabel(item.created_at) || "time unavailable"}</small>
                </div>
              )) : <div className="empty-state">No score adjustments recorded.</div>}
            </div>
          </section>
        </div>
        <div className="intelligence-grid">
          <section className="intelligence-panel">
            <div className="scanner-section-title"><strong>Checks on the data</strong><span>Recorded evidence</span></div>
            <div className="intelligence-facts">
              <div><span>Historical test</span><b>{humanStatus(walk.status)}</b><em>{walk.sample_size || "--"} samples - {walk.fold_count || "--"} folds</em></div>
              <div><span>Pattern reliability</span><b className={walk.overfit ? "neg" : "pos"}>{walk.overfit ? "Review" : "Clear"}</b><em>{walk.overfit_folds || 0} patterns need review</em></div>
              <div><span>Recent change</span><b className={drift.status === "DRIFT_DETECTED" ? "neg" : ""}>{humanStatus(drift.status)}</b><em>recent data</em></div>
              <div><span>Recent result</span><b>{number(drift.recent_expectancy_r, 2)}R</b><em>change {number(drift.expectancy_delta_r, 2)}R</em></div>
            </div>
          </section>
          <section className="intelligence-panel">
            <div className="scanner-section-title"><strong>Trading status</strong><span>Current MT5 records</span></div>
            <div className="intelligence-facts">
              <div><span>Live setup</span><b>{humanStatus(deployment.stage || deployment.status)}</b><em>MT5 decision path active</em></div>
              <div><span>Live permission</span><b className={deployment.live_approval ? "pos" : ""}>{deployment.live_approval ? "YES" : "NO"}</b><em>{deployment.replacement_strategy_live ? "Live route available" : "Live route not active"}</em></div>
              <div><span>Latest decision</span><b>{latestDecision.symbol || "--"}</b><em>{humanStatus(latestDecision.status)} - {humanReason(latestDecision.reason || "No clear setup")}</em></div>
              <div><span>Broker symbols</span><b>{compactCount(data.broker_symbol_specs || 0)}</b><em>{data.mt5_connected ? "Loaded from live MT5" : "MT5 feed unavailable"}</em></div>
            </div>
          </section>
        </div>
        <div className="intelligence-footer">
          <span>System: {health.map((item) => humanStatus(item.status) + " " + item.count).join(" - ") || "No health data"}</span>
          <span>Reviews waiting: {compactCount(learning.false_entry_review_count || 0)}</span>
        </div>
      </div>
    );
  }

  function renderMarketHoursWorkspace() {
    const market = marketHours || audit?.market || {};
    const sessions = market.sessions || [];
    const anyMarketOpen = market.global_status === "OPEN";
    return (
      <div className="market-hours-workspace">
        <div className="workspace-header"><div><h3>Market Hours</h3><span>Live session status - SAST</span></div><div className={"market-hours-state " + (anyMarketOpen ? "open" : "closed")}><span className="market-status-dot" />{anyMarketOpen ? "MARKETS OPEN" : "MARKETS CLOSED"}</div></div>
        <div className="market-session-grid">{sessions.map((session) => <div className={"market-session-row " + (session.open ? "open" : "closed")} key={session.id}><span className="market-status-dot" /><div><strong>{session.label}</strong><em>{session.region}</em></div><span className="market-session-hours">{session.hours_sast}</span><span className="market-session-status">{session.open ? "OPEN" : "CLOSED"}<small>{session.open ? "live" : session.reason}</small></span><span className="market-next-open">Next: {session.next_open_sast}</span></div>)}</div>
        <div className="market-hours-foot">Current VPS time: {formatDateTimeLabel(market.now_sast)} - Schedule adjusts for regional daylight time and is displayed in SAST.</div>
      </div>
    );
  }


  function renderDeskUtilityPanel() {
    if (desktopTab === "calendar") {
      return (
        <div className="info-panel">
          <h3>Market Sessions</h3>
          <p>Live session status for {selected.symbol || "the selected market"}. Times are SAST.</p>
          <div className="info-list">
            {(marketHours?.sessions || []).map((session) => (
              <div className="info-row" key={session.id}>
                <strong className={session.open ? "pos" : "neg"}>{session.label} · {session.open ? "OPEN" : "CLOSED"}</strong>
                <span>{session.hours_sast} · {session.open ? "Live session" : session.reason}</span>
              </div>
            ))}
            {!marketHours?.sessions?.length && <div className="empty-state">Waiting for live market-hours data.</div>}
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

  function renderReplayWorkspace(mode = "desktop") {
    const compact = mode === "mobile";
    const replayCandles = Array.isArray(replayData?.candles) ? replayData.candles : [];
    const replayTrades = Array.isArray(replayData?.trades) ? replayData.trades : [];
    const maxCursor = Math.max(0, replayCandles.length - 1);
    const cursor = Math.min(replayCursor, maxCursor);
    const currentCandle = replayCandles[cursor];
    const progress = replayCandles.length ? Math.round(((cursor + 1) / replayCandles.length) * 100) : 0;
    return (
      <div className={"replay-workspace" + (compact ? " replay-mobile" : "")}>
        <div className="workspace-header replay-header">
          <div>
            <h3>Trade Replay</h3>
            <span>Historical evidence only · no live orders · {replayData?.source || "waiting for data"}</span>
          </div>
          <div className="replay-safety-label">REPLAY ONLY</div>
        </div>
        <div className="replay-toolbar">
          <div className="replay-timeframes">
            {["M1", "M5", "M15", "H1", "H4"].map((item) => (
              <button key={item} type="button" className={replayTimeframe === item ? "active" : ""} onClick={() => setReplayTimeframe(item)}>{item}</button>
            ))}
          </div>
          <div className="replay-controls">
            <button type="button" className="icon-btn" title="Reset replay" aria-label="Reset replay" onClick={() => { setReplayPlaying(false); setReplayCursor(0); }}><SkipBack size={16} /></button>
            <button type="button" className="icon-btn replay-play" title={replayPlaying ? "Pause replay" : "Play replay"} aria-label={replayPlaying ? "Pause replay" : "Play replay"} onClick={() => setReplayPlaying((value) => !value)} disabled={!replayCandles.length}>{replayPlaying ? <Pause size={16} /> : <Play size={16} />}</button>
            <button type="button" className="icon-btn" title="Step one candle" aria-label="Step one candle" onClick={() => setReplayCursor((value) => Math.min(maxCursor, value + 1))} disabled={!replayCandles.length || cursor >= maxCursor}><StepForward size={16} /></button>
            <label className="replay-speed"><span>Speed</span><select value={replaySpeed} onChange={(event) => setReplaySpeed(Number(event.target.value))}><option value="0.5">0.5x</option><option value="1">1x</option><option value="2">2x</option><option value="4">4x</option><option value="8">8x</option></select></label>
          </div>
        </div>
        <div className="replay-progress">
          <input aria-label="Replay position" type="range" min="0" max={maxCursor} value={cursor} onChange={(event) => { setReplayPlaying(false); setReplayCursor(Number(event.target.value)); }} disabled={!replayCandles.length} />
          <span>{progress}% · {cursor + 1}/{replayCandles.length || 0} candles</span>
        </div>
        <div className="replay-chart-stage">
          <TradeReplayChart sym={selected.symbol} timeframe={replayTimeframe} candles={replayCandles} trades={replayTrades} cursor={cursor} height={compact ? 350 : 500} />
        </div>
        <div className="replay-summary">
          <div><span>Replay time</span><strong>{currentCandle ? formatDateTimeLabel(currentCandle.ts || currentCandle.time || currentCandle.timestamp) : "--"}</strong></div>
          <div><span>Candle close</span><strong>{currentCandle ? fmtPrice(selected.symbol, currentCandle.close) : "--"}</strong></div>
          <div><span>Trades loaded</span><strong>{replayTrades.length}</strong></div>
          <div><span>Execution</span><strong>DISABLED</strong></div>
        </div>
        <div className="replay-trade-list">
          <div className="scanner-section-title"><strong>Trade markers</strong><span>{replayTrades.length} persisted trade records</span></div>
          {replayTrades.length ? replayTrades.slice(0, compact ? 8 : 14).map((trade, index) => (
            <div className="replay-trade-row" key={(trade.trade_id || "trade") + "-" + index}>
              <strong className={String(trade.direction || "").toUpperCase() === "BUY" ? "pos" : "neg"}>{trade.direction || "--"}</strong>
              <span>{trade.trade_id || "MT5 trade"}</span>
              <span>{fmtPrice(selected.symbol, trade.entry)}</span>
              <span>{trade.outcome || "OPEN"}</span>
              <span>{trade.opened_at || trade.opened_date || "--"}</span>
            </div>
          )) : <div className="empty-state">No persisted trades are available for this symbol.</div>}
        </div>
      </div>
    );
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
              candles={liveDesktopCandles}
              height={420}
              fill
              apiBase={API}
            />
          </div>
          <div className="chart-readout">
            <div className="detail-card"><strong>Session</strong><span>{describeInstrument(selected.symbol, selected.group)}</span></div>
            <div className="detail-card"><strong>Low / High</strong><span>{fmtPrice(selected.symbol, selected.low)} / {fmtPrice(selected.symbol, selected.high)}</span></div>
            <div className="detail-card"><strong>Execution</strong><span>{status?.dry_run ? "DRY RUN" : status?.mt5_connected ? "LIVE MT5" : "MT5 offline"} · daily {status?.daily_trade_count ?? "--"}/{status?.max_daily_trades ?? "--"} · scan {status?.scan_seconds ?? "--"} s</span></div>
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
          <div className="ticket-footer">Spread: {selected.spread ?? "--"} · {status?.mt5_trade_allowed === true ? "MT5 trading allowed" : status?.mt5_trade_allowed === false ? "MT5 trading disabled" : "MT5 trade status unavailable"}</div>
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
                candles={liveDesktopCandles}
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
                      <span>{row.count} trade{row.count === 1 ? "" : "s"} · vol {fmtNumber(row.volume, 2, "0.00")}</span>
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

  function renderDealPeriodSummary() {
    return (
      <div className="deal-period-summary" aria-label="Trade totals">
        {[
          ["Today", dealSummaries.today],
          ["This week", dealSummaries.week],
          ["This month", dealSummaries.month],
        ].map(([label, summary]) => (
          <div key={label}>
            <span>{label}</span>
            <strong>{summary?.total_trades ?? 0}</strong>
            <em className={Number(summary?.pnl || 0) >= 0 ? "pos" : "neg"}>{formatSignedUsd(summary?.pnl || 0)}</em>
          </div>
        ))}
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
        {renderDealPeriodSummary()}
        <div className="workspace-stack">
          {deals.length ? deals.map((row) => (
            <div className="position-card" key={row.id}>
              <div><strong>{row.symbol}</strong><span>{row.side} {row.volume}</span></div>
              <div className="trade-date-line"><span>{tradeDateLine(row, "history")}</span><span>{tradeMonthLabel(row) || row.tradeDate}</span></div>
              <div><span>{row.entry} → {row.exit}</span><strong className={row.pnl >= 0 ? "pos" : "neg"}>{fmtNumber(row.pnl, 2, "0.00")}</strong></div>
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
                <span className={selected.change >= 0 ? "pos" : "neg"}>{fmtNumber(selected.change, 2)}%</span>
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
              {desktopTab === "replay" && renderReplayWorkspace("desktop")}
              {desktopTab === "scanner" && renderScannerWorkspace("desktop")}
              {desktopTab === "intelligence" && renderIntelligenceWorkspace("desktop")}
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
          <span>{status?.mt5_connected ? "MT5 connected" : "MT5 offline"}</span>
          <span>Last update: {heartbeatLabel}</span>
          <span>SAST time: {serverClockLabel}</span>
          <span>{positions.length} open position{positions.length === 1 ? "" : "s"}</span>
          <span className={status?.allow_pyramiding ? "pos" : ""}>{status?.allow_pyramiding ? "Pyramiding " + (status.max_pyramid_levels || "") : "Pyramiding unavailable"}</span>
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
                      <span className={item.change >= 0 ? "pos" : "neg"}>{fmtAbsNumber(item.change, 2)}%</span>
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
                  candles={liveMobileCandles}
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

          {mobileTab === "replay" && (
            <div className="mobile-screen replay-screen">
              {renderReplayWorkspace("mobile")}
            </div>
          )}

          {mobileTab === "intelligence" && (
            <div className="mobile-screen intelligence-screen">
              <div className="mobile-brand-row">
                <span className="mobile-brand-spacer" aria-hidden="true" />
                <img src={logoSrc} alt="" className="mobile-header-logo" aria-hidden="true" />
                <div className="phone-actions">
                  <button className="icon-btn" title="Open scanner" aria-label="Open scanner" onClick={() => setMobileTab("scanner")}><Activity size={17} /></button>
                </div>
              </div>
              {renderIntelligenceWorkspace("mobile")}
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
                      <span className={row.profit >= 0 ? "pos" : "neg"}>{fmtNumber(row.profit, 2, "0.00")}</span>
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
              {mobileHistoryTab === "deals" && renderDealPeriodSummary()}
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

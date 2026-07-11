import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import {
  Activity,
  AlertTriangle,
  BarChart3,
  Bell,
  Bot,
  Briefcase,
  CalendarDays,
  ChevronDown,
  CircleHelp,
  FileText,
  Globe2,
  Home,
  LayoutDashboard,
  Link2,
  ListFilter,
  LogOut,
  Maximize2,
  Menu,
  MoreHorizontal,
  Newspaper,
  Pause,
  PieChart,
  Play,
  Power,
  ReceiptText,
  RefreshCw,
  Search,
  Settings,
  Shield,
  SlidersHorizontal,
  Star,
  Trash2,
  Wifi,
  X,
} from "lucide-react";
import CandleChart from "./CandleChart";
import brandUrl from "./cipherfx-icon.png";
import "./IBKRDashboard.css";

const API = "/api";
const TOKEN_KEY = "cipherfx_ibkr_dashboard_token";
const POLL_MS = 1000;
const SCAN_SECONDS = 890;

const DESKTOP_NAV = [
  ["dashboard", "Dashboard", LayoutDashboard],
  ["order", "Order Entry", ReceiptText],
  ["monitor", "Monitor", BarChart3],
  ["portfolio", "Portfolio", Briefcase],
  ["watchlist", "Watchlist", Star],
  ["options", "Options", SlidersHorizontal],
  ["news", "News", Newspaper],
  ["market", "Market", Globe2],
  ["reports", "Reports", FileText],
  ["settings", "Settings", Settings],
  ["assistant", "AI Assistant", Bot],
  ["alerts", "Alerts", Bell],
  ["calendar", "Calendar", CalendarDays],
  ["help", "Help", CircleHelp],
  ["logout", "Logout", LogOut],
];

const MOBILE_NAV = [
  ["home", "Home", Home],
  ["portfolio", "Portfolio", PieChart],
  ["trade", "Trade", Activity],
  ["watchlist", "Watchlist", ReceiptText],
  ["markets", "Markets", Globe2],
];

const MONITOR_TABS = ["Portfolio", "Favorites", "Watchlist", "Options"];
const PORTFOLIO_TABS = ["Positions", "Balances", "Orders", "Impact Lens"];
const NEWS_TABS = ["News", "Market", "Portfolio", "IBKR", "Traders Insight"];
const DEFAULT_SYMBOLS = ["NVDA", "META", "AUDUSD", "NZDUSD", "EURUSD", "VOD", "BARC"];

let sessionToken = "";

function number(value, fallback = 0) {
  const n = Number(value);
  return Number.isFinite(n) ? n : fallback;
}

function compact(value, digits = 1) {
  const n = number(value, 0);
  const abs = Math.abs(n);
  if (abs >= 1_000_000_000) return `${(n / 1_000_000_000).toFixed(digits)}B`;
  if (abs >= 1_000_000) return `${(n / 1_000_000).toFixed(digits)}M`;
  if (abs >= 1_000) return `${(n / 1_000).toFixed(digits)}K`;
  return n.toFixed(abs >= 100 ? 0 : digits);
}

function money(value, digits = 2) {
  const n = number(value, 0);
  return n.toLocaleString(undefined, {
    maximumFractionDigits: digits,
    minimumFractionDigits: digits,
  });
}

function signedMoney(value, digits = 2) {
  const n = number(value, 0);
  return `${n >= 0 ? "+" : "-"}${money(Math.abs(n), digits)}`;
}

function qty(value) {
  const n = number(value, 0);
  return Math.abs(n) >= 1000 ? compact(n, 1) : n.toLocaleString(undefined, { maximumFractionDigits: 4 });
}

function decimalsFor(sym, market) {
  const upper = String(sym || "").toUpperCase();
  if (market === "forex" || /^[A-Z]{6}$/.test(upper)) return upper.includes("JPY") ? 3 : 5;
  if (upper.length <= 4) return 2;
  return 2;
}

function price(sym, value, market) {
  const n = Number(value);
  if (!Number.isFinite(n) || n === 0) return "-";
  return n.toFixed(decimalsFor(sym, market));
}

function deltaPrice(sym, value, market) {
  const n = Number(value);
  if (!Number.isFinite(n)) return "-";
  return Math.abs(n).toFixed(decimalsFor(sym, market));
}

function pct(value) {
  const n = Number(value);
  if (!Number.isFinite(n)) return "-";
  return `${n >= 0 ? "+" : ""}${n.toFixed(2)}%`;
}

function ageLabel(iso) {
  if (!iso) return "-";
  const t = new Date(String(iso).replace(" ", "T")).getTime();
  if (!Number.isFinite(t)) return "-";
  const sec = Math.max(0, Math.floor((Date.now() - t) / 1000));
  if (sec < 60) return `${sec}s`;
  if (sec < 3600) return `${Math.floor(sec / 60)}m`;
  return `${Math.floor(sec / 3600)}h`;
}

function countdownLabel(iso) {
  if (!iso) return `${Math.floor(SCAN_SECONDS / 60)}m ${SCAN_SECONDS % 60}s`;
  const t = new Date(String(iso).replace(" ", "T")).getTime();
  if (!Number.isFinite(t)) return "-";
  const sec = Math.max(0, Math.floor((t - Date.now()) / 1000));
  return `${Math.floor(sec / 60)}m ${String(sec % 60).padStart(2, "0")}s`;
}

function normalizeNewsTs(ts) {
  const raw = number(ts, 0);
  if (!raw) return "";
  return new Date(raw * 1000).toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" });
}

async function ensureToken() {
  if (sessionToken) return sessionToken;
  sessionToken = localStorage.getItem(TOKEN_KEY) || "";
  if (sessionToken) return sessionToken;
  window.dispatchEvent(new CustomEvent("cipherfx:auth-expired", { detail: { dashboard: "ibkr" } }));
  throw new Error("Login required");
}

async function apiFetch(path, options = {}) {
  let token = await ensureToken();
  const makeHeaders = () => ({
    Authorization: `Bearer ${token}`,
    ...(options.body ? { "Content-Type": "application/json" } : {}),
    ...(options.headers || {}),
  });
  let res = await fetch(`${API}${path}`, { ...options, headers: makeHeaders() });
  if (res.status === 401 || res.status === 403) {
    sessionToken = "";
    localStorage.removeItem(TOKEN_KEY);
    window.dispatchEvent(new CustomEvent("cipherfx:auth-expired", { detail: { dashboard: "ibkr" } }));
  }
  if (!res.ok) {
    const text = await res.text().catch(() => "");
    throw new Error(text || `${path} failed with ${res.status}`);
  }
  const type = res.headers.get("content-type") || "";
  return type.includes("json") ? res.json() : res.text();
}

async function apiPost(path, body) {
  return apiFetch(path, { method: "POST", body: body == null ? undefined : JSON.stringify(body) });
}

function classFor(value) {
  return number(value, 0) >= 0 ? "pos" : "neg";
}

function safeArray(value) {
  return Array.isArray(value) ? value : [];
}

function useClock() {
  const [now, setNow] = useState(() => new Date());
  useEffect(() => {
    const id = window.setInterval(() => setNow(new Date()), 1000);
    return () => window.clearInterval(id);
  }, []);
  return now;
}

export function IBKRDashboard() {
  const now = useClock();
  const aliveRef = useRef(true);
  const pollTickRef = useRef(0);
  const autoSelectedRef = useRef(true);
  const [desktopView, setDesktopView] = useState("dashboard");
  const [mobileView, setMobileView] = useState("portfolio");
  const [monitorTab, setMonitorTab] = useState("Portfolio");
  const [portfolioTab, setPortfolioTab] = useState("Positions");
  const [newsTab, setNewsTab] = useState("News");
  const [search, setSearch] = useState("");
  const [selectedSym, setSelectedSym] = useState("");
  const [activityData, setActivityData] = useState({});
  const [statusData, setStatusData] = useState({});
  const [health, setHealth] = useState({});
  const [signals, setSignals] = useState([]);
  const [trades, setTrades] = useState([]);
  const [stats, setStats] = useState({});
  const [universe, setUniverse] = useState(null);
  const [candles, setCandles] = useState([]);
  const [news, setNews] = useState([]);
  const [apiError, setApiError] = useState("");
  const [notice, setNotice] = useState("");
  const [busy, setBusy] = useState("");
  const [order, setOrder] = useState({
    side: "BUY",
    symbol: "",
    qty: 1000,
    orderType: "LMT",
    limitPrice: "",
    tif: "DAY",
    strategy: "Adaptive",
  });

  const overview = activityData.overview || {};
  const positions = safeArray(activityData.positions);
  const balances = safeArray(activityData.balances?.length ? activityData.balances : overview.cash_balances);
  const openOrders = safeArray(activityData.open_orders);
  const summaryRows = safeArray(activityData.summary);
  const cancelledOrders = safeArray(activityData.cancelled_orders);

  const universeSymbols = useMemo(() => {
    if (!universe) return DEFAULT_SYMBOLS;
    const out = [];
    Object.values(universe).forEach((group) => safeArray(group?.instruments).forEach((sym) => out.push(sym)));
    return Array.from(new Set(out));
  }, [universe]);

  const monitorRows = useMemo(() => {
    const bySym = new Map();
    universeSymbols.forEach((sym) => bySym.set(sym, { sym, market: "stock_us" }));
    safeArray(signals).forEach((sig) => {
      bySym.set(sig.sym, { ...(bySym.get(sig.sym) || {}), ...sig, sym: sig.sym });
    });
    positions.forEach((posRow) => {
      bySym.set(posRow.sym, { ...(bySym.get(posRow.sym) || {}), ...posRow, sym: posRow.sym, hasPosition: true });
    });
    if (selectedSym && !bySym.has(selectedSym)) bySym.set(selectedSym, { sym: selectedSym, market: "stock_us" });
    return Array.from(bySym.values())
      .map((row) => {
        const last = number(row.current || row.price || row.close, 0);
        const change = number(row.change, 0);
        const pctChange = last ? (change / Math.max(Math.abs(last - change), 0.000001)) * 100 : null;
        return {
          ...row,
          last,
          change,
          pctChange,
          venue: row.venue || (row.market === "forex" ? "IDEALPRO" : row.market === "stock_uk" ? "LSE" : "SMART"),
        };
      })
      .sort((a, b) => Number(Boolean(b.hasPosition)) - Number(Boolean(a.hasPosition)) || String(a.sym).localeCompare(String(b.sym)));
  }, [positions, selectedSym, signals, universeSymbols]);

  const filteredRows = useMemo(() => {
    const q = search.trim().toUpperCase();
    return monitorRows.filter((row) => {
      if (monitorTab === "Portfolio" && !row.hasPosition && positions.length) return false;
      if (monitorTab === "Options" && row.market !== "option") return false;
      if (q && !`${row.sym} ${row.venue} ${row.market}`.toUpperCase().includes(q)) return false;
      return true;
    });
  }, [monitorRows, monitorTab, positions.length, search]);

  const selectedQuote = useMemo(() => {
    return monitorRows.find((row) => row.sym === selectedSym) || monitorRows[0] || { sym: selectedSym, market: "stock_us" };
  }, [monitorRows, selectedSym]);

  const selectedPosition = useMemo(() => positions.find((row) => row.sym === selectedSym), [positions, selectedSym]);
  const lastCandle = candles[candles.length - 1] || null;
  const selectedLast = number(selectedPosition?.current || selectedQuote?.last || lastCandle?.close, 0);
  const selectedBid = selectedQuote?.bid;
  const selectedAsk = selectedQuote?.ask;
  const mode = String(statusData.mode || "PAPER").toUpperCase();
  const connected = Boolean(activityData.connected || statusData.ibkr_connected);
  const botAlive = Boolean(health.bot_alive || statusData.last_heartbeat);

  const setSymbol = useCallback((sym, userInitiated = true) => {
    const next = String(sym || "").trim().toUpperCase();
    if (!next) return;
    autoSelectedRef.current = !userInitiated;
    setSelectedSym(next);
    setOrder((prev) => ({ ...prev, symbol: next }));
  }, []);

  const refresh = useCallback(async () => {
    try {
      const tick = pollTickRef.current + 1;
      pollTickRef.current = tick;
      const jobs = [
        apiFetch("/health").then((value) => { if (aliveRef.current) setHealth(value || {}); }),
        apiFetch("/signals").then((value) => { if (aliveRef.current) setSignals(safeArray(value)); }),
        selectedSym
          ? apiFetch(`/candles?sym=${encodeURIComponent(selectedSym)}&limit=180`).then((value) => { if (aliveRef.current) setCandles(safeArray(value)); })
          : Promise.resolve(),
      ];
      if (tick === 1 || tick % 5 === 0) {
        jobs.push(apiFetch("/status").then((value) => { if (aliveRef.current) setStatusData(value || {}); }));
        jobs.push(apiFetch("/broker/activity").then((value) => { if (aliveRef.current) setActivityData(value || {}); }));
      }
      if (tick === 1 || tick % 15 === 0) {
        jobs.push(apiFetch("/trades?limit=20").then((value) => { if (aliveRef.current) setTrades(safeArray(value)); }));
        jobs.push(apiFetch("/stats").then((value) => { if (aliveRef.current) setStats(value || {}); }));
      }
      const results = await Promise.allSettled(jobs);
      if (!aliveRef.current) return;
      const firstError = results.find((res) => res.status === "rejected");
      const hasCoreData = results.some((res) => res.status === "fulfilled");
      const message = firstError ? String(firstError.reason?.message || firstError.reason) : "";
      setApiError(!hasCoreData && message && !message.startsWith("<!DOCTYPE html>") ? message.slice(0, 220) : "");
    } catch (error) {
      if (aliveRef.current) setApiError(String(error?.message || error).slice(0, 220));
    }
  }, [selectedSym]);

  useEffect(() => {
    aliveRef.current = true;
    refresh();
    const id = window.setInterval(refresh, POLL_MS);
    return () => {
      aliveRef.current = false;
      window.clearInterval(id);
    };
  }, [refresh]);

  useEffect(() => {
    let alive = true;
    async function loadUniverse() {
      try {
        const data = await apiFetch("/universe");
        if (alive) setUniverse(data);
      } catch {
        if (alive) setUniverse(null);
      }
    }
    loadUniverse();
    return () => {
      alive = false;
    };
  }, []);

  useEffect(() => {
    let alive = true;
    async function loadNews() {
      if (!selectedSym) return;
      try {
        const data = await apiFetch(`/news?sym=${encodeURIComponent(selectedSym)}&limit=9`);
        if (alive) setNews(safeArray(data));
      } catch {
        if (alive) setNews([]);
      }
    }
    loadNews();
    const id = window.setInterval(loadNews, 60000);
    return () => {
      alive = false;
      window.clearInterval(id);
    };
  }, [selectedSym]);

  useEffect(() => {
    const livePositionSym = positions[0]?.sym;
    if (livePositionSym && (!selectedSym || autoSelectedRef.current) && livePositionSym !== selectedSym) {
      setSymbol(livePositionSym, false);
      return;
    }
    if (selectedSym) return;
    const next = monitorRows.find((row) => row.last)?.sym || monitorRows[0]?.sym;
    if (next) setSymbol(next, false);
  }, [monitorRows, positions, selectedSym, setSymbol]);

  async function runControl(label, fn) {
    setBusy(label);
    setNotice("");
    try {
      const result = await fn();
      setNotice(result?.message || `${label} complete`);
      await refresh();
    } catch (error) {
      setNotice(String(error?.message || error).slice(0, 240));
    } finally {
      setBusy("");
    }
  }

  function handleOrderSubmit() {
    const formQty = number(order.qty, 0);
    if (!order.symbol || formQty <= 0) {
      setNotice("Order entry needs a symbol and quantity.");
      return;
    }
    setNotice("Manual IBKR order placement is not exposed by this dashboard API. The automated paper bot is active and order controls are available for live broker positions.");
  }

  function renderBrand(compactMode = false) {
    return (
      <div className="ibkr-brand">
        <img src={brandUrl} alt="Cipher FX" />
      </div>
    );
  }

  function renderSearch(className = "") {
    return (
      <label className={`ibkr-search ${className}`}>
        <Search size={16} />
        <input
          value={search}
          onChange={(event) => setSearch(event.target.value)}
          placeholder="Help / Ticker Lookup"
          list="ibkr-symbol-list"
        />
        {search && (
          <button type="button" onClick={() => setSearch("")} aria-label="Clear search">
            <X size={14} />
          </button>
        )}
        <datalist id="ibkr-symbol-list">
          {monitorRows.slice(0, 80).map((row) => <option key={`option-${row.sym}`} value={row.sym} />)}
        </datalist>
      </label>
    );
  }

  function renderMetric(label, value, sub, tone = "") {
    return (
      <div className={`ibkr-metric ${tone}`}>
        <span>{label}</span>
        <strong>{value}</strong>
        {sub && <small>{sub}</small>}
      </div>
    );
  }

  function renderControls() {
    return (
      <div className="ibkr-control-row">
        <button
          type="button"
          className={statusData.paused ? "ibkr-action danger" : "ibkr-action"}
          disabled={Boolean(busy)}
          onClick={() => runControl(statusData.paused ? "Resume" : "Pause", () => (
            statusData.paused ? apiPost("/control/clear_pause") : apiPost("/control/pause")
          ))}
        >
          {statusData.paused ? <Play size={15} /> : <Pause size={15} />}
          {statusData.paused ? "Resume" : "Pause"}
        </button>
        <button
          type="button"
          className={statusData.soft_stop ? "ibkr-action positive" : "ibkr-action warning"}
          disabled={Boolean(busy)}
          onClick={() => runControl(statusData.soft_stop ? "Clear Stop" : "Soft Stop", () => (
            apiPost(`/control/soft_stop?action=${statusData.soft_stop ? "cancel" : "start"}`)
          ))}
        >
          <Power size={15} />
          {statusData.soft_stop ? "Clear Stop" : "Soft Stop"}
        </button>
        <button
          type="button"
          className="ibkr-action"
          disabled={Boolean(busy) || !statusData.drawdown_halted}
          onClick={() => runControl("Reset Drawdown", () => apiPost("/control/reset_drawdown"))}
        >
          <Shield size={15} />
          Reset DD
        </button>
        <button
          type="button"
          className="ibkr-action"
          disabled={Boolean(busy)}
          onClick={() => runControl("Import History", () => apiPost("/control/import_ibkr_history"))}
        >
          <RefreshCw size={15} className={busy === "Import History" ? "spin" : ""} />
          Flex
        </button>
      </div>
    );
  }

  function renderOrderEntry(size = "default") {
    const isBuy = order.side === "BUY";
    return (
      <section className={`ibkr-panel ibkr-order ${size}`}>
        <div className="ibkr-panel-head">
          <strong>ORDER ENTRY</strong>
          <select value={order.symbol} onChange={(event) => setSymbol(event.target.value)}>
            {monitorRows.slice(0, 90).map((row) => <option key={`order-symbol-${row.sym}`} value={row.sym}>{row.sym}</option>)}
          </select>
        </div>
        <div className="ibkr-order-status">
          <span><i className={connected ? "dot on" : "dot"} /> Shortable</span>
          <span><i className={botAlive ? "dot on" : "dot"} /> Consolidated</span>
        </div>
        <div className="ibkr-order-grid">
          <div>
            <span>Position</span>
            <strong>{qty(selectedPosition?.qty || 0)}</strong>
          </div>
          <div>
            <span>Last Price</span>
            <strong>{price(selectedSym, selectedLast, selectedQuote?.market)}</strong>
          </div>
          <div>
            <span>Bid</span>
            <strong>{selectedBid == null ? "-" : price(selectedSym, selectedBid, selectedQuote?.market)}</strong>
          </div>
          <div>
            <span>Ask</span>
            <strong>{selectedAsk == null ? "-" : price(selectedSym, selectedAsk, selectedQuote?.market)}</strong>
          </div>
        </div>
        <div className="ibkr-ticket-row">
          <button type="button" className={`ibkr-side sell ${!isBuy ? "active" : ""}`} onClick={() => setOrder((prev) => ({ ...prev, side: "SELL" }))}>
            SELL
          </button>
          <label>
            <span>QTY</span>
            <div className="ibkr-stepper">
              <button type="button" onClick={() => setOrder((prev) => ({ ...prev, qty: Math.max(1, number(prev.qty) - 100) }))}>-</button>
              <input type="number" value={order.qty} min="1" onChange={(event) => setOrder((prev) => ({ ...prev, qty: event.target.value }))} />
              <button type="button" onClick={() => setOrder((prev) => ({ ...prev, qty: number(prev.qty) + 100 }))}>+</button>
            </div>
          </label>
          <label>
            <span>LMT PRICE</span>
            <input value={order.limitPrice} placeholder="0.00" inputMode="decimal" onChange={(event) => setOrder((prev) => ({ ...prev, limitPrice: event.target.value }))} />
          </label>
          <select value={order.tif} onChange={(event) => setOrder((prev) => ({ ...prev, tif: event.target.value }))}>
            <option>DAY</option>
            <option>GTC</option>
            <option>IOC</option>
          </select>
          <button type="button" className={`ibkr-side buy ${isBuy ? "active" : ""}`} onClick={() => setOrder((prev) => ({ ...prev, side: "BUY" }))}>
            BUY
          </button>
        </div>
        <div className="ibkr-ticket-tools">
          <select value={order.orderType} onChange={(event) => setOrder((prev) => ({ ...prev, orderType: event.target.value }))}>
            <option>LMT</option>
            <option>MKT</option>
            <option>STP</option>
          </select>
          <select value={order.strategy} onChange={(event) => setOrder((prev) => ({ ...prev, strategy: event.target.value }))}>
            <option>Adaptive</option>
            <option>Strategy Builder</option>
            <option>Marketable</option>
          </select>
          <button type="button" className="ibkr-submit" onClick={handleOrderSubmit}>SUBMIT</button>
        </div>
      </section>
    );
  }

  function renderQuotePanel() {
    return (
      <section className="ibkr-panel ibkr-quote-card">
        <div className="ibkr-panel-head">
          <button type="button" className="ibkr-symbol-button" onClick={() => setDesktopView("monitor")}>
            {selectedSym} <ChevronDown size={14} />
          </button>
          <button type="button" className="ibkr-icon-only" onClick={refresh} title="Refresh">
            <RefreshCw size={15} />
          </button>
        </div>
        <div className="ibkr-last">
          <strong>{price(selectedSym, selectedLast, selectedQuote?.market)}</strong>
          <span className={classFor(selectedQuote?.change)}>{number(selectedQuote?.change, 0) >= 0 ? "+" : "-"}{deltaPrice(selectedSym, selectedQuote?.change, selectedQuote?.market)} {pct(selectedQuote?.pctChange)}</span>
        </div>
        <dl>
          <div><dt>Last Size</dt><dd>{qty(selectedPosition?.qty || selectedQuote?.quantity || 0)}</dd></div>
          <div><dt>Bid/Ask</dt><dd>{selectedBid == null ? "-" : price(selectedSym, selectedBid, selectedQuote?.market)} x {selectedAsk == null ? "-" : price(selectedSym, selectedAsk, selectedQuote?.market)}</dd></div>
          <div><dt>Exchange</dt><dd>{selectedQuote?.venue || "-"}</dd></div>
          <div><dt>Hi/Lo</dt><dd>{lastCandle ? `${price(selectedSym, lastCandle.high, selectedQuote?.market)} - ${price(selectedSym, lastCandle.low, selectedQuote?.market)}` : "-"}</dd></div>
          <div><dt>Gate</dt><dd>{selectedQuote?.gate || selectedQuote?.regime || "-"}</dd></div>
        </dl>
      </section>
    );
  }

  function renderChart(height = 360) {
    return (
      <div className="ibkr-chart-panel">
        <div className="ibkr-chart-menu">
          <div>
            <strong>{selectedSym}@SMART</strong>
            <span>3 Months/Daily candles</span>
          </div>
          <div className="ibkr-chart-icons">
            <button type="button" className="ibkr-icon-only" title="Zoom"><Search size={14} /></button>
            <button type="button" className="ibkr-icon-only" title="Maximize"><Maximize2 size={14} /></button>
            <button type="button" className="ibkr-icon-only" title="Settings"><Settings size={14} /></button>
          </div>
        </div>
        <CandleChart
          sym={selectedSym}
          timeframe="15m"
          candles={candles}
          height={height}
          sourceLabel="IBKR bars"
          emptyTitle="No IBKR bars yet"
          emptyDetail={`${selectedSym} has no stored IBKR candles from the current scan universe.`}
          loadingTitle="Loading IBKR chart"
          loadingDetail="Reading the latest stored IBKR candles."
        />
      </div>
    );
  }

  function renderMonitorTable(limit = 12) {
    const rows = filteredRows.slice(0, limit);
    return (
      <div className="ibkr-table-wrap">
        <div className="ibkr-monitor-head">
          <span>FIN INSTRUMENT</span>
          <span>BID</span>
          <span>ASK</span>
          <span>LAST</span>
          <span>CHANGE</span>
          <span>% CHANGE</span>
          <span>VLM</span>
        </div>
        {rows.map((row) => (
          <button type="button" className={`ibkr-monitor-row ${row.sym === selectedSym ? "active" : ""}`} key={`monitor-${row.sym}`} onClick={() => setSymbol(row.sym)}>
            <span><strong>{row.sym}</strong><small>{row.venue}</small></span>
            <span>{row.bid == null ? "-" : price(row.sym, row.bid, row.market)}</span>
            <span>{row.ask == null ? "-" : price(row.sym, row.ask, row.market)}</span>
            <span>{price(row.sym, row.last, row.market)}</span>
            <span className={classFor(row.change)}>{number(row.change, 0) >= 0 ? "+" : "-"}{deltaPrice(row.sym, row.change, row.market)}</span>
            <span className={classFor(row.pctChange)}>{pct(row.pctChange)}</span>
            <span>{row.volume ? compact(row.volume, 1) : "-"}</span>
          </button>
        ))}
        {rows.length === 0 && <div className="ibkr-empty">No rows match the current view.</div>}
      </div>
    );
  }

  function renderPositionsTable(mobile = false) {
    const rows = positions.length ? positions : filteredRows.filter((row) => row.hasPosition);
    if (!rows.length) return <div className="ibkr-empty">No live IBKR positions returned.</div>;
    return (
      <div className={mobile ? "ibkr-mobile-table" : "ibkr-position-table"}>
        <div className="ibkr-position-head">
          <span>Instrument</span>
          <span>Last</span>
          <span>Change</span>
          <span>Position</span>
          <span>P&amp;L</span>
        </div>
        {rows.map((row) => (
          <button type="button" className="ibkr-position-row" key={`pos-${row.sym}`} onClick={() => setSymbol(row.sym)}>
            <span><strong>{row.sym}</strong><small>{row.venue || row.market}</small></span>
            <span>{price(row.sym, row.current || row.last, row.market)}</span>
            <span className={classFor(row.change)}>{number(row.change, 0) >= 0 ? "+" : "-"}{deltaPrice(row.sym, row.change, row.market)}</span>
            <span>{qty(row.qty)}</span>
            <span className={classFor(row.unrealized)}>{signedMoney(row.unrealized)}</span>
          </button>
        ))}
      </div>
    );
  }

  function renderBalances() {
    return (
      <div className="ibkr-balance-list">
        {balances.map((row) => (
          <div className="ibkr-balance-row" key={`balance-${row.currency}`}>
            <span>{row.currency === "TOTAL" ? "Total Cash" : `${row.currency} Cash`}</span>
            <strong>{compact(row.cash, 0)}</strong>
            <small>Market Value</small>
          </div>
        ))}
        {!balances.length && <div className="ibkr-empty">No IBKR cash balances returned.</div>}
      </div>
    );
  }

  function renderOrders() {
    const rows = openOrders.length ? openOrders : cancelledOrders;
    return (
      <div className="ibkr-orders">
        {rows.map((row) => (
          <div className="ibkr-order-row" key={`order-${row.order_id}-${row.status}`}>
            <div>
              <strong>{row.symbol}</strong>
              <small>{row.action} {row.type} {row.role || ""}</small>
            </div>
            <span>{qty(row.quantity || row.remaining)}</span>
            <span>{row.price == null ? "-" : price(row.symbol, row.price, row.market)}</span>
            <span>{row.status}</span>
            {openOrders.length > 0 && (
              <button type="button" className="ibkr-danger-icon" onClick={() => runControl(`Cancel ${row.order_id}`, () => apiPost("/control/broker/cancel_order", { order_id: row.order_id }))}>
                <Trash2 size={14} />
              </button>
            )}
          </div>
        ))}
        {!rows.length && <div className="ibkr-empty">No open IBKR orders.</div>}
      </div>
    );
  }

  function renderImpact() {
    const heat = number(statusData.portfolio_heat, 0);
    const heatCap = number(statusData.portfolio_heat_cap, 14);
    return (
      <div className="ibkr-impact-grid">
        {renderMetric("Portfolio Heat", `${heat.toFixed(2)}%`, `Cap ${heatCap}%`, heat > heatCap ? "neg" : "pos")}
        {renderMetric("Strategy Cap", `$${money(statusData.cap_usd || 5000, 0)}`, "Sizing limit")}
        {renderMetric("Drawdown", `${number(statusData.drawdown_pct, 0).toFixed(2)}%`, `Peak $${money(statusData.peak_equity || 0, 0)}`, statusData.drawdown_halted ? "neg" : "")}
        {renderMetric("Scan State", statusData.scan_state || "waiting", `Next ${countdownLabel(statusData.next_scan_due)}`)}
      </div>
    );
  }

  function renderPortfolioTabs(mobile = false) {
    return (
      <section className={mobile ? "ibkr-mobile-card" : "ibkr-panel"}>
        <div className="ibkr-tabs">
          {PORTFOLIO_TABS.map((tab) => (
            <button type="button" key={tab} className={portfolioTab === tab ? "active" : ""} onClick={() => setPortfolioTab(tab)}>{tab}</button>
          ))}
        </div>
        {portfolioTab === "Positions" && renderPositionsTable(mobile)}
        {portfolioTab === "Balances" && renderBalances()}
        {portfolioTab === "Orders" && renderOrders()}
        {portfolioTab === "Impact Lens" && renderImpact()}
      </section>
    );
  }

  function renderActivityPanel() {
    const rows = trades.slice(0, 7);
    return (
      <section className="ibkr-panel ibkr-activity">
        <div className="ibkr-tabs compact">
          {["ACTIVITY", "Orders", "Trades", "Summary"].map((tab) => <button type="button" key={tab} className={tab === "ACTIVITY" ? "active" : ""}>{tab}</button>)}
        </div>
        <div className="ibkr-activity-head">
          <span></span><span>Key</span><span>Account</span><span>Actn</span><span>Type</span><span>Details</span>
        </div>
        {rows.map((row, index) => (
          <div className="ibkr-activity-row" key={`trade-${row.id || index}`}>
            <span><i className={`dot ${number(row.realized || row.pnl_usd, 0) >= 0 ? "on" : "bad"}`} /> {row.sym || row.symbol || "-"}</span>
            <span>{row.id || index + 1}</span>
            <span>{overview.account_id || "IBKR"}</span>
            <span className={(row.direction || row.dir) === "SELL" ? "neg badge" : "pos badge"}>{row.direction || row.dir || "-"}</span>
            <span>{row.order_type || "LMT"}</span>
            <span>{row.status || row.outcome || signedMoney(row.realized || row.pnl_usd || 0)}</span>
          </div>
        ))}
        {!rows.length && <div className="ibkr-empty">No activity rows today.</div>}
      </section>
    );
  }

  function renderNewsPanel() {
    return (
      <section className="ibkr-panel ibkr-news-panel">
        <div className="ibkr-tabs compact">
          {NEWS_TABS.map((tab) => <button type="button" key={tab} className={newsTab === tab ? "active" : ""} onClick={() => setNewsTab(tab)}>{tab}</button>)}
        </div>
        <div className="ibkr-news-head"><span>TIME-SOURCE-SYMBOL-HEADLINE</span><span>RNK</span></div>
        {news.map((item, index) => (
          <a className="ibkr-news-row" href={item.link || "#"} target="_blank" rel="noreferrer" key={`${item.title}-${index}`}>
            <span>{normalizeNewsTs(item.ts)} {item.publisher || "Market"} - {item.title}</span>
            <ReceiptText size={14} />
          </a>
        ))}
        {!news.length && <div className="ibkr-empty">No market headlines returned.</div>}
      </section>
    );
  }

  function renderRightRail() {
    return (
      <aside className="ibkr-right-rail">
        <section className="ibkr-panel ibkr-monitor">
          <div className="ibkr-tabs compact">
            {MONITOR_TABS.map((tab) => <button type="button" key={tab} className={monitorTab === tab ? "active" : ""} onClick={() => setMonitorTab(tab)}>{tab}</button>)}
            <button type="button" className="plus"><ListFilter size={15} /></button>
          </div>
          {renderMonitorTable(9)}
        </section>
        {renderNewsPanel()}
      </aside>
    );
  }

  function renderDashboardView() {
    return (
      <div className="ibkr-workspace">
        <div className="ibkr-left-stack">
          {renderOrderEntry()}
          <div className="ibkr-chart-row">
            {renderQuotePanel()}
            {renderChart(315)}
          </div>
          {renderActivityPanel()}
        </div>
      </div>
    );
  }

  function renderMarketView() {
    const groups = universe ? Object.entries(universe) : [];
    return (
      <section className="ibkr-panel ibkr-view-panel">
        <div className="ibkr-view-head"><strong>Market Universe</strong><span>{universeSymbols.length} instruments</span></div>
        <div className="ibkr-market-grid">
          {groups.map(([key, group]) => (
            <div className="ibkr-market-card" key={key}>
              <div><strong>{key.replaceAll("_", " ").toUpperCase()}</strong><span>{group.status}</span></div>
              <p>{group.note}</p>
              <div className="ibkr-chip-row">
                {safeArray(group.instruments).map((sym) => <button type="button" key={`${key}-${sym}`} onClick={() => setSymbol(sym)}>{sym}</button>)}
              </div>
            </div>
          ))}
        </div>
      </section>
    );
  }

  function renderReportsView() {
    const perf = statusData.performance || {};
    return (
      <section className="ibkr-panel ibkr-view-panel">
        <div className="ibkr-view-head"><strong>Reports</strong><span>Trades {trades.length}</span></div>
        <div className="ibkr-impact-grid">
          {["today", "week", "month", "all_time"].map((key) => {
            const row = perf[key] || {};
            return renderMetric(key.replace("_", " "), `${signedMoney(row.total_pnl || 0)}`, `${row.total_trades || 0} trades / ${row.win_rate || 0}% WR`, number(row.total_pnl, 0) >= 0 ? "pos" : "neg");
          })}
        </div>
        {renderActivityPanel()}
      </section>
    );
  }

  function renderSettingsView() {
    return (
      <section className="ibkr-panel ibkr-view-panel">
        <div className="ibkr-view-head"><strong>Settings</strong><span>{mode} / IBKR</span></div>
        {renderControls()}
        <div className="ibkr-settings-grid">
          {renderMetric("Polling", "1s", "Dashboard REST refresh")}
          {renderMetric("Scan Cadence", "14m 50s", `Next ${countdownLabel(statusData.next_scan_due)}`)}
          {renderMetric("Heartbeat", ageLabel(statusData.last_heartbeat), botAlive ? "Process alive" : "No recent heartbeat", botAlive ? "pos" : "neg")}
          {renderMetric("Gateway", connected ? "Connected" : "Offline", `Port ${health.ibkr_port || "-"}`, connected ? "pos" : "neg")}
        </div>
      </section>
    );
  }

  function renderDesktopContent() {
    if (desktopView === "dashboard") return renderDashboardView();
    if (desktopView === "order") return <div className="ibkr-workspace split">{renderOrderEntry("large")}{renderOrders()}</div>;
    if (desktopView === "monitor" || desktopView === "watchlist" || desktopView === "options") return <div className="ibkr-workspace">{renderChart(440)}<section className="ibkr-panel">{renderMonitorTable(24)}</section></div>;
    if (desktopView === "portfolio") return <div className="ibkr-workspace">{renderPortfolioTabs(false)}{renderChart(360)}</div>;
    if (desktopView === "news") return <div className="ibkr-workspace">{renderNewsPanel()}</div>;
    if (desktopView === "market") return renderMarketView();
    if (desktopView === "reports") return renderReportsView();
    if (desktopView === "settings") return renderSettingsView();
    return (
      <section className="ibkr-panel ibkr-view-panel">
        <div className="ibkr-view-head"><strong>{DESKTOP_NAV.find(([id]) => id === desktopView)?.[1] || "View"}</strong><span>IBKR dashboard</span></div>
        <div className="ibkr-settings-grid">
          {renderMetric("Connection", connected ? "Connected" : "Offline", "IBKR Gateway", connected ? "pos" : "neg")}
          {renderMetric("Bot", botAlive ? "Alive" : "Waiting", `Heartbeat ${ageLabel(statusData.last_heartbeat)}`, botAlive ? "pos" : "neg")}
          {renderMetric("Mode", mode, "Paper account")}
          {renderMetric("Next Scan", countdownLabel(statusData.next_scan_due), "14m 50s cadence")}
        </div>
      </section>
    );
  }

  function renderDesktop() {
    return (
      <div className="ibkr-desktop">
        <header className="ibkr-topbar">
          <div className="ibkr-top-left">
            {renderBrand()}
            <nav><button>File</button><button>Account</button><button>Help</button></nav>
          </div>
          <div className="ibkr-paper-note">THIS IS NOT A BROKERAGE ACCOUNT - THIS IS A PAPER TRADING ACCOUNT FOR SIMULATED TRADING.</div>
          <div className="ibkr-top-right">
            <span className="ibkr-pro">IBKR PRO</span>
            <span className={connected ? "ibkr-data on" : "ibkr-data"}>DATA</span>
            <span>{overview.account_id || statusData.account_id || "DU179396"}</span>
            <ChevronDown size={14} />
          </div>
        </header>
        <div className="ibkr-toolbar">
          <span><Wifi size={14} /> Contact Us</span>
          {renderSearch()}
          <Link2 size={15} className={connected ? "pos" : ""} />
          <strong>{now.toLocaleTimeString()} <small>GMT+2</small></strong>
        </div>
        <div className="ibkr-desktop-grid">
          <aside className="ibkr-sidebar">
            {DESKTOP_NAV.map(([id, label, Icon]) => (
              <button
                type="button"
                key={id}
                className={desktopView === id ? "active" : ""}
                onClick={() => {
                  if (id === "logout") {
                    sessionToken = "";
                    window.dispatchEvent(new CustomEvent("cipherfx:auth-expired", { detail: { dashboard: "ibkr" } }));
                    return;
                  }
                  setDesktopView(id);
                }}
              >
                <Icon size={17} />
                <span>{label}</span>
              </button>
            ))}
            <div className="ibkr-side-status"><i className={connected ? "dot on" : "dot bad"} /> {connected ? "CONNECTED" : "OFFLINE"}<small>IBKR Gateway</small></div>
          </aside>
          <main className="ibkr-main">{renderDesktopContent()}</main>
          {renderRightRail()}
        </div>
        <footer className="ibkr-footer">
          {renderBrand()}
          {renderMetric("Net Liq", `${money(overview.net_liquidation || statusData.equity || 0, 0)} USD`)}
          {renderMetric("Daily P&L", `${signedMoney(overview.daily_pnl || statusData.daily_pnl || 0)} USD`, "", classFor(overview.daily_pnl || statusData.daily_pnl))}
          {renderMetric("Unrealized P&L", `${signedMoney(overview.unrealized_pnl || 0)} USD`, "", classFor(overview.unrealized_pnl))}
          {renderMetric("Realized P&L", `${signedMoney(overview.realized_pnl || 0)} USD`, "", classFor(overview.realized_pnl))}
          {renderMetric("Available Funds", `${money(overview.available_funds || 0, 0)} USD`)}
          {renderMetric("Buying Power", `${money(overview.buying_power || 0, 0)} USD`)}
          <div className="ibkr-powered"><span>DATA POWERED BY</span><strong>IBKR</strong></div>
          <div className="ibkr-powered"><span>MARKET DATA</span><strong className={connected ? "pos" : "neg"}>{connected ? "LIVE" : "WAIT"}</strong></div>
          <div className="ibkr-powered"><span>ACCOUNT STATUS</span><strong className={connected ? "pos" : "neg"}>{connected ? "ACTIVE" : "OFFLINE"}</strong></div>
        </footer>
      </div>
    );
  }

  function renderMobilePanel() {
    if (mobileView === "home") {
      return (
        <>
          <div className="ibkr-mobile-card">{renderImpact()}</div>
          {renderChart(260)}
        </>
      );
    }
    if (mobileView === "trade") return renderOrderEntry("mobile");
    if (mobileView === "watchlist") return <div className="ibkr-mobile-card">{renderSearch("mobile")}{renderMonitorTable(16)}</div>;
    if (mobileView === "markets") return renderMarketView();
    return renderPortfolioTabs(true);
  }

  function renderMobile() {
    return (
      <div className="ibkr-mobile-wrap">
        <div className="ibkr-phone">
          <div className="ibkr-phone-status"><span>9:41</span><span>100</span></div>
          <div className="ibkr-mobile-head">
            <button className="ibkr-icon-only" type="button" onClick={() => setMobileView("markets")}><Menu size={24} /></button>
            {renderBrand()}
            <div className="ibkr-mobile-icons">
              <Search size={24} />
              <Bell size={23} />
              <MoreHorizontal size={24} />
            </div>
          </div>
          <main className="ibkr-mobile-screen">
            <section className="ibkr-mobile-hero">
              <div>
                <span>Net Liquidation Value</span>
                <strong>{money(overview.net_liquidation || statusData.equity || 0, 0)}</strong>
              </div>
              <div>
                <span>Daily P&amp;L</span>
                <strong className={classFor(overview.daily_pnl || statusData.daily_pnl)}>{money(overview.daily_pnl || statusData.daily_pnl || 0, 0)} <small>{pct((number(overview.daily_pnl || statusData.daily_pnl, 0) / Math.max(number(overview.net_liquidation || statusData.equity, 1), 1)) * 100)}</small></strong>
              </div>
              <button type="button"><ChevronDown size={20} /></button>
            </section>
            <section className="ibkr-mobile-metrics">
              {renderMetric("UNREALIZED P&L", money(overview.unrealized_pnl || 0, 0), "", classFor(overview.unrealized_pnl))}
              {renderMetric("MKT VAL", money(overview.market_value || 0, 0))}
              {renderMetric("EXCESS LIQ", money(overview.excess_liquidity || 0, 0))}
              {renderMetric("SMA", money(overview.sma || 0, 1))}
              {renderMetric("REALIZED P&L", money(overview.realized_pnl || 0, 0), "", classFor(overview.realized_pnl))}
              {renderMetric("MAINT MARGIN", money(overview.maint_margin || 0, 0))}
              {renderMetric("BUYING POWER", money(overview.buying_power || 0, 0))}
              {renderMetric("SPX DELTA", compact(statusData.portfolio_heat || 0, 3))}
            </section>
            {renderMobilePanel()}
            <section className="ibkr-mobile-quote">
              <div className="ibkr-bidask">
                <span>BID</span>
                <strong>{selectedBid == null ? "-" : price(selectedSym, selectedBid, selectedQuote?.market)}</strong>
                <strong className="neg">{price(selectedSym, selectedLast, selectedQuote?.market)}</strong>
                <span>ASK</span>
              </div>
              {renderChart(210)}
            </section>
            <section className="ibkr-mobile-card">
              <div className="ibkr-panel-head"><strong>Cash Balances</strong></div>
              {renderBalances()}
            </section>
            <div className="ibkr-mobile-powered">Data powered by <strong>IBKR</strong></div>
          </main>
          <nav className="ibkr-mobile-nav">
            {MOBILE_NAV.map(([id, label, Icon]) => (
              <button type="button" key={id} className={mobileView === id ? "active" : ""} onClick={() => setMobileView(id)}>
                <Icon size={24} />
                <span>{label}</span>
              </button>
            ))}
          </nav>
        </div>
      </div>
    );
  }

  return (
    <div className="ibkr-shell">
      {(apiError || notice || busy) && (
        <div className={`ibkr-toast ${apiError ? "bad" : ""}`}>
          {busy && <RefreshCw size={14} className="spin" />}
          {apiError && <AlertTriangle size={14} />}
          <span>{apiError || notice || busy}</span>
        </div>
      )}
      {renderDesktop()}
      {renderMobile()}
    </div>
  );
}

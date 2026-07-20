import React, { useCallback, useEffect, useMemo, useRef, useState } from "react";
import {
  Activity,
  BarChart3,
  Briefcase,
  CheckCircle2,
  ChevronDown,
  ChevronRight,
  Clock3,
  Database,
  FileText,
  History,
  LayoutDashboard,
  LineChart,
  List,
  LogOut,
  RefreshCw,
  Search,
  Sparkles,
  TrendingDown,
  TrendingUp,
  Wifi,
  XCircle,
  Zap,
} from "lucide-react";
import logoSrc from "./cipherfx-icon.png";
import CandleChart from "./CandleChart";
import TradeReplayChart from "./TradeReplayChart";

function apiBase() {
  try {
    const live = window.localStorage?.getItem("cipherfx_mt5_mode") === "live";
    return window.location.origin + "/" + (live ? "mt5-live-api" : "mt5-api");
  } catch {
    return window.location.origin + "/mt5-api";
  }
}
const TZ = "Africa/Johannesburg";
const TZ_LABEL = "SAST";

const TABS = [
  { id: "snapshot", label: "Snapshot", short: "Snapshot", icon: LayoutDashboard },
  { id: "market", label: "Market", short: "Market", icon: TrendingUp },
  { id: "chart", label: "Chart", short: "Chart", icon: BarChart3 },
  { id: "replay", label: "Replay", short: "Replay", icon: History },
  { id: "scanner", label: "Scan", short: "Scan", icon: Activity },
  { id: "intelligence", label: "Intel", short: "Intel", icon: Sparkles },
  { id: "trade", label: "Trade", short: "Trade", icon: Briefcase },
  { id: "history", label: "History", short: "History", icon: Clock3 },
];

const TIMEFRAMES = ["M1", "M3", "M5", "M15", "M30", "H1", "H4", "D1", "W1", "MN1"];
const ENGINE_ORDER = ["Forex", "Indices", "Metals"];

function token() {
  try {
    return window.localStorage?.getItem("cipherfx_mt5_token") || "";
  } catch {
    return "";
  }
}

async function apiFetch(path, options = {}) {
  const endpoint = path.replace(/^\/api/, "");
  const response = await fetch(apiBase() + endpoint, {
    cache: "no-store",
    ...options,
    headers: {
      Accept: "application/json",
      ...(options.headers || {}),
      Authorization: `Bearer ${token()}`,
    },
  });
  if (response.status === 401) {
    window.dispatchEvent(new Event("cipherfx:auth-expired"));
    throw new Error("Session expired");
  }
  if (!response.ok) {
    throw new Error((await response.text().catch(() => "")) || `Request failed: ${response.status}`);
  }
  return response.json();
}

function finite(value, fallback = null) {
  const numeric = Number(value);
  return Number.isFinite(numeric) ? numeric : fallback;
}

function money(value, currency = "USD") {
  const numeric = finite(value, null);
  if (numeric === null) return "--";
  const sign = numeric >= 0 ? "+" : "-";
  return `${sign}$${Math.abs(numeric).toLocaleString("en-US", { minimumFractionDigits: 2, maximumFractionDigits: 2 })}${currency === "USD" ? "" : ` ${currency}`}`;
}

function plainMoney(value, currency = "USD") {
  const numeric = finite(value, null);
  if (numeric === null) return "--";
  return `$${numeric.toLocaleString("en-US", { minimumFractionDigits: 2, maximumFractionDigits: 2 })}${currency === "USD" ? "" : ` ${currency}`}`;
}

function zarMoney(value) {
  const numeric = finite(value, null);
  if (numeric === null) return "--";
  return `R ${numeric.toLocaleString("en-ZA", { minimumFractionDigits: 2, maximumFractionDigits: 2 })}`;
}

function number(value, digits = 2) {
  const numeric = finite(value, null);
  return numeric === null ? "--" : numeric.toFixed(digits);
}

function percent(value) {
  const numeric = finite(value, null);
  return numeric === null ? "--" : `${numeric.toFixed(1)}%`;
}

function price(value, symbol = "") {
  const numeric = finite(value, null);
  if (numeric === null) return "--";
  const digits = String(symbol).toUpperCase().includes("JPY") ? 3 : numeric > 1000 ? 2 : numeric > 10 ? 3 : 5;
  return numeric.toFixed(digits);
}

function parseDate(value) {
  if (!value) return null;
  if (value instanceof Date) return Number.isNaN(value.getTime()) ? null : value;
  const raw = String(value).trim();
  if (/^\d+(\.\d+)?$/.test(raw)) {
    const n = Number(raw);
    return new Date(n > 1e12 ? n : n * 1000);
  }
  const normalized = raw.replace(" ", "T");
  const parsed = new Date(/(?:Z|[+-]\d{2}:?\d{2})$/i.test(normalized) ? normalized : `${normalized}Z`);
  return Number.isNaN(parsed.getTime()) ? null : parsed;
}

function dateLabel(value, withSeconds = false) {
  const date = parseDate(value);
  if (!date) return "--";
  return date.toLocaleString("en-GB", {
    timeZone: TZ,
    day: "2-digit",
    month: "short",
    year: "numeric",
    hour: "2-digit",
    minute: "2-digit",
    ...(withSeconds ? { second: "2-digit" } : {}),
    hour12: false,
  }) + ` ${TZ_LABEL}`;
}

function age(value) {
  const date = parseDate(value);
  if (!date) return "--";
  const seconds = Math.max(0, Math.round((Date.now() - date.getTime()) / 1000));
  if (seconds < 60) return `${seconds}s ago`;
  if (seconds < 3600) return `${Math.floor(seconds / 60)}m ago`;
  return `${Math.floor(seconds / 3600)}h ago`;
}

function engineFor(symbol, explicit = "") {
  const text = `${explicit} ${symbol}`.toUpperCase();
  if (/(XAU|XAG|GOLD|SILVER)/.test(text)) return "Metals";
  if (/(US30|NAS100|SPX500|GER40|UK100|FRA40|EU50|JP225|DAX|USTEC)/.test(text)) return "Indices";
  return "Forex";
}

function engineIcon(engine) {
  return engine === "Metals" ? "M" : engine === "Indices" ? "I" : "$";
}

function engineClass(engine) {
  return String(engine || "Forex").toLowerCase();
}

function scoreOf(row) {
  return finite(row?.final_score ?? row?.score ?? row?.total_score ?? row?.score_breakdown?.total, null);
}

function directionOf(row) {
  const direction = String(row?.direction || row?.side || "").toUpperCase();
  return direction === "BUY" || direction === "SELL" ? direction : "WAIT";
}

function reasonOf(row) {
  if (Array.isArray(row?.reasons) && row.reasons.length) return row.reasons.join("; ");
  if (row?.reason) return String(row.reason);
  if (row?.scan_story) return String(row.scan_story);
  return directionOf(row) === "WAIT" ? "No current proposal" : "Live proposal path";
}

function planStatusLabel(row) {
  const status = String(row?.status || "").toUpperCase();
  if (status === "PLANNED") return "Candidate - live recheck";
  if (status === "WATCHING") return "Watching for setup";
  return "Waiting for snapshot";
}

function planReasonLabel(row) {
  const reason = String(row?.reason || "");
  if (reason === "LEARNING_CANDIDATE_FROM_CACHED_SNAPSHOT") return "Candidate from latest stored market snapshot";
  if (reason === "NO_CURRENT_QUALIFICATION") return "No current candidate in the stored snapshot";
  if (reason === "NO_CACHED_SNAPSHOT") return "No stored snapshot yet";
  return reason || "No planning reason recorded";
}

function dashboardSymbols(data) {
  const seen = new Map();
  const add = (row) => {
    const symbol = String(row?.symbol || row?.sym || "").trim().toUpperCase();
    if (symbol && !seen.has(symbol)) seen.set(symbol, { ...row, symbol });
  };
  (data.symbols || []).forEach(add);
  (data.scanner?.current_signals || []).forEach(add);
  (data.live?.ticks || []).forEach(add);
  return [...seen.values()];
}

function useLiveData() {
  const [data, setData] = useState({
    status: {}, activity: {}, scanner: {}, intelligence: {}, portfolio: {},
    marketHours: {}, symbols: [], live: {}, positions: [], orders: {}, history: { today: {}, week: {} },
    intelligenceWatch: { overall: "UNKNOWN", findings: [] },
    regime: { overall: "UNKNOWN", counts: {}, symbols: [] },
  });
  const [error, setError] = useState("");
  const [refreshing, setRefreshing] = useState(false);
  const [lastRefresh, setLastRefresh] = useState(null);

  const refresh = useCallback(async (quiet = false) => {
    if (!quiet) setRefreshing(true);
    try {
      const core = await Promise.allSettled([
        apiFetch("/api/status"),
        apiFetch("/api/broker/activity"),
        apiFetch("/api/scanner"),
        apiFetch("/api/intelligence/overview"),
        apiFetch("/api/portfolio/overview"),
        apiFetch("/api/market-hours"),
        apiFetch("/api/symbols"),
        apiFetch("/api/positions"),
        apiFetch("/api/orders"),
        apiFetch("/api/deals?period=today\&limit=500"),
        apiFetch("/api/deals?period=week\&limit=500"),
        apiFetch("/api/intelligence/watch"),
        apiFetch("/api/market/regime"),
      ]);
      const get = (index, fallback) => core[index]?.status === "fulfilled" ? core[index].value : fallback;
      const symbols = get(6, []);
      const symbolList = Array.isArray(symbols) ? symbols.map((row) => row.symbol || row.sym).filter(Boolean) : [];
      let live = {};
      if (symbolList.length) {
        try {
          live = await apiFetch(`/api/live/market?symbols=${encodeURIComponent(symbolList.join(","))}`);
        } catch {
          live = {};
        }
      }
      setData({
        status: get(0, {}),
        activity: get(1, {}),
        scanner: get(2, {}),
        intelligence: get(3, {}),
        portfolio: get(4, {}),
        marketHours: get(5, {}),
        symbols,
        live,
        positions: get(7, []),
        orders: get(8, []),
        history: { today: get(9, {}), week: get(10, {}) },
        intelligenceWatch: get(11, { overall: "UNKNOWN", findings: [] }),
        regime: get(12, { overall: "UNKNOWN", counts: {}, symbols: [] }),
      });
      setError("");
      setLastRefresh(new Date());
    } catch (err) {
      if (!String(err?.message || "").includes("Session expired")) setError(err?.message || "Live dashboard data unavailable");
    } finally {
      if (!quiet) setRefreshing(false);
    }
  }, []);

  useEffect(() => {
    refresh();
    const interval = window.setInterval(() => refresh(true), 5000);
    return () => window.clearInterval(interval);
  }, [refresh]);

  useEffect(() => {
    let cancelled = false;
    const refreshPositions = async () => {
      try {
        const [positions, orders] = await Promise.all([
          apiFetch("/api/positions"),
          apiFetch("/api/orders"),
        ]);
        if (!cancelled) {
          setData((previous) => ({ ...previous, positions, orders }));
        }
      } catch {
        // The main live refresh owns connection errors; keep the last broker snapshot visible.
      }
    };
    const interval = window.setInterval(refreshPositions, 1000);
    refreshPositions();
    return () => {
      cancelled = true;
      window.clearInterval(interval);
    };
  }, []);

  return { data, error, refreshing, lastRefresh, refresh };
}

function useHistory(period) {
  const [state, setState] = useState({ today: null, week: null, month: null, all: null });
  useEffect(() => {
    let cancelled = false;
    const periods = period === "all" ? ["all"] : [period];
    Promise.all(periods.map((item) => apiFetch(`/api/deals?period=${item}&limit=500`).catch(() => null)))
      .then((results) => {
        if (cancelled) return;
        setState((previous) => ({ ...previous, [period]: results[0] }));
      });
    return () => { cancelled = true; };
  }, [period]);
  return state[period];
}

function liveTickTimeSeconds(tick) {
  const raw = tick?.time;
  if (typeof raw === "number" && Number.isFinite(raw)) return raw > 1e12 ? raw / 1000 : raw;
  const parsed = Date.parse(String(raw || ""));
  return Number.isFinite(parsed) ? parsed / 1000 : null;
}

function timeframeSeconds(timeframe) {
  return ({
    M1: 60,
    M3: 180,
    M5: 300,
    M15: 900,
    M30: 1800,
    H1: 3600,
    H4: 14400,
    D1: 86400,
    W1: 604800,
    MN1: 2592000,
  })[String(timeframe || "").toUpperCase()] || 900;
}

function toTime(value) {
  const raw = String(value ?? "").trim();
  if (!raw) return null;
  if (/^\d+$/.test(raw)) {
    const numeric = Number(raw);
    if (!Number.isFinite(numeric)) return null;
    return numeric > 1e12 ? Math.floor(numeric / 1000) : numeric;
  }
  const normalized = raw.replace(" ", "T");
  const parsed = Math.floor(new Date(/(?:Z|[+-]\d{2}:?\d{2})$/i.test(normalized) ? normalized : `${normalized}Z`).getTime() / 1000);
  return Number.isFinite(parsed) ? parsed : null;
}

function overlayLiveTick(rows, tick, timeframe) {
  if (!Array.isArray(rows) || !rows.length || !tick || tick.status !== "LIVE_DATA" || tick.source !== "websocket") {
    return rows;
  }
  const bid = Number(tick.bid);
  const ask = Number(tick.ask);
  const price = (bid + ask) / 2;
  const tickSeconds = liveTickTimeSeconds(tick);
  if (!Number.isFinite(price) || price <= 0 || !Number.isFinite(tickSeconds)) return rows;

  const current = rows[rows.length - 1];
  const currentSeconds = toTime(current.ts ?? current.time);
  const step = timeframeSeconds(timeframe);
  const bucket = Math.floor(tickSeconds / step) * step;
  if (Number.isFinite(currentSeconds) && bucket > currentSeconds) {
    return [
      ...rows,
      {
        ts: new Date(bucket * 1000).toISOString(),
        open: price,
        high: price,
        low: price,
        close: price,
        volume: 0,
        source: "websocket",
        tick_time: tick.time,
        quote_age_seconds: tick.age_seconds,
        fresh: true,
      },
    ];
  }

  const updated = {
    ...current,
    high: Math.max(Number(current.high), price),
    low: Math.min(Number(current.low), price),
    close: price,
    source: "websocket",
    tick_time: tick.time,
    quote_age_seconds: tick.age_seconds,
    fresh: true,
  };
  return [...rows.slice(0, -1), updated];
}

function useChart(symbol, timeframe, enabled) {
  const [state, setState] = useState({ candles: [], trades: [], loading: false, source: "", liveTick: null });
  const latestTickRef = useRef(null);

  useEffect(() => {
    if (!enabled || !symbol) return undefined;
    let cancelled = false;

    const loadHistory = async () => {
      try {
        const [candles, replay] = await Promise.all([
          apiFetch(`/api/candles?sym=${encodeURIComponent(symbol)}&timeframe=${timeframe}&limit=180`),
          apiFetch(`/api/replay?sym=${encodeURIComponent(symbol)}&timeframe=${timeframe}&limit=500`).catch(() => ({})),
        ]);
        if (!cancelled) {
          const historyRows = Array.isArray(candles) ? candles : [];
          const liveRows = overlayLiveTick(historyRows, latestTickRef.current, timeframe);
          setState((previous) => ({
            ...previous,
            candles: liveRows,
            trades: Array.isArray(replay?.trades) ? replay.trades : previous.trades,
            source: latestTickRef.current?.source || candles?.[candles.length - 1]?.source || replay?.source || "",
            loading: false,
          }));
        }
      } catch {
        if (!cancelled) {
          setState((previous) => ({ ...previous, loading: false, source: "websocket" }));
        }
      }
    };

    const loadLiveTick = async () => {
      try {
        const payload = await apiFetch(`/api/live/market?symbols=${encodeURIComponent(symbol)}`);
        const tick = (Array.isArray(payload?.ticks) ? payload.ticks : []).find(
          (row) => row?.status === "LIVE_DATA" && row?.source === "websocket",
        );
        if (!cancelled && tick) {
          latestTickRef.current = tick;
          setState((previous) => ({
            ...previous,
            candles: overlayLiveTick(previous.candles, tick, timeframe),
            liveTick: tick,
            source: "websocket",
          }));
        }
      } catch {
        // Keep the last genuine WebSocket tick visible; never replace it with fallback data.
      }
    };

    latestTickRef.current = null;
    setState({ candles: [], trades: [], loading: true, source: "websocket", liveTick: null });
    loadHistory();
    loadLiveTick();
    const historyInterval = window.setInterval(loadHistory, 2000);
    const tickInterval = window.setInterval(loadLiveTick, 250);
    return () => {
      cancelled = true;
      window.clearInterval(historyInterval);
      window.clearInterval(tickInterval);
    };
  }, [symbol, timeframe, enabled]);

  return state;
}

function Metric({ label, value, sub, tone = "" }) {
  return (
    <div className="metric">
      <span className="eyebrow">{label}</span>
      <strong className={tone}>{value}</strong>
      <small>{sub || "--"}</small>
    </div>
  );
}

function Panel({ title, detail, action, children, className = "" }) {
  return (
    <section className={`panel ${className}`}>
      {(title || detail || action) && (
        <div className="panel-head">
          <div>
            {title && <h2>{title}</h2>}
            {detail && <p>{detail}</p>}
          </div>
          {action}
        </div>
      )}
      {children}
    </section>
  );
}

function StatusDot({ live = false }) {
  return <span className={`status-dot ${live ? "live" : "off"}`} aria-hidden="true" />;
}

function ControlRoom({ status, lastRefresh }) {
  const connected = Boolean(status?.mt5_connected);
  const liveMode = String(status?.mode || "DEMO").toUpperCase() === "LIVE";
  const modeLabel = liveMode ? "LIVE MODE" : "DEMO MODE";
  const feed = status?.market_feed || {};
  return (
    <div className="control-room">
      <div className="control-mark"><Zap size={27} /></div>
      <div className="control-copy">
        <strong>TRADING CONTROL ROOM</strong>
        <span>Real-time broker, market and strategy status.</span>
      </div>
      <div className="control-state">
        <span><StatusDot live={connected} /> MT5 {modeLabel} - {connected ? "CONNECTED" : "OFFLINE"}</span>
        <small>{dateLabel(lastRefresh, true)}</small>
        <small>{feed.source === "websocket" ? "Online - live market data" : "Market feed unavailable"}</small>
      </div>
    </div>
  );
}

function SessionGrid({ marketHours }) {
  const sessions = Array.isArray(marketHours?.sessions) ? marketHours.sessions : [];
  return (
    <div className="session-grid">
      {sessions.slice(0, 4).map((session) => {
        const open = Boolean(session.open) || String(session.status).toUpperCase() === "OPEN";
        const label = session.label || session.name || session.market || "Market";
        const window = session.hours_sast || session.window || session.hours || [session.open_sast, session.close_sast].filter(Boolean).join(" - ") || [session.start, session.end].filter(Boolean).join(" - ") || "--";
        return (
          <div className="session-card" key={label}>
            <StatusDot live={open} />
            <div>
              <strong>{label}</strong>
              <span className={open ? "good" : "bad"}>{open ? "OPEN" : "CLOSED"}</span>
              <small>{window} {window !== "--" ? TZ_LABEL : ""}</small>
            </div>
          </div>
        );
      })}
      {!sessions.length && <div className="empty-line">Market session data is loading.</div>}
    </div>
  );
}

function EngineRow({ engine, rows, onSelect }) {
  const liveRows = rows.filter((row) => directionOf(row) !== "WAIT");
  const ready = rows.filter((row) => row.gate_ok || String(row.status).includes("PROPOSAL"));
  return (
    <button type="button" className="engine-row" onClick={() => onSelect(rows[0]?.symbol || "")}>
      <span className={`engine-icon ${engineClass(engine)}`}>{engineIcon(engine)}</span>
      <span className="engine-copy">
        <strong>{engine}</strong>
        <small>{rows.length} symbols monitored - {liveRows.length} directional readings</small>
      </span>
      <span className={ready.length ? "good" : "muted"}>{ready.length ? `${ready.length} READY` : "OBSERVING"}</span>
      <ChevronRight size={17} />
    </button>
  );
}

function SnapshotTab({ data, lastRefresh, onTab, onSelect }) {
  const status = data.status || {};
  const activity = data.activity || {};
  const overview = activity.overview || {};
  const portfolio = data.portfolio || {};
  const health = portfolio.portfolio_health || {};
  const feed = status.market_feed || {};
  const scannerRows = data.scanner?.current_signals || [];
  const grouped = ENGINE_ORDER.reduce((acc, engine) => {
    acc[engine] = scannerRows.filter((row) => engineFor(row.symbol, row.engine || row.asset_class) === engine);
    return acc;
  }, {});
  const performance = data.intelligence?.execution?.decision_counts || {};
  const summaries = data.history || {};
  const daily = summaries.today?.summary || {};
  const weekly = summaries.week?.summary || {};
  return (
    <>
      <div className="metrics-strip">
        <Metric label="Open P/L" value={money(overview.unrealized_pnl)} sub={<><span>{number(overview.unrealized_pnl, 2)} USD floating</span><span className="metric-subzar">{zarMoney(overview.unrealized_pnl_zar)}</span></>} tone={finite(overview.unrealized_pnl, 0) >= 0 ? "blue" : "red"} />
        <Metric
          label="Equity"
          value={plainMoney(overview.net_liquidation || status.equity)}
          sub={<><span>{status.account_id ? `Account ${status.account_id} · USD` : "MT5 account · USD"}</span><span className="metric-subrate">USD/ZAR {number(overview.usd_zar_rate, 4)}</span><span className="metric-subzar">{zarMoney(overview.equity_zar)}</span></>}
        />
        <Metric label="Free margin" value={plainMoney(overview.available_funds)} sub={overview.available_funds !== undefined ? "Available broker margin" : "Waiting for MT5"} tone="green" />
        <Metric label="Today's P/L" value={money(status.daily_pnl)} sub={`${status.daily_trade_count || 0} accepted entries`} tone={finite(status.daily_pnl, 0) >= 0 ? "blue" : "red"} />
      </div>
      <ControlRoom status={status} lastRefresh={lastRefresh} />
      <div className="grid-two">
        <Panel title="System status" detail="Live components and freshness">
          <div className="status-list">
            <StatusLine icon={Wifi} label="Broker connection" value={status.mt5_connected ? "Connected" : "Offline"} live={status.mt5_connected} />
            <StatusLine icon={Activity} label="Market data" value={feed.weekend_closed ? "Market closed (weekend)" : feed.source === "websocket" && (feed.fresh_symbols || 0) > 0 ? `${feed.fresh_symbols || 0}/${feed.tracked_symbols || 0} live` : "Stale or unavailable"} live={feed.source === "websocket" && (feed.fresh_symbols || 0) >= (feed.tracked_symbols || 1) * 0.5} />
            <StatusLine icon={Sparkles} label="Learning engines" value={scannerRows.length ? "Running" : "Waiting for data"} live={Boolean(scannerRows.length)} />
            <StatusLine icon={Database} label="Database" value="Healthy" live />
            <StatusLine icon={Clock3} label="Last refresh" value={age(lastRefresh)} live={Boolean(lastRefresh)} />
          </div>
          <div className={`system-banner ${status.mt5_connected ? "ok" : "warn"}`}>
            {status.mt5_connected ? <CheckCircle2 size={17} /> : <XCircle size={17} />}
            <span>{status.mt5_connected ? "Live MT5 state is connected" : "Waiting for the MT5 bridge"}</span>
          </div>
        </Panel>
        <Panel title="Market scanner" detail={`${feed.tracked_symbols || data.symbols.length || 0} symbols`} action={<button className="link-button" type="button" onClick={() => onTab("scanner")}>Open scan <ChevronRight size={16} /></button>}>
          <div className="engine-list">
            {ENGINE_ORDER.map((engine) => <EngineRow key={engine} engine={engine} rows={grouped[engine]} onSelect={(symbol) => { onSelect(symbol); onTab("scanner"); }} />)}
          </div>
        </Panel>
      </div>
      <div className="grid-two">
        <Panel title="Live account" detail="MT5 positions only" action={<button className="link-button" type="button" onClick={() => onTab("trade")}>View positions <ChevronRight size={16} /></button>}>
          <div className="account-grid">
            <DataPair label="Equity" value={plainMoney(overview.net_liquidation)} />
            <DataPair label="Open P/L" value={money(overview.unrealized_pnl)} sub={<><span>{number(overview.unrealized_pnl, 2)} USD floating</span><span className="metric-subzar">{zarMoney(overview.unrealized_pnl_zar)}</span></>} tone={finite(overview.unrealized_pnl, 0) >= 0 ? "blue" : "red"} />
            <DataPair label="Open trades" value={status.live_position_count ?? data.positions.length} />
            <DataPair label="Free margin" value={plainMoney(overview.available_funds)} tone="green" />
          </div>
        </Panel>
        <Panel title="Performance" detail="Closed MT5 history" action={<button className="link-button" type="button" onClick={() => onTab("history")}>View history <ChevronRight size={16} /></button>}>
          <div className="performance-grid">
            <DataPair label="Today" value={`${daily.total_trades ?? daily.closed_trades ?? 0} trades`} sub={plainMoney(daily.pnl ?? daily.realized_pnl ?? 0)} tone={finite(daily.pnl ?? daily.realized_pnl, 0) >= 0 ? "blue" : "red"} />
            <DataPair label="Today win rate" value={percent(daily.win_rate)} sub={`${daily.wins ?? 0} wins / ${daily.losses ?? 0} losses`} tone="green" />
            <DataPair label="Week" value={`${weekly.total_trades ?? weekly.closed_trades ?? 0} trades`} sub={plainMoney(weekly.pnl ?? weekly.realized_pnl ?? 0)} />
            <DataPair label="Week win rate" value={percent(weekly.win_rate)} sub={`${weekly.wins ?? 0} wins / ${weekly.losses ?? 0} losses`} tone="green" />
            <DataPair label="Portfolio health" value={health.label || "--"} sub={health.risk_status || "Waiting"} tone={health.risk_status === "NORMAL" ? "green" : "red"} />
            <DataPair label="Execution records" value={Object.values(performance).reduce((sum, value) => sum + Number(value || 0), 0) || "--"} sub="Decision events" />
          </div>
        </Panel>
      </div>
      <Panel title="How the system reads the market" detail="One clear story from data to execution">
        <div className="flow-strip">
          {["MT5 data", "10-frame snapshot", "Forex / Indices / Metals engine", "Immutable proposal", "Operational execution"].map((item, index) => (
            <React.Fragment key={item}><span>{item}</span>{index < 4 && <ChevronRight size={15} />}</React.Fragment>
          ))}
        </div>
        <div className="story-box">
          <Sparkles size={18} />
          <span>Learning engines determine whether a proposal exists. Execution checks broker conditions and the portfolio view reports health only; it does not approve or block trades.</span>
        </div>
      </Panel>
    </>
  );
}

function StatusLine({ icon: Icon, label, value, live }) {
  return <div className="status-line"><Icon size={20} /><span>{label}</span><strong className={live ? "good" : "bad"}><StatusDot live={live} />{value}</strong></div>;
}

function DataPair({ label, value, sub, tone = "" }) {
  return <div className="data-pair"><span>{label}</span><strong className={tone}>{value}</strong>{sub && <small>{sub}</small>}</div>;
}

function regimeTone(label) {
  return label === "TRENDING" ? "green" : label === "CHOPPY" ? "red" : label === "TRANSITIONAL" ? "amber" : "muted";
}

function MarketTab({ data, onSelect }) {
  const symbols = dashboardSymbols(data);
  const ticks = Array.isArray(data.live?.ticks) ? data.live.ticks : [];
  const ticksBySymbol = new Map(ticks.map((row) => [String(row.symbol || row.sym || "").toUpperCase(), row]));
  const regime = data.regime || {};
  const regimeBySymbol = new Map((Array.isArray(regime.symbols) ? regime.symbols : []).map((r) => [String(r.symbol || "").toUpperCase(), r]));
  const counts = regime.counts || {};
  const overall = regime.overall || "UNKNOWN";
  const regimeNote = overall === "CHOPPY"
    ? "Choppy / ranging market - the bot stays selective and takes few entries by design. Fewer trades here is correct, not a fault."
    : overall === "TRENDING"
      ? "Trending market - conditions the strategy is built for. More setups qualify when a clean trend is present."
      : overall === "TRANSITIONAL"
        ? "Mixed / transitional market - some symbols trending, some choppy. The bot trades only the ones with a clean trend."
        : "Market regime is being measured.";
  const rows = symbols.map((base) => ({
    ...base,
    ...(ticksBySymbol.get(String(base.symbol || "").toUpperCase()) || {
      symbol: base.symbol,
      status: "STALE_MARKET_DATA",
      fresh: false,
    }),
  }));
  return (
    <>
      <Panel title="Live market" detail="Live MT5 prices. Quote age is shown per symbol." action={<span className="source-badge"><StatusDot live /> Online</span>}>
        <div className="market-summary">
          <Metric label="Live symbols" value={data.status?.fresh_symbols !== undefined ? `${data.status.fresh_symbols}/${data.status.tracked_symbols ?? rows.length}` : "--"} sub="Fresh MT5 ticks" tone="green" />
          <Metric label="Stale symbols" value={data.status?.stale_symbols ? data.status.stale_symbols.length : "--"} sub="Last-known price still shown" tone={(data.status?.stale_symbols?.length || 0) > (data.status?.tracked_symbols || 1) * 0.5 ? "red" : (data.status?.stale_symbols?.length || 0) > 0 ? "amber" : "green"} />
          <Metric label="Market regime" value={overall} sub={`${counts.TRENDING || 0} trending / ${counts.TRANSITIONAL || 0} mixed / ${counts.CHOPPY || 0} choppy`} tone={regimeTone(overall)} />
        </div>
        <div className="story-box"><Activity size={17} /> {regimeNote}</div>
      </Panel>
      <Panel title="Market watch" detail="Bid/ask, spread, and per-symbol trend regime (Choppiness Index).">
        <div className="quote-table">
          <div className="table-head"><span>Symbol</span><span>Engine</span><span>Bid / Ask</span><span>Spread</span><span>Regime</span><span>Feed</span></div>
          {rows.map((row) => {
            const symbol = row.symbol || row.sym;
            const engine = engineFor(symbol, row.engine || row.asset_class);
            const live = row.status === "LIVE_DATA" || row.fresh;
            const hasQuote = Number.isFinite(finite(row.bid, null)) && Number.isFinite(finite(row.ask, null));
            const sr = regimeBySymbol.get(String(symbol).toUpperCase()) || {};
            return (
              <button className="table-row" type="button" key={symbol} onClick={() => onSelect(symbol)}>
                <span className="symbol-cell"><strong>{symbol}</strong><small>{engine}</small></span>
                <span><span className={`engine-tag ${engineClass(engine)}`}>{engine}</span></span>
                <span>{hasQuote ? `${price(row.bid, symbol)} / ${price(row.ask, symbol)}` : "-- / --"}</span>
                <span>{hasQuote ? number(row.spread, String(symbol).includes("JPY") ? 3 : 5) : "--"}</span>
                <span className={regimeTone(sr.label)}>{sr.label ? `${sr.label === "TRANSITIONAL" ? "MIXED" : sr.label}${sr.choppiness != null ? ` ${sr.choppiness}` : ""}` : "--"}</span>
                <span className={live ? "good" : "bad"}><StatusDot live={live} /> {live ? `${number(row.age_seconds, 1)}s` : hasQuote ? "Market closed" : "Waiting"}</span>
              </button>
            );
          })}
          {!rows.length && <div className="empty-line">No MT5 symbols have been received yet.</div>}
        </div>
      </Panel>
    </>
  );
}

function ChartTab({ symbol, setSymbol, timeframe, setTimeframe, data, chart }) {
  const symbols = dashboardSymbols(data).map((row) => row.symbol).filter(Boolean);
  const liveTick = chart.liveTick || data.live?.ticks?.find((row) => row.symbol === symbol);
  const marketClosed = chart.candles.length > 0 && chart.candles[chart.candles.length - 1]?.status === "MARKET_CLOSED";
  const tickLive = liveTick?.status === "LIVE_DATA" || liveTick?.fresh;
  return (
    <Panel title={`${symbol} live chart`} detail={`${timeframe} candles with live MT5 price updates`} action={<div className="control-row"><select value={symbol} onChange={(event) => setSymbol(event.target.value)}>{symbols.map((item) => <option key={item}>{item}</option>)}</select><span className={`source-badge${tickLive ? "" : " offline"}`}><StatusDot live={tickLive} /> {tickLive ? "Online" : marketClosed ? "Market closed" : "Reconnecting"}</span></div>}>
      <div className="timeframe-row">{TIMEFRAMES.map((item) => <button type="button" key={item} className={timeframe === item ? "active" : ""} onClick={() => setTimeframe(item)}>{item}</button>)}</div>
      <div className="chart-meta"><span><StatusDot live={tickLive} /> {tickLive ? "Online - live WebSocket prices" : marketClosed ? "Market closed - last session shown" : "Waiting for the MT5 bridge"}</span><span>Updated {age(liveTick?.time)} - {liveTick?.age_seconds == null ? "--" : number(liveTick.age_seconds, 1) + "s quote age"}</span></div>
      <div className="chart-frame">{chart.loading && !chart.candles.length ? <div className="empty-state"><RefreshCw className="spin" /> Loading MT5 candles</div> : <CandleChart sym={symbol} timeframe={timeframe} candles={chart.candles} height={440} sourceLabel="MT5 live WebSocket" emptyTitle="No candles yet" emptyDetail="The MT5 bridge has not supplied this timeframe yet." />}</div>
    </Panel>
  );
}

function ReplayTab({ symbol, setSymbol, timeframe, setTimeframe, data, chart }) {
  const symbols = dashboardSymbols(data).map((row) => row.symbol).filter(Boolean);
  return (
    <Panel title="Trade replay" detail="Broker candles with recorded MT5 deals overlaid" action={<div className="control-row"><select value={symbol} onChange={(event) => setSymbol(event.target.value)}>{symbols.map((item) => <option key={item}>{item}</option>)}</select></div>}>
      <div className="timeframe-row">{["M1", "M5", "M15", "H1"].map((item) => <button type="button" key={item} className={timeframe === item ? "active" : ""} onClick={() => setTimeframe(item)}>{item}</button>)}</div>
      <div className="chart-meta"><span><StatusDot live /> Online - MT5 history</span><span>{chart.trades.length} recorded deal markers</span></div>
      <div className="chart-frame"><TradeReplayChart sym={symbol} timeframe={timeframe} candles={chart.candles} trades={chart.trades} cursor={chart.candles.length} height={440} /></div>
    </Panel>
  );
}

function ScannerTab({ data, onSelect }) {
  const scannerRows = data.scanner?.current_signals || [];
  const grouped = ENGINE_ORDER.reduce((acc, engine) => {
    acc[engine] = scannerRows.filter((row) => engineFor(row.symbol, row.engine || row.asset_class) === engine);
    return acc;
  }, {});
  const ready = scannerRows.filter((row) => row.gate_ok || String(row.status).includes("PROPOSAL"));
  const sessions = data.marketHours;
  const symbolCount = data.status?.tracked_symbols ?? (scannerRows.length || "--");
  return (
    <>
      <div className="scan-header">
        <div><span className="eyebrow">LIVE SCAN</span><h1>Live MT5 symbols - {symbolCount}</h1><p>Every row is current platform data. A waiting row tells you where the engine stopped.</p></div>
        <div className="ready-pill"><Activity size={19} /> {ready.length} READY</div>
      </div>
      <Panel title="Market windows" detail={sessions?.global_reason || "Current SAST market sessions"}><SessionGrid marketHours={sessions} /></Panel>
      <Panel title="Engines" detail="Independent engine status, grouped by asset class">
        <div className="engine-list">{ENGINE_ORDER.map((engine) => <EngineRow key={engine} engine={engine} rows={grouped[engine]} onSelect={onSelect} />)}</div>
      </Panel>
      <Panel title="Scan path" detail="The same explanation is used on desktop and mobile">
        <div className="flow-strip">{["MT5 live feed", "Ten-frame snapshot", "Engine analysis", "Score and confidence", "Immutable proposal", "Broker checks", "MT5 order", "Position record"].map((item, index) => <React.Fragment key={item}><span>{item}</span>{index < 7 && <ChevronRight size={15} />}</React.Fragment>)}</div>
      </Panel>
      <Panel title="Current symbol decisions" detail={`${scannerRows.length} latest platform evaluations`}>
        <div className="scan-list">
          {scannerRows.map((row) => {
            const symbol = row.symbol || row.sym;
            const engine = engineFor(symbol, row.engine || row.asset_class);
            const direction = directionOf(row);
            const score = scoreOf(row);
            const scores = row.scores || row.score_breakdown?.timeframes || {};
            return (
              <button type="button" className="scan-row" key={symbol} onClick={() => onSelect(symbol)}>
                <span className={`engine-icon small ${engineClass(engine)}`}>{engineIcon(engine)}</span>
                <span className="scan-symbol"><strong>{symbol}</strong><small>{engine} - {dateLabel(row.updated_at || row.created_at)}</small></span>
                <span className={`direction ${direction.toLowerCase()}`}>{direction}</span>
                <span className="scan-score"><strong>{score === null ? "--" : number(score, 1)}</strong><small>score</small></span>
                <span className="scan-reason">{reasonOf(row)}</span>
                <ChevronRight size={16} />
                <span className="scan-timeframes">{Object.entries(scores).slice(0, 10).map(([tf, value]) => <em key={tf} title={`${tf} score`}>{tf} {number(value, 0)}</em>)}</span>
              </button>
            );
          })}
          {!scannerRows.length && <div className="empty-state"><Search size={20} /> Waiting for the next live MT5 scan.</div>}
        </div>
      </Panel>
    </>
  );
}

function IntelligenceWatchPanel({ watch }) {
  const findings = Array.isArray(watch?.findings) ? watch.findings : [];
  const overall = watch?.overall || "UNKNOWN";
  const tone = overall === "CRITICAL" ? "red" : overall === "WARNING" ? "amber" : overall === "OK" ? "green" : "muted";
  return (
    <Panel
      title="Intelligence watch"
      detail="Automated read-only findings - runs every 6 hours, never changes the live bot"
      action={<span className={`source-badge${tone === "green" ? "" : " offline"}`}><StatusDot live={tone === "green"} /> {overall}</span>}
    >
      <div className="intel-data-list">
        {findings.map((f, index) => (
          <div className="intel-data-row compact" key={index}>
            <span><strong className={f.severity === "critical" ? "red" : f.severity === "warning" ? "amber" : "muted"}>{String(f.severity || "info").toUpperCase()}</strong><small>{f.area}</small></span>
            <span style={{ gridColumn: "span 3" }}>{f.summary}</span>
          </div>
        ))}
        {!findings.length && <div className="empty-state compact"><CheckCircle2 size={18} /> No findings - everything checked out clean.</div>}
      </div>
      <div className="compact-note">Last run {watch?.generated_at ? age(watch.generated_at) : "never"} - config drift, risk-gate activity, trailing win rate vs the current engine build, and system health.</div>
    </Panel>
  );
}

function IntelligenceTab({ data }) {
  const intelligence = data.intelligence || {};
  const portfolio = data.portfolio || {};
  const health = portfolio.portfolio_health || {};
  const planning = intelligence.planning || {};
  const learning = intelligence.learning || {};
  const execution = intelligence.execution || {};
  const validation = intelligence.validation || {};
  const dataStatus = intelligence.data_status || {};
  const plans = Array.isArray(planning.latest_by_symbol) ? planning.latest_by_symbol : [];
  const adjustments = Array.isArray(learning.latest_adjustments) ? learning.latest_adjustments : [];
  const learningEngines = Array.isArray(learning.engines) ? learning.engines : [];
  const liveScans = Array.isArray(intelligence.live_scans) ? intelligence.live_scans : [];
  const symbolResults = Array.isArray(intelligence.symbol_results) ? intelligence.symbol_results : [];
  const decisionCounts = Array.isArray(execution.decision_counts) ? execution.decision_counts : [];
  const decisionTotal = decisionCounts.reduce((total, row) => total + Number(row.count || 0), 0);
  const lastDecision = execution.last_decision || {};
  const feed = intelligence.live_market_feed || {};
  const facts = [
    { title: "Plans", value: String(plans.length), text: planning.status === "READY" ? plans.length + " current symbol plans." : "No current plans." },
    { title: "Learning feedback", value: String(learning.outcomes_received ?? 0), text: (learning.mode || "Unavailable") + " - closed outcomes received." },
    { title: "Decisions", value: String(decisionTotal), text: lastDecision.status ? "Latest: " + lastDecision.status + " " + (lastDecision.symbol || "") : "No decision recorded." },
    { title: "MT5 feed", value: dataStatus.mt5_connected ? String(dataStatus.broker_symbol_specs ?? 0) + " specs" : "Offline", text: "Seen " + age(dataStatus.last_heartbeat) + "; " + (feed.status || feed.state || "status unavailable") + "." },
  ];
  return (
    <>
      <ControlRoom status={data.status} lastRefresh={new Date()} />
      <div className="metrics-strip">
        <Metric label="Portfolio health" value={health.label || "--"} sub={"Score " + (health.score ?? "--") + " - " + (health.risk_status || "waiting")} tone={health.risk_status === "NORMAL" ? "green" : "red"} />
        <Metric label="Pending proposals" value={portfolio.proposals?.pending ?? "--"} sub="Persisted proposal count" />
        <Metric label="Open positions" value={portfolio.exposure?.open_positions ?? "--"} sub="MT5 broker positions" />
        <Metric label="Live scan rows" value={String(liveScans.length)} sub={"Last scan " + age(dataStatus.last_scan)} tone="blue" />
      </div>
      <IntelligenceWatchPanel watch={data.intelligenceWatch} />
      <Panel title="Intelligence" detail="Live MT5 planning, learning and execution">
        <div className="intel-grid">{facts.map((fact) => <div className="intel-card" key={fact.title}><span className="eyebrow">{fact.title}</span><strong>{fact.value}</strong><p>{fact.text}</p></div>)}</div>
      </Panel>
      <Panel title="Live scan evidence" detail={String(liveScans.length) + " symbols - four-frame scores (H4/H1/M15/M5)"}>
        <div className="intel-live-list">
          {liveScans.map((row) => {
            const side = directionOf(row) === "SELL" ? "SELL" : "BUY";
            const frameScores = row.scores?.[side] || row.score_breakdown?.timeframes?.[side] || {};
            const frames = ["H4", "H1", "M15", "M5"];
            return (
              <div className="intel-live-row" key={row.proposal_id || row.symbol}>
                <div className="intel-live-head"><strong>{row.symbol}</strong><small>{row.engine || row.asset_class || "engine"} - {row.status || "unreported"}</small></div>
                <div className="intel-live-summary"><span className={"direction " + directionOf(row).toLowerCase()}>{directionOf(row)}</span><span>Score <strong>{number(row.final_score ?? row.score, 1)}</strong></span><span>Confidence <strong>{number(row.confidence, 1)}</strong></span><span>Probability <strong>{number(row.probability, 1)}</strong></span></div>
                <div className="intel-score-strip">{frames.map((frame) => <span className="intel-score-chip" key={frame}><small>{frame}</small><strong>{number(frameScores[frame], 0)}</strong></span>)}</div>
                <div className="intel-live-reason">{(Array.isArray(row.reasons) && row.reasons.length ? row.reasons : [row.reason || "No reason recorded"]).join(" - ")}</div>
              </div>
            );
          })}
          {!liveScans.length && <div className="empty-state compact"><Activity size={18} /> No live scan rows are present in the MT5 state database.</div>}
        </div>
      </Panel>
      <Panel title="Current symbol plans" detail={plans.length + " current MT5 plans"}>
        <div className="intel-data-list">
          {plans.map((row) => (
            <div className="intel-data-row" key={row.plan_id || row.symbol}>
              <span><strong>{row.symbol}</strong><small>{row.asset_class || "engine"} - {row.status || "unreported"}</small></span>
              <span><small>Target market</small>{row.target_market_day || "--"} {row.target_market_date || "--"}<small>{row.target_window_sast || "--"}</small></span>
              <span><small>Direction / score</small>{row.direction || "WATCH"} {number(row.score, 1)}</span>
              <span><small>Snapshot</small>{row.source_snapshot_at ? age(row.source_snapshot_at) : "--"}<small>{(row.timeframes || []).length || 0} frames</small></span>
              <span><small>Next step</small>{planStatusLabel(row)}<small>{planReasonLabel(row)}</small></span>
            </div>
          ))}
          {!plans.length && <div className="empty-state compact"><Database size={18} /> No persisted intelligence plans in the active MT5 database.</div>}
        </div>
      </Panel>
      <Panel title="Active learning by engine" detail="Live threshold response to the latest closed MT5 outcomes">
        <div className="engine-evidence">
          {learningEngines.map((row) => <div className="evidence-row learning-row" key={row.engine}><strong>{row.engine}</strong><span>{row.outcomes_received} outcomes</span><span>{row.wins} wins / {row.losses} losses</span><span>Win rate {percent(row.win_rate)}</span><span>Avg R {number(row.average_result_r, 3)}</span><span>Threshold {number(row.threshold_current, 2)}</span><span className={Number(row.threshold_adjustment) > 0 ? "red" : "green"}>{Number(row.threshold_adjustment) > 0 ? "Tighter" : "No tighter adjustment"}</span></div>)}
          {!learningEngines.length && <div className="empty-state compact"><Sparkles size={18} /> No active engine outcomes recorded.</div>}
        </div>
        <div className="story-box"><Sparkles size={17} /> {learning.story || "No learning feedback status is available."}</div>
      </Panel>
      <div className="grid-two">
        <Panel title="Today's symbol outcomes" detail="Direct MT5 closed-deal results">
        <div className="symbol-results">
          {symbolResults.map((row) => <div className="symbol-result" key={row.symbol}>
            <div className="symbol-result-name"><strong>{row.symbol}</strong><small>{row.trades || 0} closed deals</small></div>
            <div className="symbol-result-metrics"><span className="blue">{row.wins || 0} W</span><span className="red">{row.losses || 0} L</span><span>{row.breakeven || 0} BE</span><span>{percent(row.win_rate)} win</span><span className={finite(row.pnl, 0) >= 0 ? "blue" : "red"}>{money(row.pnl)}</span></div>
          </div>)}
          {!symbolResults.length && <div className="empty-line">No closed MT5 deals recorded today.</div>}
        </div>
      </Panel>
      <Panel title="Recent learning" detail="Recorded updates from closed and missed trades">
          <div className="intel-data-list">
            {adjustments.slice(0, 8).map((row, index) => (
              <div className="intel-data-row compact" key={(row.symbol || "portfolio") + "-" + row.created_at + "-" + index}>
                <span><strong>{row.symbol || "Portfolio"}</strong><small>{row.mode || "SHADOW_ONLY"} - {dateLabel(row.created_at)}</small></span>
                <span>Score {number(row.score_before, 1)} &rarr; {number(row.score_after, 1)}</span>
                <span className={row.applied ? "green" : "muted"}>{row.applied ? "Applied" : "Observed only"}</span>
                <span className="muted">{row.reason || "No reason recorded"}</span>
              </div>
            ))}
            {!adjustments.length && <div className="empty-state compact"><Sparkles size={18} /> No persisted learning adjustments are available.</div>}
          </div>
        </Panel>
        <Panel title="Execution status" detail="Broker checks and order results">
          <div className="decision-facts">
            <DataPair label="Last decision" value={lastDecision.status || "--"} />
            <DataPair label="Symbol" value={lastDecision.symbol || "--"} />
            <DataPair label="Reason" value={lastDecision.reason || "--"} />
            <DataPair label="Recorded" value={dateLabel(lastDecision.created_at)} />
          </div>
          <div className="story-box"><Zap size={17} /> {execution.story || "No execution narrative is stored for the current runtime."}</div>
        </Panel>
      </div>
      <Panel title="Engine summary" detail="Current scores by engine">
        <div className="engine-evidence">
          {ENGINE_ORDER.map((engine) => {
            const rows = liveScans.filter((row) => engineFor(row.symbol, row.engine || row.asset_class) === engine);
            const scores = rows.map(scoreOf).filter((value) => value !== null);
            return <div className="evidence-row" key={engine}><span className={"engine-icon small " + engineClass(engine)}>{engineIcon(engine)}</span><strong>{engine}</strong><span>{rows.length} current symbols</span><span>Average score {scores.length ? number(scores.reduce((a, b) => a + b, 0) / scores.length, 1) : "--"}</span><span className="muted">{rows.filter((row) => row.gate_ok).length} proposals</span></div>;
          })}
        </div>
      </Panel>
      <Panel title="Runtime checks" detail="Current platform status">
        <div className="decision-facts">
          <DataPair label="Walk-forward" value={validation.walk_forward?.status || "--"} />
          <DataPair label="Walk-forward samples" value={validation.walk_forward?.sample_size ?? "--"} />
          <DataPair label="Live drift" value={validation.drift?.status || "--"} />
          <DataPair label="Deployment stage" value={execution.deployment?.stage || "--"} />
          <DataPair label="Trade mode" value={execution.deployment?.trade_mode || "--"} />
          <DataPair label="Last scan" value={dateLabel(dataStatus.last_scan)} />
        </div>
      </Panel>
      <Panel title="Module roles" detail="What each MT5 module does">
        <div className="boundary-list"><Boundary label="Market data" value="Synchronises MT5 ticks and candles" /><Boundary label="Learning engines" value="Create independent scored proposals" /><Boundary label="Execution" value="Checks broker conditions and submits orders" /><Boundary label="Trade management" value="Manages active positions only" /><Boundary label="Portfolio intelligence" value="Reports health only" /></div>
      </Panel>
    </>
  );
}

function Boundary({ label, value }) {
  return <div className="boundary-row"><span>{label}</span><strong>{value}</strong><CheckCircle2 size={17} /></div>;
}

function TradeTab({ data, onSelect }) {
  const positions = Array.isArray(data.positions) ? data.positions : [];
  const orders = Array.isArray(data.orders) ? data.orders : [];
  return (
    <>
      <Panel title="Trade desk" detail="The MT5 runtime owns order submission. This dashboard is an accurate read-only view.">
        <div className="read-only-banner"><Briefcase size={19} /><span>Execution is runtime-owned. No manual dashboard action can create a broker order.</span></div>
      </Panel>
      <Panel title="Open MT5 positions" detail={`${positions.length} broker positions currently visible`}>
        <div className="trade-list">
          {positions.map((row) => {
            const symbol = row.sym || row.symbol;
            const pnl = row.unrealized ?? row.profit ?? row.pnl;
            const pnlValue = finite(pnl, null);
            const pnlTone = pnlValue === null ? "muted" : pnlValue >= 0 ? "blue" : "red";
            return <button type="button" className="trade-row" key={row.ticket || symbol} onClick={() => onSelect(symbol)}><span><strong>{symbol}</strong><small>{row.direction || row.side || "--"} - {row.qty ?? row.volume ?? "--"} lots</small></span><span><small>Entry</small>{price(row.entry ?? row.price_open, symbol)}</span><span><small>Current</small>{price(row.current ?? row.price_current, symbol)}</span><span className={"position-pnl " + pnlTone}><small>Open P/L</small>{money(pnl)}</span><ChevronRight size={16} /></button>;
          })}
          {!positions.length && <div className="empty-line">No open positions reported by MT5.</div>}
        </div>
      </Panel>
      <Panel title="Pending broker orders" detail={`${orders.length} orders from the MT5 bridge`}>
        <div className="trade-list">{orders.map((row) => <div className="trade-row static" key={row.ticket || row.symbol}><span><strong>{row.symbol}</strong><small>{row.side} - {row.state}</small></span><span>{row.volume || "--"} lots</span><span>{price(row.price_open, row.symbol)}</span><span>{dateLabel(row.time_setup)}</span></div>)}{!orders.length && <div className="empty-line">No pending broker orders.</div>}</div>
      </Panel>
    </>
  );
}

function HistoryTab({ data }) {
  const [period, setPeriod] = useState("today");
  const payload = useHistory(period);
  const historyLoaded = Boolean(payload);
  const deals = payload?.deals || [];
  const summary = payload?.summary || {};
  const realizedPnl = summary.pnl ?? summary.total_pnl ?? 0;
  const symbolStats = Array.isArray(summary.by_symbol) ? summary.by_symbol : [];
  const periods = ["today", "week", "month", "all"];
  return (
    <>
      <Panel title="MT5 trade history" detail="Closed broker deals including today, this week, month and all available history." action={<div className="period-row">{periods.map((item) => <button type="button" className={period === item ? "active" : ""} key={item} onClick={() => setPeriod(item)}>{item === "all" ? "All" : item[0].toUpperCase() + item.slice(1)}</button>)}</div>}>
        <div className="history-summary">
          <DataPair label="Closed trades" value={historyLoaded ? (summary.total_trades ?? deals.length) : "--"} />
          <DataPair label="Win rate" value={historyLoaded ? percent(summary.win_rate) : "--"} sub={historyLoaded ? `${summary.decided_trades ?? 0} decided` : ""} tone="green" />
          <DataPair label="Wins" value={historyLoaded ? (summary.wins ?? 0) : "--"} tone="green" />
          <DataPair label="Losses" value={historyLoaded ? (summary.losses ?? 0) : "--"} tone="red" />
          <DataPair label="Realized P/L" value={historyLoaded ? money(realizedPnl) : "--"} tone={historyLoaded ? (finite(realizedPnl, 0) >= 0 ? "blue" : "red") : ""} />
        </div>
      </Panel>
      <Panel title="Symbol results" detail="Closed MT5 deals by symbol">
        <div className="symbol-results">
          {symbolStats.map((row) => <div className="symbol-result" key={row.symbol}>
            <div className="symbol-result-name"><strong>{row.symbol}</strong><small>{row.trades || 0} closed deals</small></div>
            <div className="symbol-result-metrics"><span className="blue">{row.wins || 0} W</span><span className="red">{row.losses || 0} L</span><span>{row.breakeven || 0} BE</span><span>{percent(row.win_rate)} win</span><span className={finite(row.pnl, 0) >= 0 ? "blue" : "red"}>{money(row.pnl)}</span></div>
          </div>)}
          {!symbolStats.length && <div className="empty-line">No closed MT5 deals in this period.</div>}
        </div>
      </Panel>
      <Panel title={`${period === "all" ? "All" : period[0].toUpperCase() + period.slice(1)} closed trades`} detail="Source: MT5 broker history reconciled by Cipher FX">
        <div className="history-list">
          <div className="table-head history-head"><span>Symbol / engine</span><span>Direction</span><span>Opened</span><span>Closed</span><span>P/L</span></div>
          {deals.map((row, index) => {
            const symbol = row.symbol || row.sym || row.ticket || `deal-${index}`;
            const value = row.realized ?? row.pnl ?? row.profit ?? 0;
            return <div className="table-row history-row" key={row.trade_id || row.id || `${symbol}-${index}`}><span className="symbol-cell"><strong>{symbol}</strong><small>{engineFor(symbol, row.engine || row.asset_class)} {row.ticket ? `- #${row.ticket}` : ""}</small></span><span className={String(row.direction || row.side).toUpperCase() === "BUY" ? "blue" : "red"}>{row.direction || row.side || "--"}</span><span>{dateLabel(row.opened_at)}</span><span>{dateLabel(row.closed_at)}</span><span className={finite(value, 0) >= 0 ? "blue" : "red"}>{money(value)}</span></div>;
          })}
          {!historyLoaded && <div className="empty-state"><RefreshCw className="spin" /> Loading MT5 history.</div>}
          {historyLoaded && !deals.length && <div className="empty-state"><History size={20} /> No closed MT5 deals in this period.</div>}
        </div>
      </Panel>
    </>
  );
}

function App({ onLogout }) {
  const { data, error, lastRefresh } = useLiveData();
  const [tab, setTab] = useState("snapshot");
  const defaultSymbol = dashboardSymbols(data)[0]?.symbol || "EURUSD";
  const [symbol, setSymbol] = useState(defaultSymbol);
  const [timeframe, setTimeframe] = useState("M15");
  const [replayTimeframe, setReplayTimeframe] = useState("M5");
  useEffect(() => {
    const available = dashboardSymbols(data);
    if (available.length && !available.some((row) => row.symbol === symbol)) setSymbol(available[0].symbol);
  }, [data.symbols, symbol]);
  const chart = useChart(symbol, timeframe, tab === "chart");
  const replay = useChart(symbol, replayTimeframe, tab === "replay");
  const currentTab = TABS.find((item) => item.id === tab) || TABS[0];
  const selectSymbol = (next) => { if (next) setSymbol(next); };

  return (
    <div className="dashboard-shell">
      <header className="app-header">
        <div className="brand"><img src={logoSrc} alt="Cipher FX" /><div><strong>CIPHER FX</strong><span>MT5 TRADING CONTROL ROOM</span></div></div>
        <div className="header-status"><StatusDot live={Boolean(data.status?.mt5_connected)} /> MT5 {String(data.status?.mode || "DEMO").toUpperCase() === "LIVE" ? "LIVE MODE" : "DEMO MODE"} - {data.status?.mt5_connected ? "CONNECTED" : "OFFLINE"} <button type="button" className="signout-button header-signout" title="Sign out" onClick={onLogout}><LogOut size={14} /></button></div>
      </header>
      <main className="dashboard-main">
        {error && <div className="alert-bar"><XCircle size={17} /> {error}</div>}
        {tab === "snapshot" && <SnapshotTab data={data} lastRefresh={lastRefresh} onTab={setTab} onSelect={selectSymbol} />}
        {tab === "market" && <MarketTab data={data} onSelect={(item) => { selectSymbol(item); setTab("chart"); }} />}
        {tab === "chart" && <ChartTab symbol={symbol} setSymbol={setSymbol} timeframe={timeframe} setTimeframe={setTimeframe} data={data} chart={chart} />}
        {tab === "replay" && <ReplayTab symbol={symbol} setSymbol={setSymbol} timeframe={replayTimeframe} setTimeframe={setReplayTimeframe} data={data} chart={replay} />}
        {tab === "scanner" && <ScannerTab data={data} onSelect={selectSymbol} />}
        {tab === "intelligence" && <IntelligenceTab data={data} />}
        {tab === "trade" && <TradeTab data={data} onSelect={(item) => { selectSymbol(item); setTab("chart"); }} />}
        {tab === "history" && <HistoryTab data={data} />}
      </main>
      <nav className="bottom-nav" aria-label="MT5 dashboard navigation">
        {TABS.map(({ id, label, short, icon: Icon }) => <button type="button" key={id} className={tab === id ? "active" : ""} onClick={() => setTab(id)}><Icon size={20} /><span className="desktop-label">{label}</span><span className="mobile-label">{short}</span></button>)}
      </nav>
    </div>
  );
}

export function TradingDashboard({ onLogout }) {
  return <App onLogout={onLogout} />;
}

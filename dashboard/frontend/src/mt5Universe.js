const SYMBOL_GROUPS = {
  Forex: [
    "EURUSD", "GBPUSD", "USDJPY", "USDCHF", "USDCAD", "AUDUSD", "NZDUSD", "USDZAR", "USDSEK", "USDNOK",
    "EURGBP", "EURJPY", "EURCHF", "EURAUD", "EURNZD", "EURCAD", "EURSEK", "EURNOK", "EURZAR",
    "GBPJPY", "GBPCHF", "GBPAUD", "GBPNZD", "GBPCAD", "GBPSEK", "GBPNOK", "GBPZAR",
    "AUDJPY", "AUDCHF", "AUDNZD", "AUDCAD", "AUDSGD", "AUDHKD",
    "NZDJPY", "NZDCHF", "NZDCAD", "NZDSGD",
    "CADJPY", "CADCHF", "CHFJPY", "SGDJPY", "USDHKD", "USDSGD", "USDMXN", "USDTRY", "USDCNH", "USDPLN", "EURPLN", "GBPPLN",
  ],
  Metals: ["XAUUSD", "XAGUSD", "XPTUSD", "XPDUSD"],
  Energies: ["USOIL", "UKOIL", "NATGAS", "BRENT", "WTI"],
  Crypto: [
    "BTCUSD", "ETHUSD", "LTCUSD", "XRPUSD", "SOLUSD", "ADAUSD", "DOGEUSD", "BNBUSD", "AVAXUSD",
    "LINKUSD", "MATICUSD", "DOTUSD", "BCHUSD", "ATOMUSD", "UNIUSD", "TRXUSD", "ETCUSD", "XLMUSD",
  ],
  Indices: [
    "NAS100", "US30", "SPX500", "US2000", "GER40", "UK100", "FRA40", "EU50", "ESP35", "AUS200",
    "HK50", "JP225", "CHINA50", "SWI20", "NED25", "STOXX50", "SA40", "SING30", "ITALY40",
  ],
  Stocks: [
    "AAPL", "MSFT", "NVDA", "AMZN", "META", "TSLA", "GOOGL", "AMD", "NFLX", "PLTR", "COIN", "INTC",
    "QCOM", "AVGO", "ORCL", "CRM", "ADBE", "SHOP", "UBER", "SNOW", "MU", "PYPL", "SQ", "IBM",
    "CSCO", "SAP", "ASML", "SONY", "TM", "BABA", "PDD", "JD", "TCEHY", "NKE", "DIS", "KO",
    "PEP", "MCD", "SBUX", "WMT", "COST", "HD", "LOW", "JPM", "BAC", "GS", "MS", "V", "MA",
    "AXP", "BRK.B", "UNH", "JNJ", "LLY", "PFE", "MRK", "ABBV", "XOM", "CVX", "SHEL", "BP",
    "COP", "SLB", "CAT", "DE", "GE", "BA", "RTX", "LMT", "RIO", "BHP", "NIO", "LI", "XPEV",
  ],
  ETFs: ["SPY", "QQQ", "DIA", "IWM", "XLF", "XLK", "XLE", "GLD", "SLV", "TLT", "ARKK", "USO"],
};

const QUOTE_OVERRIDES = {
  EURUSD: { bid: 1.11921, ask: 1.11936, change: 0.29, low: 1.11512, high: 1.12344, spread: 15 },
  GBPUSD: { bid: 1.32129, ask: 1.32203, change: 0.18, low: 1.31602, high: 1.32498, spread: 74 },
  USDJPY: { bid: 144.336, ask: 144.444, change: 0.06, low: 143.901, high: 144.891, spread: 108 },
  USDCHF: { bid: 0.79805, ask: 0.79826, change: 0.19, low: 0.79521, high: 0.79912, spread: 21 },
  USDCAD: { bid: 1.31144, ask: 1.31166, change: 0.18, low: 1.30912, high: 1.3132, spread: 22 },
  AUDUSD: { bid: 0.67951, ask: 0.67982, change: 0.09, low: 0.67712, high: 0.68121, spread: 31 },
  NZDUSD: { bid: 0.62327, ask: 0.62347, change: 0.11, low: 0.62008, high: 0.62491, spread: 20 },
  XAUUSD: { bid: 2358.42, ask: 2358.88, change: 0.21, low: 2341.12, high: 2369.44, spread: 46 },
  XAGUSD: { bid: 30.114, ask: 30.136, change: -0.12, low: 29.98, high: 30.31, spread: 22 },
  BTCUSD: { bid: 66281.25, ask: 66287.11, change: 0.45, low: 65821.12, high: 66421.55, spread: 586 },
  ETHUSD: { bid: 3524.4, ask: 3527.7, change: 0.34, low: 3479.1, high: 3559.4, spread: 33 },
  NAS100: { bid: 19872.4, ask: 19874.2, change: 0.41, low: 19721.2, high: 19921.1, spread: 18 },
  US30: { bid: 39111.8, ask: 39115.1, change: 0.17, low: 38871.2, high: 39224.6, spread: 33 },
  GER40: { bid: 18442.1, ask: 18443.7, change: -0.07, low: 18398.1, high: 18502.4, spread: 16 },
  UK100: { bid: 8242.2, ask: 8243.1, change: 0.13, low: 8198.4, high: 8268.4, spread: 9 },
  AAPL: { bid: 214.44, ask: 214.49, change: 0.55, low: 212.11, high: 215.09, spread: 5 },
  NVDA: { bid: 129.12, ask: 129.2, change: 1.08, low: 126.8, high: 130.44, spread: 8 },
  TSLA: { bid: 241.11, ask: 241.18, change: -0.24, low: 237.6, high: 243.2, spread: 7 },
  META: { bid: 533.72, ask: 533.91, change: 0.64, low: 528.22, high: 535.11, spread: 19 },
  SPY: { bid: 548.2, ask: 548.23, change: 0.18, low: 545.11, high: 549.82, spread: 3 },
  QQQ: { bid: 484.14, ask: 484.19, change: 0.24, low: 481.21, high: 485.33, spread: 5 },
};

const GROUP_PRESETS = {
  Forex: { base: 0.86, step: 0.013, spread: 0.00022, range: 0.0063, digits: 5 },
  Metals: { base: 24, step: 630, spread: 0.19, range: 14, digits: 2 },
  Energies: { base: 64, step: 2.8, spread: 0.08, range: 1.9, digits: 2 },
  Crypto: { base: 3200, step: 1900, spread: 3.4, range: 220, digits: 2 },
  Indices: { base: 4800, step: 640, spread: 1.6, range: 48, digits: 1 },
  Stocks: { base: 28, step: 5.6, spread: 0.06, range: 2.4, digits: 2 },
  ETFs: { base: 82, step: 4.2, spread: 0.04, range: 1.6, digits: 2 },
};

export const DEFAULT_ACTIVE_SYMBOLS = [
  "EURUSD", "GBPUSD", "USDJPY", "USDCHF", "USDCAD", "AUDUSD", "NZDUSD",
  "XAUUSD", "BTCUSD", "ETHUSD", "NAS100", "US30", "GER40", "SPY", "QQQ",
];

function hashSymbol(symbol) {
  return [...symbol].reduce((sum, char, index) => sum + char.charCodeAt(0) * (index + 3), 0);
}

function inferDigits(symbol, group, bid) {
  if (symbol.includes("JPY")) return 3;
  if (group === "Forex") return 5;
  if (bid >= 1000) return 2;
  if (bid >= 100) return 2;
  if (bid >= 10) return 3;
  return 5;
}

export function buildSymbolSnapshot(symbol, group) {
  if (QUOTE_OVERRIDES[symbol]) {
    return { symbol, group, ...QUOTE_OVERRIDES[symbol] };
  }

  const preset = GROUP_PRESETS[group] || GROUP_PRESETS.Stocks;
  const hash = hashSymbol(symbol);
  const center = preset.base + (hash % 37) * preset.step + ((hash % 5) - 2) * preset.range * 0.2;
  const drift = ((hash % 9) - 4) * preset.range * 0.18;
  const rawBid = center + drift;
  const rawAsk = rawBid + preset.spread * ((hash % 4) + 1);
  const digits = inferDigits(symbol, group, rawBid);
  const bid = Number(rawBid.toFixed(digits));
  const ask = Number(rawAsk.toFixed(digits));
  const low = Number((Math.min(bid, ask) - preset.range).toFixed(digits));
  const high = Number((Math.max(bid, ask) + preset.range * 1.15).toFixed(digits));
  const spread = Number(((ask - bid) * (digits >= 4 ? 100000 : digits === 3 ? 1000 : 100)).toFixed(0));
  const change = Number((((hash % 21) - 10) * 0.11).toFixed(2));

  return { symbol, group, bid, ask, change, low, high, spread };
}

export const ALL_SYMBOLS = Object.entries(SYMBOL_GROUPS).flatMap(([group, symbols]) => (
  symbols.map((symbol) => buildSymbolSnapshot(symbol, group))
));

export const SYMBOL_GROUP_NAMES = ["All", ...Object.keys(SYMBOL_GROUPS)];

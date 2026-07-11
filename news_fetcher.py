"""
News fetcher for ScalpBot.
Strategy: IBKR built-in news first, Yahoo Finance as fallback.
Returns per-symbol sentiment used by the bot's trading gate and dashboard.
"""
import logging
from datetime import datetime, timedelta

log = logging.getLogger("ScalpBot")

# ── Sentiment keyword lists ───────────────────────────────────────────────────

_BULLISH = {
    "beats", "beat", "record", "record high", "raises guidance", "raised guidance",
    "upgrade", "upgraded", "outperform", "exceeds", "exceed", "above estimates",
    "better than expected", "strong results", "top estimates", "growth", "surge",
    "soars", "soar", "rally", "breakthrough", "buyback", "dividend increase",
    "positive", "profit", "revenue growth", "robust", "boosts",
}

_BEARISH = {
    "misses", "miss", "missed", "disappoints", "disappoint", "disappointing",
    "cuts guidance", "cut guidance", "downgrade", "downgraded", "underperform",
    "below estimates", "worse than expected", "fraud", "investigation", "probe",
    "layoffs", "layoff", "job cuts", "loss", "losses", "declines", "decline",
    "falls", "fall", "drops", "drop", "warning", "recall", "lawsuit", "sec",
    "subpoena", "weak", "miss estimates", "earnings miss", "revenue miss",
    "bankruptcy", "restructuring", "write-off", "impairment",
}


def _score(text: str) -> float:
    """Sentiment score: +1.0 = strongly bullish, -1.0 = strongly bearish."""
    t = text.lower()
    bull = sum(1 for kw in _BULLISH if kw in t)
    bear = sum(1 for kw in _BEARISH if kw in t)
    if bull + bear == 0:
        return 0.0
    return (bull - bear) / (bull + bear)


# ── IBKR news ─────────────────────────────────────────────────────────────────

def fetch_ibkr_news(ib, qualified_contracts: dict) -> dict:
    """
    Pull headlines via IBKR reqHistoricalNews.
    qualified_contracts: {sym: contract} — contracts must already be qualified (have conId).
    Returns: {sym: [{"headline":..., "time":..., "source":...}]}
    """
    results = {}
    try:
        providers = ib.reqNewsProviders()
        if not providers:
            log.debug("IBKR: no news providers available for this account")
            return {}
        provider_str = "+".join(p.code for p in providers)
        log.info(f"IBKR news providers: {provider_str}")
        end_dt   = datetime.now()
        start_dt = end_dt - timedelta(hours=24)
        start_s  = start_dt.strftime("%Y-%m-%d %H:%M:%S")
        end_s    = end_dt.strftime("%Y-%m-%d %H:%M:%S")
        for sym, contract in qualified_contracts.items():
            if not contract or not getattr(contract, "conId", None):
                continue
            try:
                articles = ib.reqHistoricalNews(
                    contract.conId, provider_str, start_s, end_s, 10, []
                )
                ib.sleep(0.2)
                if articles:
                    results[sym] = [
                        {"headline": a.headline, "time": str(a.time),
                         "source": a.providerCode}
                        for a in articles
                    ]
                    log.debug(f"IBKR news {sym}: {len(articles)} articles")
            except Exception as e:
                log.debug(f"IBKR news {sym}: {e}")
    except Exception as e:
        log.debug(f"IBKR news fetch error: {e}")
    return results


# ── Yahoo Finance fallback ────────────────────────────────────────────────────

def fetch_yahoo_news(symbols: list, skip: set = None) -> dict:
    """
    Pull headlines via yfinance for any symbols not covered by IBKR.
    skip: set of symbols already fetched from IBKR.
    Returns: {sym: [{"headline":..., "time":..., "source":"Yahoo"}]}
    """
    skip = skip or set()
    to_fetch = [s for s in symbols if s not in skip]
    if not to_fetch:
        return {}
    results = {}
    try:
        import yfinance as yf
        for sym in to_fetch:
            try:
                news = yf.Ticker(sym).news or []
                items = []
                for art in news[:10]:
                    # yfinance >=0.2 nests under "content"
                    content = art.get("content", {})
                    title   = content.get("title") or art.get("title", "")
                    pub     = content.get("pubDate") or art.get("providerPublishTime", "")
                    if title:
                        items.append({"headline": title, "time": str(pub), "source": "Yahoo"})
                if items:
                    results[sym] = items
                    log.debug(f"Yahoo news {sym}: {len(items)} articles")
            except Exception as e:
                log.debug(f"Yahoo news {sym}: {e}")
    except ImportError:
        log.warning("yfinance not installed — Yahoo news unavailable")
    return results


# ── Sentiment analysis ────────────────────────────────────────────────────────

def analyze(news_by_sym: dict) -> dict:
    """
    Compute per-symbol sentiment from raw article lists.
    Returns: {sym: {"sentiment":"bullish|bearish|neutral", "score":float, "headlines":[...]}}
    """
    out = {}
    for sym, articles in news_by_sym.items():
        if not articles:
            continue
        scores  = [_score(a["headline"]) for a in articles]
        avg     = sum(scores) / len(scores)
        if avg > 0.20:
            sentiment = "bullish"
        elif avg < -0.20:
            sentiment = "bearish"
        else:
            sentiment = "neutral"
        out[sym] = {
            "sentiment": sentiment,
            "score":     round(avg, 3),
            "headlines": articles[:6],
        }
    return out


# ── Full pipeline ─────────────────────────────────────────────────────────────

def fetch_all(ib, qualified_contracts: dict, all_symbols: list) -> dict:
    """
    Run full pipeline: IBKR first, Yahoo for remainder.
    Returns analyzed sentiment dict.
    """
    ibkr_raw  = fetch_ibkr_news(ib, qualified_contracts)
    yahoo_raw = fetch_yahoo_news(all_symbols, skip=set(ibkr_raw.keys()))
    combined  = {**ibkr_raw, **yahoo_raw}
    result    = analyze(combined)
    log.info(f"News fetch complete: {len(result)} symbols — "
             f"{sum(1 for v in result.values() if v['sentiment']=='bullish')} bullish, "
             f"{sum(1 for v in result.values() if v['sentiment']=='bearish')} bearish, "
             f"{sum(1 for v in result.values() if v['sentiment']=='neutral')} neutral")
    return result


def whatsapp_summary(news_sentiment: dict, session: str = "UK") -> str:
    """Format a compact WhatsApp news brief for session open."""
    bullish = [s for s, d in news_sentiment.items() if d["sentiment"] == "bullish"]
    bearish = [s for s, d in news_sentiment.items() if d["sentiment"] == "bearish"]
    lines   = [f"📰 {session} SESSION NEWS BRIEF"]
    if bullish:
        lines.append(f"✅ Bullish: {', '.join(bullish)}")
    if bearish:
        lines.append(f"⚠️ Bearish: {', '.join(bearish)}")
    if not bullish and not bearish:
        lines.append("All symbols: neutral news")
    top_head = next(
        (d["headlines"][0]["headline"][:80] for d in news_sentiment.values()
         if d["headlines"]), None
    )
    if top_head:
        lines.append(f'📌 "{top_head}"')
    lines.append("🔗 dashboard.mytradebot.co.za")
    return "\n".join(lines)

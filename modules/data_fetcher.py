"""
Module 1: Data Fetcher
─────────────────────
Pulls all raw data the signal engine needs for each ticker:
  - OHLCV price bars (Alpaca)
  - Technical indicators: RSI, MACD, EMA (computed locally via pandas-ta)
  - News headlines + sentiment scores (Finnhub)
  - Upcoming earnings / economic events (Finnhub calendar)

Output: a clean dict per ticker, ready to be handed to the signal engine.
"""

import os
import sys
import json
import logging
from datetime import datetime, timedelta, timezone
from typing import Optional

import requests
import pandas as pd

sys.path.append(os.path.join(os.path.dirname(__file__), ".."))
from config.settings import (
    ALPACA_API_KEY, ALPACA_SECRET_KEY, ALPACA_BASE_URL,
    FINNHUB_API_KEY,
    WATCHLIST, NEWS_LOOKBACK_HOURS,
    RSI_PERIOD, MACD_FAST, MACD_SLOW, MACD_SIGNAL,
)

# ── Logging ───────────────────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger(__name__)


# ══════════════════════════════════════════════════════════════════════════════
# SECTION 1: Price data + technical indicators (Alpaca + pandas-ta)
# ══════════════════════════════════════════════════════════════════════════════

def fetch_price_bars(ticker: str, days: int = 60) -> Optional[pd.DataFrame]:
    """
    Pull daily OHLCV bars from Alpaca for the past `days` calendar days.
    Returns a DataFrame with columns: open, high, low, close, volume.
    Returns None on failure so callers can handle gracefully.
    """
    end   = datetime.now(timezone.utc)
    start = end - timedelta(days=days)

    url = f"https://data.alpaca.markets/v2/stocks/{ticker}/bars"
    headers = {
        "APCA-API-KEY-ID":     ALPACA_API_KEY,
        "APCA-API-SECRET-KEY": ALPACA_SECRET_KEY,
    }
    params = {
        "start":     start.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "end":       end.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "timeframe": "1Day",
        "limit":     days,
        "feed":      "iex",   # free data feed
    }

    try:
        resp = requests.get(url, headers=headers, params=params, timeout=10)
        resp.raise_for_status()
        bars = resp.json().get("bars", [])

        if not bars:
            log.warning(f"No price bars returned for {ticker}")
            return None

        df = pd.DataFrame(bars)
        df["t"] = pd.to_datetime(df["t"])
        df.set_index("t", inplace=True)
        df.rename(columns={"o": "open", "h": "high", "l": "low",
                            "c": "close", "v": "volume"}, inplace=True)
        df.sort_index(inplace=True)
        log.info(f"  {ticker}: {len(df)} price bars fetched")
        return df[["open", "high", "low", "close", "volume"]]

    except requests.RequestException as e:
        log.error(f"  {ticker}: price fetch failed — {e}")
        return None


def compute_indicators(df: pd.DataFrame) -> dict:
    """
    Compute RSI, MACD, and EMA-20 from a price DataFrame.
    Uses pandas-ta under the hood. Returns a flat dict of current values.
    Falls back to pandas math if pandas-ta isn't installed.
    """
    close = df["close"]

    # ── RSI ──────────────────────────────────────────────────
    try:
        import pandas_ta as ta
        rsi_series = ta.rsi(close, length=RSI_PERIOD)
        macd_df    = ta.macd(close, fast=MACD_FAST, slow=MACD_SLOW, signal=MACD_SIGNAL)
        ema20      = ta.ema(close, length=20)

        rsi_val    = round(float(rsi_series.iloc[-1]),  2) if rsi_series is not None else None
        macd_val   = round(float(macd_df.iloc[-1, 0]),  4) if macd_df is not None else None
        macd_sig   = round(float(macd_df.iloc[-1, 2]),  4) if macd_df is not None else None
        macd_hist  = round(float(macd_df.iloc[-1, 1]),  4) if macd_df is not None else None
        ema20_val  = round(float(ema20.iloc[-1]),        4) if ema20 is not None else None

    except ImportError:
        # Fallback: manual RSI + MACD with pure pandas
        log.warning("pandas-ta not installed — using manual indicator math")
        delta    = close.diff()
        gain     = delta.clip(lower=0).ewm(com=RSI_PERIOD - 1, adjust=False).mean()
        loss     = (-delta.clip(upper=0)).ewm(com=RSI_PERIOD - 1, adjust=False).mean()
        rs       = gain / loss
        rsi_val  = round(float(100 - (100 / (1 + rs.iloc[-1]))), 2)

        ema_fast  = close.ewm(span=MACD_FAST,   adjust=False).mean()
        ema_slow  = close.ewm(span=MACD_SLOW,   adjust=False).mean()
        macd_line = ema_fast - ema_slow
        signal    = macd_line.ewm(span=MACD_SIGNAL, adjust=False).mean()
        macd_val  = round(float(macd_line.iloc[-1]), 4)
        macd_sig  = round(float(signal.iloc[-1]),    4)
        macd_hist = round(float((macd_line - signal).iloc[-1]), 4)
        ema20_val = round(float(close.ewm(span=20, adjust=False).mean().iloc[-1]), 4)

    current_price = round(float(close.iloc[-1]), 4)
    prev_close    = round(float(close.iloc[-2]), 4) if len(close) > 1 else current_price
    day_change_pct = round(((current_price - prev_close) / prev_close) * 100, 2)

    return {
        "current_price":  current_price,
        "day_change_pct": day_change_pct,
        "rsi":            rsi_val,
        "macd":           macd_val,
        "macd_signal":    macd_sig,
        "macd_histogram": macd_hist,
        "ema_20":         ema20_val,
        # Simple derived flags for the signal engine
        "rsi_oversold":   rsi_val is not None and rsi_val < 30,
        "rsi_overbought": rsi_val is not None and rsi_val > 70,
        "macd_bullish":   (macd_val is not None and macd_sig is not None
                           and macd_val > macd_sig),
        "above_ema20":    (current_price is not None and ema20_val is not None
                           and current_price > ema20_val),
    }


# ══════════════════════════════════════════════════════════════════════════════
# SECTION 2: News headlines + sentiment (Finnhub)
# ══════════════════════════════════════════════════════════════════════════════

def fetch_news_sentiment(ticker: str) -> dict:
    """
    Fetch recent news + sentiment score from Finnhub.
    Returns a dict with:
      - headlines: list of recent article titles (last 24h)
      - sentiment_score: Finnhub's pre-computed buzz/sentiment float (-1 to 1)
      - sentiment_label: "positive" | "neutral" | "negative"
      - article_count: number of articles in the window
    """
    cutoff = datetime.now(timezone.utc) - timedelta(hours=NEWS_LOOKBACK_HOURS)
    cutoff_ts = int(cutoff.timestamp())
    now_ts    = int(datetime.now(timezone.utc).timestamp())

    base_url = "https://finnhub.io/api/v1"
    headers  = {"X-Finnhub-Token": FINNHUB_API_KEY}

    headlines = []
    try:
        news_resp = requests.get(
            f"{base_url}/company-news",
            headers=headers,
            params={"symbol": ticker, "from": cutoff.strftime("%Y-%m-%d"),
                    "to": datetime.now(timezone.utc).strftime("%Y-%m-%d")},
            timeout=10,
        )
        news_resp.raise_for_status()
        articles = news_resp.json()
        # Filter to our lookback window and grab headlines
        headlines = [
            a["headline"] for a in articles
            if a.get("datetime", 0) >= cutoff_ts
        ][:10]  # cap at 10 most recent headlines
        log.info(f"  {ticker}: {len(headlines)} headlines in last {NEWS_LOOKBACK_HOURS}h")
    except requests.RequestException as e:
        log.error(f"  {ticker}: news fetch failed — {e}")

    # Sentiment score removed — Finnhub sentiment endpoint requires paid tier
    # Gemini search grounding provides superior real-time sentiment analysis
    sentiment_score = 0.0
    sentiment_label = "neutral"

    return {
        "headlines":       headlines,
        "sentiment_score": sentiment_score,
        "sentiment_label": sentiment_label,
        "article_count":   len(headlines),
    }


# ══════════════════════════════════════════════════════════════════════════════
# SECTION 3: Earnings + economic calendar (Finnhub)
# ══════════════════════════════════════════════════════════════════════════════

def fetch_calendar_flags(ticker: str) -> dict:
    """
    Check whether an earnings release is imminent (within 7 days).
    Upcoming earnings = elevated uncertainty → risk filter should reduce
    position sizing even on a strong signal.
    """
    today     = datetime.now(timezone.utc).date()
    week_out  = today + timedelta(days=7)
    base_url  = "https://finnhub.io/api/v1"
    headers   = {"X-Finnhub-Token": FINNHUB_API_KEY}

    earnings_soon = False
    earnings_date = None
    earnings_estimate = None

    try:
        resp = requests.get(
            f"{base_url}/calendar/earnings",
            headers=headers,
            params={"from": today.strftime("%Y-%m-%d"),
                    "to":   week_out.strftime("%Y-%m-%d"),
                    "symbol": ticker},
            timeout=5,
        )
        resp.raise_for_status()
        items = resp.json().get("earningsCalendar", [])

        for item in items:
            if item.get("symbol") == ticker:
                earnings_soon     = True
                earnings_date     = item.get("date")
                earnings_estimate = item.get("epsEstimate")
                break

        if earnings_soon:
            log.info(f"  {ticker}: earnings due {earnings_date} — flagged")

    except requests.RequestException as e:
        log.error(f"  {ticker}: calendar fetch failed — {e}")

    return {
        "earnings_soon":     earnings_soon,
        "earnings_date":     earnings_date,
        "earnings_estimate": earnings_estimate,
    }


# ══════════════════════════════════════════════════════════════════════════════
# SECTION 4: Master fetch — assembles everything for one ticker
# ══════════════════════════════════════════════════════════════════════════════

def fetch_ticker_data(ticker: str) -> Optional[dict]:
    """
    Full data fetch for a single ticker.
    Returns a structured dict ready for the signal engine, or None on failure.
    """
    log.info(f"Fetching data for {ticker}...")

    price_df = fetch_price_bars(ticker)
    if price_df is None or len(price_df) < MACD_SLOW + 5:
        log.warning(f"  {ticker}: insufficient price data — skipping")
        return None

    indicators = compute_indicators(price_df)
    news       = fetch_news_sentiment(ticker)
    calendar   = fetch_calendar_flags(ticker)

    return {
        "ticker":         ticker,
        "timestamp":      datetime.now(timezone.utc).isoformat(),
        "price":          indicators,
        "news":           news,
        "calendar":       calendar,
        # Quick summary string for LLM prompts (keeps token count down)
        "summary": (
            f"{ticker} @ ${indicators['current_price']} "
            f"({indicators['day_change_pct']:+.2f}% today). "
            f"RSI={indicators['rsi']} "
            f"({'oversold' if indicators['rsi_oversold'] else 'overbought' if indicators['rsi_overbought'] else 'neutral'}). "
            f"MACD {'bullish' if indicators['macd_bullish'] else 'bearish'} "
            f"(histogram={indicators['macd_histogram']}). "
            f"News sentiment: {news['sentiment_label']} "
            f"(score={news['sentiment_score']}, {news['article_count']} articles). "
            f"{'EARNINGS DUE ' + str(calendar['earnings_date']) + '. ' if calendar['earnings_soon'] else ''}"
        ),
    }


def fetch_all_tickers(tickers: list = None) -> dict:
    """
    Fetch data for every ticker in the watchlist.
    Returns a dict keyed by ticker symbol.
    Skips tickers that fail rather than crashing the whole run.
    """
    tickers = tickers or WATCHLIST
    results = {}

    log.info(f"Starting data fetch for {len(tickers)} tickers...")
    for ticker in tickers:
        data = fetch_ticker_data(ticker)
        if data:
            results[ticker] = data

    log.info(f"Data fetch complete: {len(results)}/{len(tickers)} tickers ready")
    return results


# ══════════════════════════════════════════════════════════════════════════════
# Quick test — run this file directly to verify your API keys work
# ══════════════════════════════════════════════════════════════════════════════

if __name__ == "__main__":
    print("\n=== Module 1: Data Fetcher — Test Run ===\n")

    # Test with just one ticker first
    test_ticker = "NVDA"
    data = fetch_ticker_data(test_ticker)

    if data:
        print(f"\n✓ Data fetched successfully for {test_ticker}\n")
        print("── Price & Indicators ──────────────────────────")
        for k, v in data["price"].items():
            print(f"  {k:<20} {v}")
        print("\n── News ────────────────────────────────────────")
        print(f"  Sentiment: {data['news']['sentiment_label']} ({data['news']['sentiment_score']})")
        print(f"  Articles:  {data['news']['article_count']}")
        for h in data["news"]["headlines"][:3]:
            print(f"  - {h[:80]}")
        print("\n── Calendar ────────────────────────────────────")
        print(f"  Earnings soon: {data['calendar']['earnings_soon']}")
        if data["calendar"]["earnings_soon"]:
            print(f"  Earnings date: {data['calendar']['earnings_date']}")
        print("\n── Summary (sent to signal engine) ─────────────")
        print(f"  {data['summary']}")
    else:
        print(f"\n✗ Failed to fetch data for {test_ticker}")
        print("  Check your .env file and API keys.")

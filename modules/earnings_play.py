"""
Module 14: Earnings Play Mode
──────────────────────────────
Detects upcoming earnings (3-5 days out) and takes small positions
to capture potential post-earnings jumps. Exits within 24h of report.

Strategy:
  - Scans watchlist daily for tickers with earnings in 3-5 days
  - If AI signals are positive (conf >= 0.60), takes 50% of normal position
  - After earnings drop, Gemini reads results via search grounding
  - Beat + positive reaction → hold until normal stop-loss/take-profit
  - Miss + negative reaction → sell immediately
  - No clear signal after 24h → sell regardless

Safety rails:
  - Max 2 earnings plays open at once
  - ETFs excluded (SPY, QQQ, SOXX, IWM)
  - Lower confidence threshold (0.60 vs 0.65) since earnings are uncertain
  - Position size capped at 50% of max_position_usd
  - All plays logged separately for performance tracking
"""

import os
import sys
import json
import logging
import sqlite3
from datetime import datetime, timezone, timedelta
from typing import Optional

import requests

sys.path.append(os.path.join(os.path.dirname(__file__), ".."))
from config.settings import (
    ALPACA_API_KEY, ALPACA_SECRET_KEY, ALPACA_BASE_URL,
    MAX_POSITION_USD, STOP_LOSS_PCT, TAKE_PROFIT_PCT,
)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger(__name__)

EARNINGS_LOG_PATH = os.path.join(os.path.dirname(__file__), "..", "logs", "earnings_plays.json")

# Configuration
MAX_EARNINGS_PLAYS     = 2
EARNINGS_PLAY_SIZE_PCT = 0.50    # 50% of max position
EARNINGS_CONF_FLOOR    = 0.60    # lower threshold for earnings plays
EARNINGS_WINDOW_DAYS   = (3, 5)  # buy when earnings are 3-5 days away
AUTO_EXIT_HOURS        = 24      # sell if no clear signal 24h after earnings

# ETFs excluded from earnings plays
ETF_EXCLUSIONS = {"SPY", "QQQ", "SOXX", "IWM", "XLK", "SMH", "ARKK"}


# ══════════════════════════════════════════════════════════════════════════════
# SECTION 1: Earnings play log
# ══════════════════════════════════════════════════════════════════════════════

def _load_earnings_log() -> list:
    if not os.path.exists(EARNINGS_LOG_PATH):
        return []
    with open(EARNINGS_LOG_PATH) as f:
        return json.load(f)

def _save_earnings_log(log_data: list):
    os.makedirs(os.path.dirname(EARNINGS_LOG_PATH), exist_ok=True)
    with open(EARNINGS_LOG_PATH, "w") as f:
        json.dump(log_data, f, indent=2)

def get_active_earnings_plays() -> list:
    """Return all earnings plays that haven't been exited yet."""
    return [p for p in _load_earnings_log() if p.get("status") == "open"]

def log_earnings_play(ticker: str, entry_price: float, quantity: float,
                      earnings_date: str, confidence: float):
    """Record a new earnings play."""
    plays = _load_earnings_log()
    plays.append({
        "ticker":        ticker,
        "entry_price":   entry_price,
        "quantity":      quantity,
        "earnings_date": earnings_date,
        "confidence":    confidence,
        "opened_at":     datetime.now(timezone.utc).isoformat(),
        "closed_at":     None,
        "exit_price":    None,
        "pnl_pct":       None,
        "exit_reason":   None,
        "status":        "open",
    })
    _save_earnings_log(plays)
    log.info(f"  Earnings play logged: {ticker} @ ${entry_price:.2f} x {quantity:.4f}")

def close_earnings_play(ticker: str, exit_price: float, reason: str):
    """Mark an earnings play as closed."""
    plays = _load_earnings_log()
    for play in plays:
        if play["ticker"] == ticker and play["status"] == "open":
            pnl_pct = (exit_price - play["entry_price"]) / play["entry_price"]
            play.update({
                "status":      "closed",
                "closed_at":   datetime.now(timezone.utc).isoformat(),
                "exit_price":  exit_price,
                "pnl_pct":     round(pnl_pct * 100, 2),
                "exit_reason": reason,
            })
            log.info(f"  Earnings play closed: {ticker} @ ${exit_price:.2f} "
                     f"({pnl_pct:+.2%}) — {reason}")
    _save_earnings_log(plays)


# ══════════════════════════════════════════════════════════════════════════════
# SECTION 2: Earnings detection
# ══════════════════════════════════════════════════════════════════════════════

def get_earnings_in_window(ticker_data: dict) -> Optional[str]:
    """
    Check if ticker has earnings within the buy window (3-5 days).
    Returns the earnings date string if found, None otherwise.
    """
    if ticker in ETF_EXCLUSIONS:
        return None

    ticker   = ticker_data.get("ticker", "")
    calendar = ticker_data.get("calendar", {})

    if not calendar.get("earnings_soon"):
        return None

    earnings_date = calendar.get("earnings_date")
    if not earnings_date:
        return None

    try:
        earn_dt  = datetime.fromisoformat(earnings_date)
        now      = datetime.now(timezone.utc)
        days_out = (earn_dt - now).days
        min_days, max_days = EARNINGS_WINDOW_DAYS
        if min_days <= days_out <= max_days:
            log.info(f"  {ticker}: earnings in {days_out} days ({earnings_date})")
            return earnings_date
    except Exception:
        pass
    return None


def check_post_earnings_reaction(ticker: str) -> Optional[str]:
    """
    Use Gemini search grounding to assess post-earnings reaction.
    Returns 'beat', 'miss', or None if unclear.
    """
    try:
        from google import genai
        from google.genai import types
        from config.settings import GEMINI_API_KEY

        client   = genai.Client(api_key=GEMINI_API_KEY)
        prompt   = f"""
Search for the most recent earnings report for {ticker} stock.
Did the company beat or miss analyst expectations?
What was the immediate stock price reaction?

Respond with ONLY a JSON object, no markdown:
{{
  "result": "beat" or "miss" or "in_line",
  "reaction": "positive" or "negative" or "neutral",
  "price_change_pct": <number or null>,
  "summary": "<one sentence>"
}}
"""
        response = client.models.generate_content(
            model="gemini-2.5-flash",
            contents=prompt,
            config=types.GenerateContentConfig(
                temperature=0.1,
                max_output_tokens=512,
                tools=[types.Tool(google_search=types.GoogleSearch())],
            ),
        )
        import re
        raw     = response.text.strip()
        cleaned = re.sub(r"```[a-z]*\n?", "", raw).strip()
        data    = json.loads(cleaned)

        result   = data.get("result", "in_line")
        reaction = data.get("reaction", "neutral")
        summary  = data.get("summary", "")
        log.info(f"  {ticker} post-earnings: {result} / {reaction} — {summary}")

        if result == "beat" and reaction == "positive":
            return "beat"
        elif result == "miss" or reaction == "negative":
            return "miss"
        return "in_line"

    except Exception as e:
        log.error(f"  Post-earnings check failed for {ticker}: {e}")
        return None


# ══════════════════════════════════════════════════════════════════════════════
# SECTION 3: Earnings play execution
# ══════════════════════════════════════════════════════════════════════════════

def should_enter_earnings_play(ticker: str, ticker_data: dict,
                                arbitrated_signal: dict) -> tuple[bool, str]:
    """
    Decide whether to enter an earnings play for this ticker.
    Returns (should_enter, reason).
    """
    # ETF check
    if ticker in ETF_EXCLUSIONS:
        return False, "ETF excluded from earnings plays"

    # Already have max plays open
    active = get_active_earnings_plays()
    if len(active) >= MAX_EARNINGS_PLAYS:
        return False, f"max earnings plays open ({MAX_EARNINGS_PLAYS})"

    # Already have an earnings play open for this ticker
    if any(p["ticker"] == ticker for p in active):
        return False, "already have earnings play open for this ticker"

    # Check earnings window
    earnings_date = get_earnings_in_window(ticker_data)
    if not earnings_date:
        return False, "no earnings in 3-5 day window"

    # Check signal confidence
    confidence = arbitrated_signal.get("confidence", 0)
    if confidence < EARNINGS_CONF_FLOOR:
        return False, f"confidence {confidence:.2f} below earnings floor {EARNINGS_CONF_FLOOR}"

    # Only enter on buy signals
    if arbitrated_signal.get("action") != "buy":
        return False, f"signal is {arbitrated_signal.get('action')} not buy"

    return True, earnings_date


def execute_earnings_play(ticker: str, earnings_date: str,
                           current_price: float, confidence: float) -> bool:
    """
    Place an earnings play buy order at 50% of normal position size.
    Returns True on success.
    """
    try:
        from modules.param_manager import get_effective_params
        params       = get_effective_params()
        max_pos      = params.get("max_position_usd", MAX_POSITION_USD)
        play_size    = max_pos * EARNINGS_PLAY_SIZE_PCT
        quantity     = round(play_size / current_price, 6)

        if quantity <= 0:
            log.error(f"  {ticker}: calculated 0 shares — skipping")
            return False

        from alpaca.trading.client import TradingClient
        from alpaca.trading.requests import MarketOrderRequest
        from alpaca.trading.enums import OrderSide, TimeInForce

        paper  = "paper" in ALPACA_BASE_URL
        client = TradingClient(ALPACA_API_KEY, ALPACA_SECRET_KEY, paper=paper)
        order  = client.submit_order(MarketOrderRequest(
            symbol=ticker,
            qty=quantity,
            side=OrderSide.BUY,
            time_in_force=TimeInForce.DAY,
        ))

        log.info(f"  Earnings play BUY: {ticker} {quantity:.4f} shares @ ~${current_price:.2f} "
                 f"(${play_size:.2f}) — earnings {earnings_date}")

        log_earnings_play(ticker, current_price, quantity, earnings_date, confidence)

        from modules.notifier import send_sms, send_email
        send_sms(
            f"[EARNINGS PLAY] BUY {ticker} {quantity:.4f} shares @ ${current_price:.2f}. "
            f"Earnings: {earnings_date}"
        )
        send_email(
            f"Earnings Play — BUY {ticker}",
            f"Entered earnings play for {ticker}\n"
            f"Shares: {quantity:.4f} @ ${current_price:.2f}\n"
            f"Total: ${play_size:.2f} (50% of max position)\n"
            f"Earnings date: {earnings_date}\n"
            f"Confidence: {confidence:.2f}\n"
            f"Will auto-exit 24h after earnings if no clear signal."
        )
        return True

    except Exception as e:
        log.error(f"  Earnings play execution failed for {ticker}: {e}")
        return False


# ══════════════════════════════════════════════════════════════════════════════
# SECTION 4: Post-earnings exit management
# ══════════════════════════════════════════════════════════════════════════════

def manage_post_earnings_exits(current_prices: dict):
    """
    Check all open earnings plays:
    - If earnings have passed, check the reaction and decide to hold or sell
    - If 24h has passed since earnings with no clear signal, auto-sell
    """
    active = get_active_earnings_plays()
    if not active:
        return

    now = datetime.now(timezone.utc)

    for play in active:
        ticker        = play["ticker"]
        earnings_date = play.get("earnings_date")
        opened_at     = datetime.fromisoformat(play["opened_at"])

        if not earnings_date:
            continue

        try:
            earn_dt = datetime.fromisoformat(earnings_date)
        except Exception:
            continue

        # Earnings haven't happened yet
        if now < earn_dt:
            days_left = (earn_dt - now).days
            log.info(f"  {ticker}: earnings play open, {days_left}d until earnings")
            continue

        # Earnings have passed — check reaction
        hours_since = (now - earn_dt).total_seconds() / 3600
        current_price = current_prices.get(ticker, play["entry_price"])

        log.info(f"  {ticker}: earnings passed {hours_since:.0f}h ago, checking reaction...")
        reaction = check_post_earnings_reaction(ticker)

        if reaction == "beat":
            log.info(f"  {ticker}: beat — holding, normal stop-loss applies")
            continue

        elif reaction == "miss":
            log.info(f"  {ticker}: miss — selling immediately")
            _sell_earnings_play(ticker, play, current_price, "earnings miss")

        elif hours_since >= AUTO_EXIT_HOURS:
            log.info(f"  {ticker}: {AUTO_EXIT_HOURS}h auto-exit triggered")
            _sell_earnings_play(ticker, play, current_price, f"{AUTO_EXIT_HOURS}h auto-exit")

        else:
            log.info(f"  {ticker}: reaction unclear, waiting (${current_price:.2f})")


def _sell_earnings_play(ticker: str, play: dict, current_price: float, reason: str):
    """Execute the sell for an earnings play exit."""
    try:
        from alpaca.trading.client import TradingClient
        from alpaca.trading.requests import MarketOrderRequest
        from alpaca.trading.enums import OrderSide, TimeInForce

        paper    = "paper" in ALPACA_BASE_URL
        client   = TradingClient(ALPACA_API_KEY, ALPACA_SECRET_KEY, paper=paper)
        quantity = play["quantity"]

        client.submit_order(MarketOrderRequest(
            symbol=ticker,
            qty=quantity,
            side=OrderSide.SELL,
            time_in_force=TimeInForce.DAY,
        ))

        close_earnings_play(ticker, current_price, reason)
        pnl_pct = (current_price - play["entry_price"]) / play["entry_price"]
        proceeds = round(quantity * current_price, 2)

        from modules.notifier import send_sms, send_email
        send_sms(
            f"[EARNINGS EXIT] {ticker} sold @ ${current_price:.2f} "
            f"({pnl_pct:+.1%}) — {reason}"
        )
        send_email(
            f"Earnings Play Exit — {ticker} ({pnl_pct:+.1%})",
            f"Closed earnings play for {ticker}\n"
            f"Entry: ${play['entry_price']:.2f}\n"
            f"Exit: ${current_price:.2f}\n"
            f"P&L: {pnl_pct:+.2%}\n"
            f"Proceeds: ${proceeds:.2f}\n"
            f"Reason: {reason}"
        )

    except Exception as e:
        log.error(f"  Earnings play sell failed for {ticker}: {e}")


# ══════════════════════════════════════════════════════════════════════════════
# SECTION 5: Performance summary
# ══════════════════════════════════════════════════════════════════════════════

def get_earnings_play_stats() -> dict:
    """Return performance statistics for all earnings plays."""
    plays  = _load_earnings_log()
    closed = [p for p in plays if p["status"] == "closed" and p.get("pnl_pct") is not None]
    active = [p for p in plays if p["status"] == "open"]

    if not closed:
        return {
            "total_plays":  len(plays),
            "active_plays": len(active),
            "closed_plays": 0,
            "win_rate":     None,
            "avg_pnl_pct":  None,
            "total_pnl_pct": None,
        }

    wins     = sum(1 for p in closed if p["pnl_pct"] > 0)
    avg_pnl  = sum(p["pnl_pct"] for p in closed) / len(closed)

    return {
        "total_plays":   len(plays),
        "active_plays":  len(active),
        "closed_plays":  len(closed),
        "win_rate":      round(wins / len(closed), 2),
        "avg_pnl_pct":   round(avg_pnl, 2),
        "best_play":     max(closed, key=lambda p: p["pnl_pct"]),
        "worst_play":    min(closed, key=lambda p: p["pnl_pct"]),
    }


# ══════════════════════════════════════════════════════════════════════════════
# Quick test
# ══════════════════════════════════════════════════════════════════════════════

if __name__ == "__main__":
    print("\n=== Module 14: Earnings Play Mode — Test Run ===\n")

    print("Active earnings plays:")
    active = get_active_earnings_plays()
    if active:
        for p in active:
            print(f"  {p['ticker']}: opened {p['opened_at'][:10]}, "
                  f"earnings {p['earnings_date']}")
    else:
        print("  None open")

    print("\nEarnings play stats:")
    stats = get_earnings_play_stats()
    for k, v in stats.items():
        if k not in ("best_play", "worst_play"):
            print(f"  {k:<20} {v}")

    print("\nETF exclusion check:")
    for ticker in ["SPY", "NVDA", "QQQ", "AMD"]:
        excluded = ticker in ETF_EXCLUSIONS
        print(f"  {ticker}: {'excluded' if excluded else 'eligible'}")

    print("\nTest complete — no trades placed")

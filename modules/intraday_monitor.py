"""
Module 11: Intraday Monitor
────────────────────────────
Runs every 2 hours during market hours (9:30am–4pm ET, Mon–Fri).
Checks open positions against current prices and triggers stop-loss
or take-profit sells immediately — no AI calls, pure price logic.

Also checks for bot health (scheduler alive) and sends alert if dead.

Cost: zero AI API calls. Uses only Alpaca market data.
"""

import os
import sys
import logging
import sqlite3
from datetime import datetime, timezone, timedelta
from typing import Optional

import requests
import pytz

sys.path.append(os.path.join(os.path.dirname(__file__), ".."))
from config.settings import (
    ALPACA_API_KEY, ALPACA_SECRET_KEY, ALPACA_BASE_URL,
    STOP_LOSS_PCT, TAKE_PROFIT_PCT,
)
from modules.risk_filter import get_open_positions, update_position, DB_PATH, init_db
from modules.notifier import send_email, send_sms

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger(__name__)

ET_TZ         = pytz.timezone("America/New_York")
MARKET_OPEN   = 9 * 60 + 30   # 9:30am in minutes
MARKET_CLOSE  = 16 * 60        # 4:00pm in minutes
HEARTBEAT_PATH = os.path.join(os.path.dirname(__file__), "..", "logs", "heartbeat.txt")


# ══════════════════════════════════════════════════════════════════════════════
# SECTION 1: Market hours check
# ══════════════════════════════════════════════════════════════════════════════

def is_market_open() -> bool:
    """Returns True if US market is currently open."""
    now_et  = datetime.now(ET_TZ)
    if now_et.weekday() >= 5:  # Saturday=5, Sunday=6
        return False
    minutes = now_et.hour * 60 + now_et.minute
    return MARKET_OPEN <= minutes <= MARKET_CLOSE


# ══════════════════════════════════════════════════════════════════════════════
# SECTION 2: Fetch current prices from Alpaca
# ══════════════════════════════════════════════════════════════════════════════

def get_current_prices(tickers: list) -> dict:
    """
    Fetch latest trade prices for a list of tickers from Alpaca.
    Returns {ticker: price} dict. Missing tickers silently omitted.
    """
    if not tickers:
        return {}
    try:
        paper = "paper" in ALPACA_BASE_URL
        base  = "https://data.alpaca.markets"
        resp  = requests.get(
            f"{base}/v2/stocks/trades/latest",
            headers={
                "APCA-API-KEY-ID":     ALPACA_API_KEY,
                "APCA-API-SECRET-KEY": ALPACA_SECRET_KEY,
            },
            params={"symbols": ",".join(tickers), "feed": "iex"},
            timeout=10,
        )
        resp.raise_for_status()
        trades = resp.json().get("trades", {})
        return {
            ticker: data["p"]
            for ticker, data in trades.items()
            if "p" in data
        }
    except Exception as e:
        log.error(f"Price fetch failed: {e}")
        return {}


# ══════════════════════════════════════════════════════════════════════════════
# SECTION 3: Execute emergency sell via Alpaca
# ══════════════════════════════════════════════════════════════════════════════

def emergency_sell(ticker: str, quantity: float, reason: str) -> bool:
    """
    Place an immediate market sell order on Alpaca.
    Returns True on success.
    """
    try:
        from alpaca.trading.client import TradingClient
        from alpaca.trading.requests import MarketOrderRequest
        from alpaca.trading.enums import OrderSide, TimeInForce

        paper  = "paper" in ALPACA_BASE_URL
        client = TradingClient(ALPACA_API_KEY, ALPACA_SECRET_KEY, paper=paper)
        order  = client.submit_order(MarketOrderRequest(
            symbol=ticker,
            qty=quantity,
            side=OrderSide.SELL,
            time_in_force=TimeInForce.DAY,
        ))
        log.warning(f"  EMERGENCY SELL {ticker}: {quantity} shares — {reason} (order {order.id})")

        # Update local position log
        update_position(ticker, 0, 0)

        # Log to trades DB
        init_db()
        conn = sqlite3.connect(DB_PATH)
        conn.execute("""
            INSERT INTO trades
              (timestamp, ticker, action, quantity, price, total_usd,
               confidence, arbitration, signal_type, reasoning, status)
            VALUES (?,?,?,?,?,?,?,?,?,?,?)
        """, (
            datetime.now(timezone.utc).isoformat(),
            ticker, "sell", quantity, None, None,
            1.0, "intraday_monitor", "risk_management", reason, "executed",
        ))
        conn.commit()
        conn.close()
        return True

    except Exception as e:
        log.error(f"  Emergency sell failed for {ticker}: {e}")
        return False


# ══════════════════════════════════════════════════════════════════════════════
# SECTION 4: Main intraday check
# ══════════════════════════════════════════════════════════════════════════════

def run_intraday_check():
    """
    Check all open positions for stop-loss / take-profit triggers.
    Sells immediately if triggered. Sends SMS + email alert.
    Only runs during market hours.
    """
    if not is_market_open():
        log.info("Intraday check: market closed — skipping")
        return

    positions = get_open_positions()
    if not positions:
        log.info("Intraday check: no open positions")
        return

    tickers = list(positions.keys())
    prices  = get_current_prices(tickers)

    if not prices:
        log.warning("Intraday check: could not fetch prices")
        return

    log.info(f"Intraday check: monitoring {len(tickers)} positions...")
    triggered = []

    for ticker, pos in positions.items():
        current_price = prices.get(ticker)
        if current_price is None:
            log.warning(f"  {ticker}: no price available")
            continue

        avg_cost   = pos["avg_cost"]
        quantity   = pos["quantity"]
        pct_change = (current_price - avg_cost) / avg_cost if avg_cost else 0

        log.info(f"  {ticker}: ${current_price:.2f} ({pct_change:+.2%}) "
                 f"vs avg cost ${avg_cost:.2f}")

        # Load effective params in case they've been tuned
        try:
            from modules.param_manager import get_effective_params
            params       = get_effective_params()
            stop_loss    = params.get("stop_loss_pct",   STOP_LOSS_PCT)
            take_profit  = params.get("take_profit_pct", TAKE_PROFIT_PCT)
        except Exception:
            stop_loss   = STOP_LOSS_PCT
            take_profit = TAKE_PROFIT_PCT

        if pct_change <= -stop_loss:
            reason = f"Stop-loss triggered: {pct_change:.2%} (threshold: -{stop_loss:.2%})"
            log.warning(f"  {ticker}: {reason}")
            ok = emergency_sell(ticker, quantity, reason)
            if ok:
                proceeds = round(quantity * current_price, 2)
                triggered.append({
                    "ticker": ticker, "action": "sell", "reason": reason,
                    "pct": pct_change, "proceeds": proceeds,
                })
                send_sms(
                    f"[STOP-LOSS] {ticker} sold at {pct_change:.1%}. "
                    f"Proceeds: ${proceeds:.2f}"
                )
                send_email(
                    f"[STOP-LOSS] {ticker} sold — {pct_change:.1%}",
                    f"{reason}\nProceeds: ${proceeds:.2f}\nQuantity: {quantity}"
                )

        elif pct_change >= take_profit:
            reason = f"Take-profit triggered: {pct_change:.2%} (threshold: +{take_profit:.2%})"
            log.info(f"  {ticker}: {reason}")
            ok = emergency_sell(ticker, quantity, reason)
            if ok:
                proceeds = round(quantity * current_price, 2)
                triggered.append({
                    "ticker": ticker, "action": "sell", "reason": reason,
                    "pct": pct_change, "proceeds": proceeds,
                })
                send_sms(
                    f"[TAKE-PROFIT] {ticker} sold at {pct_change:.1%}. "
                    f"Proceeds: ${proceeds:.2f}"
                )
                send_email(
                    f"[TAKE-PROFIT] {ticker} sold — +{pct_change:.1%}",
                    f"{reason}\nProceeds: ${proceeds:.2f}\nQuantity: {quantity}"
                )

    if not triggered:
        log.info("Intraday check: all positions within range")

    return triggered


# ══════════════════════════════════════════════════════════════════════════════
# SECTION 5: Bot health watchdog
# ══════════════════════════════════════════════════════════════════════════════

def write_heartbeat():
    """Write current timestamp to heartbeat file. Called by main scheduler."""
    os.makedirs(os.path.dirname(HEARTBEAT_PATH), exist_ok=True)
    with open(HEARTBEAT_PATH, "w") as f:
        f.write(datetime.now(timezone.utc).isoformat())


def check_bot_health():
    """
    Check if the main scheduler is still alive by reading the heartbeat file.
    If the heartbeat is older than 26 hours on a weekday, sends an alert.
    """
    if not os.path.exists(HEARTBEAT_PATH):
        log.warning("Heartbeat file not found — bot may not have run yet")
        return

    with open(HEARTBEAT_PATH) as f:
        last_beat = datetime.fromisoformat(f.read().strip())

    now     = datetime.now(timezone.utc)
    age_hrs = (now - last_beat).total_seconds() / 3600
    now_et  = now.astimezone(ET_TZ)

    # Only alert on weekdays — no trading on weekends
    if now_et.weekday() >= 5:
        return

    if age_hrs > 26:
        msg = (
            f"[ALERT] Trading bot may be down. "
            f"Last heartbeat: {last_beat.strftime('%Y-%m-%d %H:%M UTC')} "
            f"({age_hrs:.0f}h ago). SSH in to check."
        )
        log.error(msg)
        send_sms(msg)
        send_email("[ALERT] Trading Bot Health Check Failed", msg)
    else:
        log.info(f"Bot health: OK (last heartbeat {age_hrs:.1f}h ago)")


# ══════════════════════════════════════════════════════════════════════════════
# Quick test
# ══════════════════════════════════════════════════════════════════════════════

if __name__ == "__main__":
    print("\n=== Module 11: Intraday Monitor — Test Run ===\n")
    print(f"Market open: {is_market_open()}")

    positions = get_open_positions()
    print(f"Open positions: {list(positions.keys()) or 'none'}")

    if positions:
        tickers = list(positions.keys())
        prices  = get_current_prices(tickers)
        print(f"Current prices: {prices}")
        for ticker, pos in positions.items():
            price = prices.get(ticker, pos["avg_cost"])
            pct   = (price - pos["avg_cost"]) / pos["avg_cost"] if pos["avg_cost"] else 0
            print(f"  {ticker}: ${price:.2f} ({pct:+.2%}) vs avg ${pos['avg_cost']:.2f}")

    write_heartbeat()
    print("\nHeartbeat written.")
    check_bot_health()
    print("\nHealth check complete.")

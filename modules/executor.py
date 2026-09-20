"""
Module 5: Executor
───────────────────
Places approved trades on Alpaca using the official alpaca-py SDK.
Updates the local position log after each fill.

Paper trading is the default (ALPACA_BASE_URL in .env points to paper API).
Switch to live by changing ALPACA_BASE_URL to https://api.alpaca.markets
"""

import os
import sys
import logging
from datetime import datetime, timezone
from typing import Optional

from alpaca.trading.client import TradingClient
from alpaca.trading.requests import MarketOrderRequest
from alpaca.trading.enums import OrderSide, TimeInForce

sys.path.append(os.path.join(os.path.dirname(__file__), ".."))
from config.settings import (
    ALPACA_API_KEY,
    ALPACA_SECRET_KEY,
    ALPACA_BASE_URL,
)
from modules.risk_filter import log_trade, update_position, get_open_positions

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger(__name__)

_trading_client = None

def _get_client() -> TradingClient:
    global _trading_client
    if _trading_client is None:
        paper = "paper" in ALPACA_BASE_URL
        _trading_client = TradingClient(
            ALPACA_API_KEY,
            ALPACA_SECRET_KEY,
            paper=paper,
        )
    return _trading_client


# ══════════════════════════════════════════════════════════════════════════════
# SECTION 1: Execute a single approved trade
# ══════════════════════════════════════════════════════════════════════════════

def execute_trade(decision: dict) -> dict:
    """
    Submit a market order to Alpaca for an approved buy or sell decision.

    Returns the decision dict updated with:
      - status: "executed" | "failed"
      - order_id: Alpaca order ID if successful
      - error: error message if failed
    """
    ticker   = decision["ticker"]
    action   = decision["action"]
    quantity = decision.get("quantity", 0)

    if not decision.get("approved"):
        log.warning(f"  {ticker}: execute_trade called on unapproved decision — skipping")
        return {**decision, "status": "skipped", "error": "not approved"}

    if action == "hold":
        return {**decision, "status": "hold", "order_id": None}

    if quantity <= 0:
        log.error(f"  {ticker}: quantity is zero — skipping")
        return {**decision, "status": "failed", "error": "quantity is zero"}

    side = OrderSide.BUY if action == "buy" else OrderSide.SELL

    log.info(
        f"  Placing {action.upper()} order: "
        f"{quantity} shares of {ticker} @ ~${decision.get('price', '?')}"
    )

    try:
        client = _get_client()
        order_request = MarketOrderRequest(
            symbol=ticker,
            qty=quantity,
            side=side,
            time_in_force=TimeInForce.DAY,
        )
        order = client.submit_order(order_request)
        order_id = str(order.id)

        log.info(f"  {ticker}: order submitted — ID {order_id}")

        # Update local position log
        positions = get_open_positions()
        if action == "buy":
            existing = positions.get(ticker)
            if existing:
                # Average down/up
                old_qty    = existing["quantity"]
                old_cost   = existing["avg_cost"]
                new_qty    = old_qty + quantity
                new_cost   = ((old_qty * old_cost) + (quantity * decision["price"])) / new_qty
                update_position(ticker, new_qty, new_cost)
            else:
                update_position(ticker, quantity, decision["price"])
        elif action == "sell":
            existing = positions.get(ticker)
            if existing:
                remaining = existing["quantity"] - quantity
                if remaining > 0.001:
                    update_position(ticker, remaining, existing["avg_cost"])
                else:
                    update_position(ticker, 0, 0)

        result = {**decision, "status": "executed", "order_id": order_id}
        log_trade(result)
        return result

    except Exception as e:
        log.error(f"  {ticker}: order failed — {e}")
        result = {**decision, "status": "failed", "error": str(e)}
        log_trade({**result, "blocked_reason": str(e)})
        return result


# ══════════════════════════════════════════════════════════════════════════════
# SECTION 2: Execute all approved trades
# ══════════════════════════════════════════════════════════════════════════════

def execute_all(filtered_decisions: dict) -> dict:
    """
    Execute every approved non-hold trade.
    Returns results dict keyed by ticker.
    """
    results   = {}
    to_execute = [
        d for d in filtered_decisions.values()
        if d.get("approved") and d.get("action") != "hold"
    ]

    if not to_execute:
        log.info("No approved trades to execute this cycle.")
        return filtered_decisions

    log.info(f"Executing {len(to_execute)} approved trade(s)...")

    # Sells first — free up capital before buying
    sells = [d for d in to_execute if d["action"] == "sell"]
    buys  = [d for d in to_execute if d["action"] == "buy"]

    for decision in sells + buys:
        ticker = decision["ticker"]
        results[ticker] = execute_trade(decision)

    executed = sum(1 for r in results.values() if r.get("status") == "executed")
    failed   = sum(1 for r in results.values() if r.get("status") == "failed")
    log.info(f"Execution complete — {executed} executed, {failed} failed")

    return {**filtered_decisions, **results}


# ══════════════════════════════════════════════════════════════════════════════
# SECTION 3: Account info helpers (used by the dashboard)
# ══════════════════════════════════════════════════════════════════════════════

def get_account_info() -> dict:
    """Fetch current account equity, cash, and buying power from Alpaca."""
    try:
        client  = _get_client()
        account = client.get_account()
        return {
            "equity":        float(account.equity),
            "cash":          float(account.cash),
            "buying_power":  float(account.buying_power),
            "pnl_today":     float(account.equity) - float(account.last_equity),
            "paper_trading": "paper" in ALPACA_BASE_URL,
        }
    except Exception as e:
        log.error(f"Could not fetch account info: {e}")
        return {}

def get_alpaca_positions() -> list:
    """Fetch live positions from Alpaca (market value, unrealized P&L)."""
    try:
        client    = _get_client()
        positions = client.get_all_positions()
        return [
            {
                "ticker":       p.symbol,
                "quantity":     float(p.qty),
                "avg_cost":     float(p.avg_entry_price),
                "current_price": float(p.current_price),
                "market_value": float(p.market_value),
                "unrealized_pl": float(p.unrealized_pl),
                "unrealized_plpc": float(p.unrealized_plpc) * 100,
            }
            for p in positions
        ]
    except Exception as e:
        log.error(f"Could not fetch positions: {e}")
        return []


# ══════════════════════════════════════════════════════════════════════════════
# Quick test
# ══════════════════════════════════════════════════════════════════════════════

if __name__ == "__main__":
    print("\n=== Module 5: Executor — Test Run ===\n")
    print("Fetching account info from Alpaca (paper trading)...")

    info = get_account_info()
    if info:
        print(f"  Equity:        ${info['equity']:,.2f}")
        print(f"  Cash:          ${info['cash']:,.2f}")
        print(f"  Buying power:  ${info['buying_power']:,.2f}")
        print(f"  P&L today:     ${info['pnl_today']:+,.2f}")
        print(f"  Paper trading: {info['paper_trading']}")
    else:
        print("  Could not connect — check ALPACA_API_KEY and ALPACA_SECRET_KEY in .env")

    print("\nFetching open positions...")
    positions = get_alpaca_positions()
    if positions:
        for p in positions:
            print(f"  {p['ticker']}: {p['quantity']} shares, "
                  f"P&L {p['unrealized_plpc']:+.1f}%")
    else:
        print("  No open positions (or connection failed)")

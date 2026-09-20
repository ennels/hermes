"""
Module 12: Portfolio Guardian
──────────────────────────────
Three passive safety and efficiency systems that run automatically:

1. Concentration Guard — blocks buys if any sector > MAX_SECTOR_PCT of portfolio
2. Weekly Rebalancer   — trims positions that have grown > REBALANCE_THRESHOLD
3. Deposit Handler     — detects new cash and scales TOTAL_BUDGET_USD automatically

All three are low-cost: no AI calls, Alpaca data only.
"""

import os
import sys
import json
import logging
from datetime import datetime, timezone
from typing import Optional

import requests

sys.path.append(os.path.join(os.path.dirname(__file__), ".."))
from config.settings import (
    ALPACA_API_KEY, ALPACA_SECRET_KEY, ALPACA_BASE_URL,
    TOTAL_BUDGET_USD, MAX_POSITION_USD,
)
from modules.risk_filter import get_open_positions, update_position
from modules.notifier import send_email, send_sms

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger(__name__)

MAX_SECTOR_PCT       = 0.40   # block if any sector exceeds 40% of portfolio
REBALANCE_THRESHOLD  = 2.5    # trim if position grows to 2.5x original allocation
DEPOSIT_TRACK_PATH   = os.path.join(os.path.dirname(__file__), "..", "logs", "deposit_track.json")

# Sector mapping for watchlist tickers
SECTOR_MAP = {
    # Memory & Storage
    "MU": "memory", "WDC": "memory", "MRVL": "memory",
    # AI Chips & Compute
    "NVDA": "semiconductors", "AMD": "semiconductors", "TSM": "semiconductors",
    "AVGO": "semiconductors", "SOXX": "semiconductors",
    # Data Center Infrastructure
    "VRT": "infrastructure", "ETN": "infrastructure", "GEV": "infrastructure",
    "NVT": "infrastructure", "EME": "infrastructure", "PWR": "infrastructure",
    # Clean Energy
    "NEE": "clean_energy", "CEG": "clean_energy", "FLNC": "clean_energy",
    "ENPH": "clean_energy", "BEP": "clean_energy",
    # AI Networking
    "CRDO": "networking", "ALAB": "networking", "ANET": "networking",
    "LITE": "networking",
    # Cloud Hyperscalers
    "MSFT": "cloud", "GOOGL": "cloud", "AMZN": "cloud", "META": "cloud",
    # AI Software
    "PLTR": "software", "NOW": "software", "CRM": "software", "SNOW": "software",
    # Market Hedges
    "SPY": "index", "QQQ": "index", "IWM": "index",
}


# ══════════════════════════════════════════════════════════════════════════════
# SECTION 1: Concentration Guard
# ══════════════════════════════════════════════════════════════════════════════

def get_sector_exposure(current_prices: dict) -> dict:
    """
    Calculate current portfolio exposure by sector.
    Returns {sector: pct_of_portfolio} dict.
    """
    positions = get_open_positions()
    if not positions:
        return {}

    total_value   = sum(
        pos["quantity"] * current_prices.get(t, pos["avg_cost"])
        for t, pos in positions.items()
    )
    if total_value <= 0:
        return {}

    sector_values = {}
    for ticker, pos in positions.items():
        sector = SECTOR_MAP.get(ticker, "other")
        price  = current_prices.get(ticker, pos["avg_cost"])
        value  = pos["quantity"] * price
        sector_values[sector] = sector_values.get(sector, 0) + value

    return {
        sector: round(value / total_value, 3)
        for sector, value in sector_values.items()
    }


def check_concentration(ticker: str, current_prices: dict) -> tuple[bool, str]:
    """
    Check if buying ticker would breach sector concentration limit.
    Returns (is_ok, reason). is_ok=True means safe to buy.
    """
    sector = SECTOR_MAP.get(ticker, "other")
    if sector == "other":
        return True, ""

    exposure = get_sector_exposure(current_prices)
    current_pct = exposure.get(sector, 0)

    if current_pct >= MAX_SECTOR_PCT:
        reason = (
            f"Sector concentration limit: {sector} already at "
            f"{current_pct:.0%} of portfolio (max {MAX_SECTOR_PCT:.0%})"
        )
        log.warning(f"  {ticker}: BLOCKED by concentration guard — {reason}")
        return False, reason

    return True, ""


# ══════════════════════════════════════════════════════════════════════════════
# SECTION 2: Weekly Rebalancer
# ══════════════════════════════════════════════════════════════════════════════

def run_rebalancer(current_prices: dict) -> list:
    """
    Check all positions for oversized growth. Trim any that have exceeded
    REBALANCE_THRESHOLD × original allocation.

    Returns list of trim actions taken.
    """
    positions = get_open_positions()
    if not positions:
        return []

    trimmed = []

    for ticker, pos in positions.items():
        current_price = current_prices.get(ticker, pos["avg_cost"])
        current_value = pos["quantity"] * current_price
        original_alloc = pos["quantity"] * pos["avg_cost"]

        if original_alloc <= 0:
            continue

        growth_ratio = current_value / original_alloc

        if growth_ratio >= REBALANCE_THRESHOLD:
            # Trim back to original allocation value
            target_value    = original_alloc
            target_quantity = target_value / current_price
            trim_quantity   = round(pos["quantity"] - target_quantity, 6)

            if trim_quantity <= 0.001:
                continue

            log.info(
                f"  {ticker}: rebalancing — grown {growth_ratio:.1f}x, "
                f"trimming {trim_quantity:.4f} shares"
            )

            try:
                from alpaca.trading.client import TradingClient
                from alpaca.trading.requests import MarketOrderRequest
                from alpaca.trading.enums import OrderSide, TimeInForce

                paper  = "paper" in ALPACA_BASE_URL
                client = TradingClient(ALPACA_API_KEY, ALPACA_SECRET_KEY, paper=paper)
                order  = client.submit_order(MarketOrderRequest(
                    symbol=ticker,
                    qty=trim_quantity,
                    side=OrderSide.SELL,
                    time_in_force=TimeInForce.DAY,
                ))
                update_position(ticker, target_quantity, pos["avg_cost"])
                proceeds = round(trim_quantity * current_price, 2)
                trimmed.append({
                    "ticker":       ticker,
                    "trim_qty":     trim_quantity,
                    "proceeds":     proceeds,
                    "growth_ratio": growth_ratio,
                })
                log.info(f"  {ticker}: trimmed {trim_quantity:.4f} shares, proceeds ${proceeds:.2f}")

            except Exception as e:
                log.error(f"  {ticker}: trim failed — {e}")

    if trimmed:
        summary = ", ".join(f"{t['ticker']} ({t['growth_ratio']:.1f}x)" for t in trimmed)
        send_sms(f"[REBALANCE] Trimmed {len(trimmed)} positions: {summary}")
        send_email(
            f"Trading Bot Weekly Rebalance — {len(trimmed)} positions trimmed",
            "\n".join(
                f"{t['ticker']}: sold {t['trim_qty']:.4f} shares (${t['proceeds']:.2f}), "
                f"was {t['growth_ratio']:.1f}x original"
                for t in trimmed
            )
        )
    else:
        log.info("Rebalancer: no positions need trimming")

    return trimmed


# ══════════════════════════════════════════════════════════════════════════════
# SECTION 3: Recurring Deposit Handler
# ══════════════════════════════════════════════════════════════════════════════

def _load_deposit_track() -> dict:
    if not os.path.exists(DEPOSIT_TRACK_PATH):
        return {"last_equity": 0.0, "last_checked": None}
    with open(DEPOSIT_TRACK_PATH) as f:
        return json.load(f)

def _save_deposit_track(data: dict):
    os.makedirs(os.path.dirname(DEPOSIT_TRACK_PATH), exist_ok=True)
    with open(DEPOSIT_TRACK_PATH, "w") as f:
        json.dump(data, f, indent=2)


def check_for_deposits() -> Optional[float]:
    """
    Detect if new cash has been deposited into the Alpaca account.
    If a deposit is detected, automatically scales TOTAL_BUDGET_USD.
    Returns the new budget if updated, None otherwise.
    """
    try:
        paper = "paper" in ALPACA_BASE_URL
        base  = "https://paper-api.alpaca.markets" if paper else "https://api.alpaca.markets"
        resp  = requests.get(
            f"{base}/v2/account",
            headers={
                "APCA-API-KEY-ID":     ALPACA_API_KEY,
                "APCA-API-SECRET-KEY": ALPACA_SECRET_KEY,
            },
            timeout=10,
        )
        resp.raise_for_status()
        account     = resp.json()
        current_cash = float(account.get("cash", 0))
        equity       = float(account.get("equity", 0))

        track       = _load_deposit_track()
        last_equity = track.get("last_equity", 0)

        # A deposit is a significant equity increase not explained by market moves
        # We use a 5% threshold to distinguish deposits from normal gains
        if last_equity > 0:
            increase    = equity - last_equity
            increase_pct = increase / last_equity if last_equity else 0

            if increase > 100 and increase_pct > 0.05:
                # Looks like a deposit — scale budget
                from modules.param_manager import set_param, get_effective_params
                params      = get_effective_params()
                old_budget  = params.get("total_budget_usd", TOTAL_BUDGET_USD)
                new_budget  = round(old_budget + increase * 0.90, 2)  # 90% of deposit

                set_param("total_budget_usd", new_budget, source="deposit_handler")
                log.info(
                    f"Deposit detected: equity ${last_equity:.0f} → ${equity:.0f} "
                    f"(+${increase:.0f}). Budget scaled: ${old_budget:.0f} → ${new_budget:.0f}"
                )

                send_sms(
                    f"[DEPOSIT] New funds detected: +${increase:.0f}. "
                    f"Bot budget updated to ${new_budget:.0f}"
                )
                send_email(
                    "Trading Bot — New Deposit Detected",
                    f"Equity increased by ${increase:.2f} (+{increase_pct:.1%}).\n"
                    f"Trading budget automatically updated: ${old_budget:.2f} → ${new_budget:.2f}\n"
                    f"Current cash: ${current_cash:.2f}"
                )

                track["last_equity"] = equity
                track["last_checked"] = datetime.now(timezone.utc).isoformat()
                _save_deposit_track(track)
                return new_budget

        # Update baseline
        track["last_equity"]  = equity
        track["last_checked"] = datetime.now(timezone.utc).isoformat()
        _save_deposit_track(track)
        log.info(f"Deposit check: equity ${equity:.0f}, no new deposits detected")
        return None

    except Exception as e:
        log.error(f"Deposit check failed: {e}")
        return None


# ══════════════════════════════════════════════════════════════════════════════
# SECTION 4: Fetch current prices helper
# ══════════════════════════════════════════════════════════════════════════════

def fetch_current_prices_for_positions() -> dict:
    """Fetch current prices for all open positions."""
    positions = get_open_positions()
    if not positions:
        return {}
    try:
        from modules.intraday_monitor import get_current_prices
        return get_current_prices(list(positions.keys()))
    except Exception as e:
        log.error(f"Could not fetch prices: {e}")
        return {}


# ══════════════════════════════════════════════════════════════════════════════
# Quick test
# ══════════════════════════════════════════════════════════════════════════════

if __name__ == "__main__":
    print("\n=== Module 12: Portfolio Guardian — Test Run ===\n")

    prices = fetch_current_prices_for_positions()

    print("Sector exposure:")
    exposure = get_sector_exposure(prices)
    if exposure:
        for sector, pct in sorted(exposure.items(), key=lambda x: -x[1]):
            bar = "█" * int(pct * 20)
            print(f"  {sector:<20} {pct:.0%} {bar}")
    else:
        print("  No open positions yet")

    print("\nConcentration check (NVDA):")
    ok, reason = check_concentration("NVDA", prices)
    print(f"  Safe to buy: {ok}" + (f" — {reason}" if reason else ""))

    print("\nDeposit check:")
    check_for_deposits()

    print("\nRebalancer check:")
    trimmed = run_rebalancer(prices)
    print(f"  Positions trimmed: {len(trimmed)}")

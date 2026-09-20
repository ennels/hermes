"""
Module 4: Risk Filter
──────────────────────
Sits between arbitration and execution. Every trade decision MUST pass
through here before touching Alpaca. If any rule fails, the trade is
blocked and the reason is logged.

Rules enforced:
  1. Kill switch        — if bot is paused, block everything
  2. Budget cap         — total deployed capital cannot exceed TOTAL_BUDGET_USD
  3. Position limit     — max number of simultaneous open positions
  4. Per-ticker cap     — single position cannot exceed MAX_POSITION_USD
  5. Duplicate guard    — don't buy a ticker we already hold
  6. Stop-loss check    — flag positions that have breached stop-loss for forced sell
  7. Take-profit check  — flag positions that have hit take-profit for forced sell
  8. Earnings guard     — reduce position size when earnings are imminent
  9. Confidence floor   — reject trades below MIN_CONFIDENCE
"""

import os
import sys
import json
import logging
import sqlite3
from datetime import datetime, timezone
from typing import Optional

sys.path.append(os.path.join(os.path.dirname(__file__), ".."))
from config.settings import (
    TOTAL_BUDGET_USD,
    MAX_POSITION_USD,
    MAX_POSITIONS,
    STOP_LOSS_PCT,
    TAKE_PROFIT_PCT,
    MIN_CONFIDENCE,
)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger(__name__)

DB_PATH     = os.path.join(os.path.dirname(__file__), "..", "logs", "trades.db")
STATE_PATH  = os.path.join(os.path.dirname(__file__), "..", "logs", "bot_state.json")


# ══════════════════════════════════════════════════════════════════════════════
# SECTION 1: Bot state (kill switch + pause flag)
# ══════════════════════════════════════════════════════════════════════════════

def _default_state() -> dict:
    return {"paused": False, "paused_reason": "", "paused_at": None}

def load_state() -> dict:
    os.makedirs(os.path.dirname(STATE_PATH), exist_ok=True)
    if not os.path.exists(STATE_PATH):
        save_state(_default_state())
    with open(STATE_PATH) as f:
        return json.load(f)

def save_state(state: dict):
    os.makedirs(os.path.dirname(STATE_PATH), exist_ok=True)
    with open(STATE_PATH, "w") as f:
        json.dump(state, f, indent=2)

def pause_bot(reason: str = "Manual pause"):
    state = load_state()
    state["paused"]       = True
    state["paused_reason"] = reason
    state["paused_at"]    = datetime.now(timezone.utc).isoformat()
    save_state(state)
    log.warning(f"Bot PAUSED: {reason}")

def resume_bot():
    state = load_state()
    state["paused"]       = False
    state["paused_reason"] = ""
    state["paused_at"]    = None
    save_state(state)
    log.info("Bot RESUMED")

def is_paused() -> bool:
    return load_state().get("paused", False)


# ══════════════════════════════════════════════════════════════════════════════
# SECTION 2: Trade log (SQLite)
# ══════════════════════════════════════════════════════════════════════════════

def init_db():
    """Create the trades table if it doesn't exist."""
    os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS trades (
            id            INTEGER PRIMARY KEY AUTOINCREMENT,
            timestamp     TEXT NOT NULL,
            ticker        TEXT NOT NULL,
            action        TEXT NOT NULL,
            quantity      REAL,
            price         REAL,
            total_usd     REAL,
            confidence    REAL,
            arbitration   TEXT,
            signal_type   TEXT,
            reasoning     TEXT,
            status        TEXT,
            blocked_reason TEXT
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS positions (
            ticker        TEXT PRIMARY KEY,
            quantity      REAL NOT NULL,
            avg_cost      REAL NOT NULL,
            opened_at     TEXT NOT NULL,
            last_updated  TEXT NOT NULL
        )
    """)
    conn.commit()
    conn.close()

def log_trade(trade: dict):
    """Write a trade record (executed or blocked) to the audit log."""
    init_db()
    conn = sqlite3.connect(DB_PATH)
    conn.execute("""
        INSERT INTO trades
          (timestamp, ticker, action, quantity, price, total_usd,
           confidence, arbitration, signal_type, reasoning, status, blocked_reason)
        VALUES (?,?,?,?,?,?,?,?,?,?,?,?)
    """, (
        datetime.now(timezone.utc).isoformat(),
        trade.get("ticker"),
        trade.get("action"),
        trade.get("quantity"),
        trade.get("price"),
        trade.get("total_usd"),
        trade.get("confidence"),
        trade.get("arbitration"),
        trade.get("signal_type"),
        trade.get("reasoning", "")[:1000],
        trade.get("status", "unknown"),
        trade.get("blocked_reason", ""),
    ))
    conn.commit()
    conn.close()

def get_open_positions() -> dict:
    """Return current open positions from the local DB as {ticker: {...}}."""
    init_db()
    conn   = sqlite3.connect(DB_PATH)
    cursor = conn.execute("SELECT ticker, quantity, avg_cost, opened_at FROM positions")
    rows   = cursor.fetchall()
    conn.close()
    return {
        row[0]: {"ticker": row[0], "quantity": row[1],
                 "avg_cost": row[2], "opened_at": row[3]}
        for row in rows
    }

def update_position(ticker: str, quantity: float, avg_cost: float):
    """Upsert a position record."""
    init_db()
    now  = datetime.now(timezone.utc).isoformat()
    conn = sqlite3.connect(DB_PATH)
    if quantity <= 0:
        conn.execute("DELETE FROM positions WHERE ticker=?", (ticker,))
    else:
        conn.execute("""
            INSERT INTO positions (ticker, quantity, avg_cost, opened_at, last_updated)
            VALUES (?,?,?,?,?)
            ON CONFLICT(ticker) DO UPDATE SET
              quantity=excluded.quantity,
              avg_cost=excluded.avg_cost,
              last_updated=excluded.last_updated
        """, (ticker, quantity, avg_cost, now, now))
    conn.commit()
    conn.close()

def get_total_deployed(current_prices: dict) -> float:
    """Calculate total USD currently deployed across all open positions."""
    positions = get_open_positions()
    total = 0.0
    for ticker, pos in positions.items():
        price = current_prices.get(ticker, pos["avg_cost"])
        total += pos["quantity"] * price
    return round(total, 2)


# ══════════════════════════════════════════════════════════════════════════════
# SECTION 3: Position sizing
# ══════════════════════════════════════════════════════════════════════════════

def calculate_position_size(
    decision: dict,
    current_price: float,
    available_budget: float,
    earnings_soon: bool = False,
) -> float:
    """
    Calculate how many shares to buy.

    Base position = min(MAX_POSITION_USD, available_budget) scaled by confidence.
    Earnings penalty: halve the position size if earnings are imminent.
    Returns quantity as a float (Alpaca supports fractional shares).
    """
    # Scale position by confidence: 0.65 confidence = 65% of max position
    confidence   = decision.get("confidence", MIN_CONFIDENCE)
    base_usd     = min(MAX_POSITION_USD, available_budget) * confidence
    base_usd     = min(base_usd, available_budget)

    if earnings_soon:
        base_usd *= 0.5
        log.info(f"  {decision['ticker']}: earnings penalty applied — "
                 f"position halved to ${base_usd:.2f}")

    if base_usd < 1.0 or current_price <= 0:
        return 0.0

    quantity = round(base_usd / current_price, 6)
    log.info(
        f"  {decision['ticker']}: position size = "
        f"{quantity} shares @ ${current_price} = ${base_usd:.2f}"
    )
    return quantity


# ══════════════════════════════════════════════════════════════════════════════
# SECTION 4: Stop-loss and take-profit scanner
# ══════════════════════════════════════════════════════════════════════════════

def check_exit_conditions(current_prices: dict) -> list:
    """
    Scan all open positions for stop-loss or take-profit triggers.
    Returns a list of forced-sell decisions to pass to the executor.
    """
    positions    = get_open_positions()
    forced_sells = []

    for ticker, pos in positions.items():
        current_price = current_prices.get(ticker)
        if current_price is None:
            continue

        avg_cost  = pos["avg_cost"]
        pct_change = (current_price - avg_cost) / avg_cost

        if pct_change <= -STOP_LOSS_PCT:
            log.warning(
                f"  {ticker}: STOP-LOSS triggered "
                f"({pct_change*100:.1f}% < -{STOP_LOSS_PCT*100:.0f}%)"
            )
            forced_sells.append({
                "ticker":      ticker,
                "action":      "sell",
                "quantity":    pos["quantity"],
                "confidence":  1.0,
                "reasoning":   f"Stop-loss triggered at {pct_change*100:.1f}%",
                "signal_type": "risk_management",
                "arbitration": "stop_loss",
                "forced":      True,
            })

        elif pct_change >= TAKE_PROFIT_PCT:
            log.info(
                f"  {ticker}: TAKE-PROFIT triggered "
                f"({pct_change*100:.1f}% > +{TAKE_PROFIT_PCT*100:.0f}%)"
            )
            forced_sells.append({
                "ticker":      ticker,
                "action":      "sell",
                "quantity":    pos["quantity"],
                "confidence":  1.0,
                "reasoning":   f"Take-profit triggered at {pct_change*100:.1f}%",
                "signal_type": "risk_management",
                "arbitration": "take_profit",
                "forced":      True,
            })

    return forced_sells


# ══════════════════════════════════════════════════════════════════════════════
# SECTION 5: Main filter — approve or block a trade decision
# ══════════════════════════════════════════════════════════════════════════════

def filter_decision(
    decision: dict,
    ticker_data: dict,
    current_prices: dict,
) -> dict:
    """
    Run all risk rules against a single arbitrated decision.

    Returns the decision dict with added fields:
      - approved (bool)
      - blocked_reason (str, empty if approved)
      - quantity (float, calculated if approved buy)
      - price (float, current price)
    """
    ticker        = decision["ticker"]
    action        = decision["action"]
    current_price = ticker_data["price"]["current_price"]
    earnings_soon = ticker_data["calendar"]["earnings_soon"]

    decision = {**decision, "price": current_price, "approved": False,
                "blocked_reason": "", "quantity": 0.0}

    # Rule 1: Kill switch
    if is_paused():
        state = load_state()
        decision["blocked_reason"] = f"Bot paused: {state.get('paused_reason', '')}"
        log_trade({**decision, "status": "blocked"})
        log.info(f"  {ticker}: BLOCKED — {decision['blocked_reason']}")
        return decision

    # Rule 9: Confidence floor
    if decision["confidence"] < MIN_CONFIDENCE and not decision.get("forced"):
        decision["blocked_reason"] = (
            f"Confidence {decision['confidence']:.2f} below floor {MIN_CONFIDENCE}"
        )
        log_trade({**decision, "status": "blocked"})
        log.info(f"  {ticker}: BLOCKED — {decision['blocked_reason']}")
        return decision

    # Hold decisions pass through without further checks
    if action == "hold":
        decision["approved"] = True
        decision["blocked_reason"] = ""
        return decision

    positions     = get_open_positions()
    total_deployed = get_total_deployed(current_prices)
    available     = max(0.0, TOTAL_BUDGET_USD - total_deployed)

    if action == "buy":
        # Rule 5: Duplicate guard
        if ticker in positions:
            decision["blocked_reason"] = f"Already holding {ticker}"
            log_trade({**decision, "status": "blocked"})
            log.info(f"  {ticker}: BLOCKED — {decision['blocked_reason']}")
            return decision

        # Rule 10: Sector concentration guard
        try:
            from modules.portfolio_guardian import check_concentration
            ok, conc_reason = check_concentration(ticker, current_prices)
            if not ok:
                decision["blocked_reason"] = conc_reason
                log_trade({**decision, "status": "blocked"})
                log.info(f"  {ticker}: BLOCKED — {conc_reason}")
                return decision
        except ImportError:
            pass

        # Rule 3: Position count limit
        if len(positions) >= MAX_POSITIONS:
            decision["blocked_reason"] = (
                f"Max positions ({MAX_POSITIONS}) already open"
            )
            log_trade({**decision, "status": "blocked"})
            log.info(f"  {ticker}: BLOCKED — {decision['blocked_reason']}")
            return decision

        # Rule 2: Budget cap
        if available < 1.0:
            decision["blocked_reason"] = (
                f"Budget exhausted — deployed ${total_deployed:.2f} "
                f"of ${TOTAL_BUDGET_USD:.2f}"
            )
            log_trade({**decision, "status": "blocked"})
            log.info(f"  {ticker}: BLOCKED — {decision['blocked_reason']}")
            return decision

        # Apply sector rotation position size adjustment
        try:
            from modules.sector_rotation import get_ticker_adjustments
            adj = get_ticker_adjustments(ticker)
            pos_delta = adj.get("pos_delta", 0.0)
            if pos_delta != 0.0:
                old_max = MAX_POSITION_USD
                import config.settings as _cs
                _cs.MAX_POSITION_USD = round(MAX_POSITION_USD * (1 + pos_delta), 2)
                log.info(f"  {ticker}: rotation pos size ${old_max:.0f}->${_cs.MAX_POSITION_USD:.0f}")
        except ImportError:
            pass

        # High conviction boost — up to 125% position size when both models agree strongly
        effective_max = MAX_POSITION_USD
        if decision.get("confidence", 0) >= 0.80 and decision.get("arbitration") == "agreement":
            effective_max = round(MAX_POSITION_USD * 1.25, 2)
            log.info(f"  {ticker}: high conviction boost — max pos ${MAX_POSITION_USD:.0f}->${effective_max:.0f}")

        # High conviction boost — up to 125% position size when both models agree strongly
        effective_max = MAX_POSITION_USD
        if decision.get("confidence", 0) >= 0.80 and decision.get("arbitration") == "agreement":
            effective_max = round(MAX_POSITION_USD * 1.25, 2)
            log.info(f"  {ticker}: high conviction boost — max pos ${MAX_POSITION_USD:.0f}->${effective_max:.0f}")

        # Calculate quantity
        qty = calculate_position_size(
            decision, current_price, available, earnings_soon
        )
        if qty <= 0:
            decision["blocked_reason"] = "Calculated quantity is zero"
            log_trade({**decision, "status": "blocked"})
            return decision

        total_usd = round(qty * current_price, 2)

        # Rule 4: Per-ticker cap
        if total_usd > MAX_POSITION_USD:
            qty       = round(MAX_POSITION_USD / current_price, 6)
            total_usd = round(qty * current_price, 2)

        decision.update({
            "approved":  True,
            "quantity":  qty,
            "total_usd": total_usd,
        })

    elif action == "sell":
        # Verify we actually hold the ticker (unless forced)
        if ticker not in positions and not decision.get("forced"):
            decision["blocked_reason"] = f"No position in {ticker} to sell"
            log_trade({**decision, "status": "blocked"})
            log.info(f"  {ticker}: BLOCKED — {decision['blocked_reason']}")
            return decision

        pos = positions.get(ticker, {})
        qty = decision.get("quantity") or pos.get("quantity", 0)
        decision.update({
            "approved":  True,
            "quantity":  qty,
            "total_usd": round(qty * current_price, 2),
        })

    log.info(
        f"  {ticker}: APPROVED — {action.upper()} "
        f"{decision.get('quantity', 0):.4f} shares "
        f"@ ${current_price} = ${decision.get('total_usd', 0):.2f}"
    )
    return decision


def filter_all_decisions(
    decisions: dict,
    all_ticker_data: dict,
) -> dict:
    """
    Run the risk filter across all arbitrated decisions.
    Returns filtered decisions dict with approved/blocked flags set.
    """
    current_prices = {
        t: d["price"]["current_price"]
        for t, d in all_ticker_data.items()
    }

    # First check stop-loss/take-profit on existing positions
    forced_sells = check_exit_conditions(current_prices)
    for fs in forced_sells:
        ticker = fs["ticker"]
        if ticker not in decisions:
            decisions[ticker] = fs
        else:
            decisions[ticker] = fs  # forced sell overrides signal

    filtered = {}
    for ticker, decision in decisions.items():
        td = all_ticker_data.get(ticker, {})
        if not td:
            td = {
                "price":    {"current_price": current_prices.get(ticker, 0)},
                "calendar": {"earnings_soon": False},
            }
        filtered[ticker] = filter_decision(decision, td, current_prices)

    approved = sum(1 for d in filtered.values() if d.get("approved") and d["action"] != "hold")
    log.info(f"Risk filter: {approved} trades approved for execution")
    return filtered


# ══════════════════════════════════════════════════════════════════════════════
# Quick test
# ══════════════════════════════════════════════════════════════════════════════

if __name__ == "__main__":
    print("\n=== Module 4: Risk Filter — Test Run ===\n")
    init_db()

    mock_ticker_data = {
        "NVDA": {
            "ticker": "NVDA",
            "price":    {"current_price": 135.50, "day_change_pct": 1.82,
                         "rsi": 58.4, "macd": 1.23, "macd_signal": 0.95,
                         "macd_histogram": 0.28, "ema_20": 131.10,
                         "rsi_oversold": False, "rsi_overbought": False,
                         "macd_bullish": True, "above_ema20": True},
            "news":     {"sentiment_score": 0.72, "sentiment_label": "positive",
                         "article_count": 8, "headlines": []},
            "calendar": {"earnings_soon": False, "earnings_date": None,
                         "earnings_estimate": None},
            "summary":  "NVDA mock data",
        }
    }

    mock_decisions = {
        "NVDA": {
            "ticker":      "NVDA",
            "action":      "buy",
            "confidence":  0.76,
            "reasoning":   "Strong technicals + positive sentiment.",
            "signal_type": "mixed",
            "arbitration": "agreement",
        }
    }

    print("Testing risk filter with mock NVDA buy decision...")
    filtered = filter_all_decisions(mock_decisions, mock_ticker_data)

    for ticker, d in filtered.items():
        print(f"\n  {ticker}:")
        print(f"    Action:    {d['action'].upper()}")
        print(f"    Approved:  {d['approved']}")
        print(f"    Quantity:  {d.get('quantity', 0):.4f} shares")
        print(f"    Total USD: ${d.get('total_usd', 0):.2f}")
        if d.get("blocked_reason"):
            print(f"    Blocked:   {d['blocked_reason']}")

    print("\n  Testing kill switch...")
    pause_bot("Test pause")
    filtered2 = filter_all_decisions(mock_decisions, mock_ticker_data)
    for ticker, d in filtered2.items():
        print(f"    {ticker}: approved={d['approved']}, reason='{d['blocked_reason']}'")
    resume_bot()
    print("  Bot resumed.")

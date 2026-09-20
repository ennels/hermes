"""
Module 7: Adaptive Risk Manager
─────────────────────────────────
Automatically tightens or loosens risk parameters based on:

  1. Recent win/loss streak      — losing streaks tighten everything
  2. Portfolio drawdown          — large drawdowns trigger defensive mode
  3. Market volatility (VIX)     — high VIX = smaller positions
  4. Per-ticker volatility       — high-beta stocks get smaller allocations
  5. Account equity growth       — winning streaks gradually unlock more capital

Risk modes (auto-selected):
  DEFENSIVE  — losing streak or large drawdown.  Tight stops, tiny positions.
  CAUTIOUS   — mild underperformance.            Slightly tighter than normal.
  NORMAL     — baseline.                         Use config/settings.py values.
  CONFIDENT  — sustained profitability.          Slightly larger positions.
  AGGRESSIVE — strong uptrend + low volatility.  Max positions, relaxed stops.

All adjustments are logged and reversible. Parameters never exceed the
hard ceilings defined at the bottom of this file.
"""

import os
import sys
import sqlite3
import logging
from datetime import datetime, timezone, timedelta
from typing import Optional

import requests

sys.path.append(os.path.join(os.path.dirname(__file__), ".."))
from config.settings import (
    TOTAL_BUDGET_USD, MAX_POSITION_USD, MAX_POSITIONS,
    STOP_LOSS_PCT, TAKE_PROFIT_PCT, MIN_CONFIDENCE,
    ALPACA_API_KEY, ALPACA_SECRET_KEY,
)
from modules.risk_filter import DB_PATH, init_db

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger(__name__)

# ── Hard ceilings — adaptive logic can NEVER exceed these ─────────────────────
HARD_MAX_POSITION_USD  = MAX_POSITION_USD * 1.5   # never more than 150% of config
HARD_MIN_STOP_LOSS     = 0.02                      # never tighter than 2%
HARD_MAX_STOP_LOSS     = 0.12                      # never looser than 12%
HARD_MIN_CONFIDENCE    = 0.55                      # never below 55%
HARD_MAX_CONFIDENCE    = 0.85                      # never require more than 85%

# ── Risk mode definitions ──────────────────────────────────────────────────────
RISK_MODES = {
    "DEFENSIVE": {
        "position_scale":   0.40,   # 40% of normal position size
        "stop_loss_mult":   0.60,   # tighter stops (e.g. 5% → 3%)
        "take_profit_mult": 0.70,
        "confidence_bump":  0.10,   # require higher confidence
        "max_positions":    2,
    },
    "CAUTIOUS": {
        "position_scale":   0.65,
        "stop_loss_mult":   0.80,
        "take_profit_mult": 0.85,
        "confidence_bump":  0.05,
        "max_positions":    3,
    },
    "NORMAL": {
        "position_scale":   1.00,
        "stop_loss_mult":   1.00,
        "take_profit_mult": 1.00,
        "confidence_bump":  0.00,
        "max_positions":    MAX_POSITIONS,
    },
    "CONFIDENT": {
        "position_scale":   1.20,
        "stop_loss_mult":   1.15,
        "take_profit_mult": 1.20,
        "confidence_bump": -0.03,
        "max_positions":    MAX_POSITIONS,
    },
    "AGGRESSIVE": {
        "position_scale":   1.40,
        "stop_loss_mult":   1.30,
        "take_profit_mult": 1.35,
        "confidence_bump": -0.05,
        "max_positions":    min(MAX_POSITIONS + 2, 8),
    },
}


# ══════════════════════════════════════════════════════════════════════════════
# SECTION 1: Performance metrics from trade log
# ══════════════════════════════════════════════════════════════════════════════

def get_recent_performance(lookback_days: int = 14) -> dict:
    """
    Analyse the last N days of closed trades from the SQLite log.
    Returns win rate, streak, avg P&L, and drawdown metrics.
    """
    init_db()
    cutoff = (datetime.now(timezone.utc) - timedelta(days=lookback_days)).isoformat()

    conn = sqlite3.connect(DB_PATH)
    rows = conn.execute("""
        SELECT ticker, action, total_usd, status, timestamp
        FROM trades
        WHERE status = 'executed'
          AND timestamp >= ?
        ORDER BY timestamp ASC
    """, (cutoff,)).fetchall()
    conn.close()

    if not rows:
        return {
            "trade_count": 0, "win_rate": 0.5, "recent_streak": 0,
            "avg_pnl_pct": 0.0, "max_drawdown_pct": 0.0, "has_data": False,
        }

    # Pair buys with subsequent sells to calculate per-trade P&L
    positions = {}
    pnls      = []

    for ticker, action, total_usd, status, ts in rows:
        if action == "buy":
            positions[ticker] = {"cost": total_usd, "ts": ts}
        elif action == "sell" and ticker in positions:
            cost    = positions.pop(ticker)["cost"]
            pnl_pct = (total_usd - cost) / cost if cost else 0
            pnls.append(pnl_pct)

    if not pnls:
        return {
            "trade_count": len(rows), "win_rate": 0.5, "recent_streak": 0,
            "avg_pnl_pct": 0.0, "max_drawdown_pct": 0.0, "has_data": False,
        }

    wins       = sum(1 for p in pnls if p > 0)
    win_rate   = wins / len(pnls)
    avg_pnl    = sum(pnls) / len(pnls)

    # Streak: positive = winning streak, negative = losing streak
    streak = 0
    for pnl in reversed(pnls):
        if pnl > 0:
            if streak >= 0: streak += 1
            else: break
        else:
            if streak <= 0: streak -= 1
            else: break

    # Max drawdown from cumulative P&L
    cumulative = 0.0
    peak       = 0.0
    max_dd     = 0.0
    for pnl in pnls:
        cumulative += pnl
        peak        = max(peak, cumulative)
        dd          = peak - cumulative
        max_dd      = max(max_dd, dd)

    return {
        "trade_count":     len(pnls),
        "win_rate":        round(win_rate, 3),
        "recent_streak":   streak,
        "avg_pnl_pct":     round(avg_pnl, 4),
        "max_drawdown_pct": round(max_dd, 4),
        "has_data":        True,
    }


# ══════════════════════════════════════════════════════════════════════════════
# SECTION 2: Market volatility (VIX proxy via Alpaca)
# ══════════════════════════════════════════════════════════════════════════════

def get_market_volatility() -> dict:
    """
    Fetch recent S&P 500 (SPY) volatility as a VIX proxy.
    Returns a volatility level: "low", "normal", "elevated", "high".
    Falls back to "normal" if unavailable.
    """
    try:
        from datetime import timedelta
        end   = datetime.now(timezone.utc)
        start = end - timedelta(days=30)

        resp = requests.get(
            "https://data.alpaca.markets/v2/stocks/SPY/bars",
            headers={
                "APCA-API-KEY-ID":     ALPACA_API_KEY,
                "APCA-API-SECRET-KEY": ALPACA_SECRET_KEY,
            },
            params={
                "start":     start.strftime("%Y-%m-%dT%H:%M:%SZ"),
                "end":       end.strftime("%Y-%m-%dT%H:%M:%SZ"),
                "timeframe": "1Day",
                "feed":      "iex",
            },
            timeout=10,
        )
        resp.raise_for_status()
        bars = resp.json().get("bars", [])

        if len(bars) < 5:
            return {"level": "normal", "daily_vol_pct": 1.0}

        import pandas as pd
        closes  = pd.Series([b["c"] for b in bars])
        returns = closes.pct_change().dropna()
        daily_vol = float(returns.std() * 100)  # as percentage

        if daily_vol < 0.6:
            level = "low"
        elif daily_vol < 1.2:
            level = "normal"
        elif daily_vol < 2.0:
            level = "elevated"
        else:
            level = "high"

        log.info(f"Market volatility: {level} ({daily_vol:.2f}% daily std)")
        return {"level": level, "daily_vol_pct": round(daily_vol, 3)}

    except Exception as e:
        log.warning(f"Could not fetch volatility data: {e}")
        return {"level": "normal", "daily_vol_pct": 1.0}


# ══════════════════════════════════════════════════════════════════════════════
# SECTION 3: Risk mode selection
# ══════════════════════════════════════════════════════════════════════════════

def select_risk_mode(performance: dict, volatility: dict) -> str:
    """
    Select a risk mode based on recent performance and market volatility.
    Returns one of: DEFENSIVE, CAUTIOUS, NORMAL, CONFIDENT, AGGRESSIVE
    """
    if not performance["has_data"]:
        # No history yet — start cautious
        if volatility["level"] in ("elevated", "high"):
            return "CAUTIOUS"
        return "NORMAL"

    streak  = performance["recent_streak"]
    win_rate = performance["win_rate"]
    drawdown = performance["max_drawdown_pct"]
    vol      = volatility["level"]

    # Hard defensive triggers
    if drawdown > 0.15 or streak <= -4:
        return "DEFENSIVE"

    if drawdown > 0.08 or streak <= -2 or vol == "high":
        return "CAUTIOUS"

    # Positive performance in calm market
    if streak >= 4 and win_rate > 0.65 and vol == "low":
        return "AGGRESSIVE"

    if streak >= 2 and win_rate > 0.58 and vol in ("low", "normal"):
        return "CONFIDENT"

    if vol == "elevated":
        return "CAUTIOUS"

    return "NORMAL"


# ══════════════════════════════════════════════════════════════════════════════
# SECTION 4: Compute adaptive parameters
# ══════════════════════════════════════════════════════════════════════════════

def compute_adaptive_params() -> dict:
    """
    Main entry point. Returns a complete set of risk parameters adjusted
    for current conditions, ready to be used by the risk filter.

    Always returns a full parameter dict even if data is unavailable
    (falls back to settings.py values).
    """
    performance = get_recent_performance()
    volatility  = get_market_volatility()
    mode        = select_risk_mode(performance, volatility)
    multipliers = RISK_MODES[mode]

    raw_stop_loss     = STOP_LOSS_PCT   * multipliers["stop_loss_mult"]
    raw_take_profit   = TAKE_PROFIT_PCT * multipliers["take_profit_mult"]
    raw_max_pos_usd   = MAX_POSITION_USD * multipliers["position_scale"]
    raw_min_conf      = MIN_CONFIDENCE  + multipliers["confidence_bump"]

    # Apply hard ceilings
    adj_stop_loss   = round(max(HARD_MIN_STOP_LOSS,
                                min(HARD_MAX_STOP_LOSS, raw_stop_loss)), 4)
    adj_take_profit = round(max(0.05, min(0.50, raw_take_profit)), 4)
    adj_max_pos_usd = round(max(10.0,
                                min(HARD_MAX_POSITION_USD, raw_max_pos_usd)), 2)
    adj_min_conf    = round(max(HARD_MIN_CONFIDENCE,
                                min(HARD_MAX_CONFIDENCE, raw_min_conf)), 3)
    adj_max_pos     = multipliers["max_positions"]

    params = {
        "risk_mode":        mode,
        "stop_loss_pct":    adj_stop_loss,
        "take_profit_pct":  adj_take_profit,
        "max_position_usd": adj_max_pos_usd,
        "max_positions":    adj_max_pos,
        "min_confidence":   adj_min_conf,
        "performance":      performance,
        "volatility":       volatility,
        "computed_at":      datetime.now(timezone.utc).isoformat(),
        # Show delta vs baseline for transparency
        "deltas": {
            "stop_loss":    round(adj_stop_loss   - STOP_LOSS_PCT,    4),
            "take_profit":  round(adj_take_profit - TAKE_PROFIT_PCT,  4),
            "max_position": round(adj_max_pos_usd - MAX_POSITION_USD, 2),
            "confidence":   round(adj_min_conf    - MIN_CONFIDENCE,   3),
        },
    }

    log.info(
        f"Adaptive risk: mode={mode}, "
        f"stop={adj_stop_loss:.1%}, "
        f"conf>={adj_min_conf:.2f}, "
        f"max_pos=${adj_max_pos_usd:.0f}"
    )
    return params


# ══════════════════════════════════════════════════════════════════════════════
# Quick test
# ══════════════════════════════════════════════════════════════════════════════

if __name__ == "__main__":
    print("\n=== Module 7: Adaptive Risk — Test Run ===\n")

    print("Fetching performance history...")
    perf = get_recent_performance()
    print(f"  Trades analyzed: {perf['trade_count']}")
    print(f"  Win rate:        {perf['win_rate']:.0%}")
    print(f"  Recent streak:   {perf['recent_streak']:+d}")
    print(f"  Avg P&L:         {perf['avg_pnl_pct']:.2%}")
    print(f"  Max drawdown:    {perf['max_drawdown_pct']:.2%}")

    print("\nFetching market volatility...")
    vol = get_market_volatility()
    print(f"  Level:     {vol['level']}")
    print(f"  Daily vol: {vol['daily_vol_pct']:.2f}%")

    print("\nComputing adaptive parameters...")
    params = compute_adaptive_params()
    print(f"\n  Risk mode:      {params['risk_mode']}")
    print(f"  Stop-loss:      {params['stop_loss_pct']:.1%}  "
          f"(delta: {params['deltas']['stop_loss']:+.1%})")
    print(f"  Take-profit:    {params['take_profit_pct']:.1%}  "
          f"(delta: {params['deltas']['take_profit']:+.1%})")
    print(f"  Max position:   ${params['max_position_usd']:.0f}  "
          f"(delta: ${params['deltas']['max_position']:+.0f})")
    print(f"  Min confidence: {params['min_confidence']:.2f}  "
          f"(delta: {params['deltas']['confidence']:+.2f})")
    print(f"  Max positions:  {params['max_positions']}")

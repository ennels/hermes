"""
Module 9: AI Advisor
─────────────────────
On-demand Claude analysis. Call this anytime to get:

  1. Portfolio health review    — is the current strategy working?
  2. Parameter recommendations  — should any settings be adjusted?
  3. Watchlist suggestions      — tickers worth adding/removing
  4. Market context summary     — what's the macro picture right now?
  5. Risk assessment            — any red flags to address?

Unlike the signal engine (which generates trade signals), the advisor
gives strategic, human-readable guidance you can act on or ignore.
It has full visibility into your trade history, current positions,
performance metrics, and current parameters.
"""

import os
import sys
import json
import logging
from datetime import datetime, timezone

import anthropic

sys.path.append(os.path.join(os.path.dirname(__file__), ".."))
from config.settings import ANTHROPIC_API_KEY, WATCHLIST
from modules.risk_filter import DB_PATH, get_open_positions, init_db
from modules.param_manager import get_all_params, get_param_history, get_effective_params
from modules.adaptive_risk import get_recent_performance, get_market_volatility

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger(__name__)


# ══════════════════════════════════════════════════════════════════════════════
# SECTION 1: Build advisor context
# ══════════════════════════════════════════════════════════════════════════════

def _build_advisor_context() -> str:
    """
    Assemble a comprehensive context block from all available bot data.
    Sent to Claude as the system/user context for any advisory query.
    """
    init_db()

    # Performance metrics
    perf = get_recent_performance(lookback_days=30)
    vol  = get_market_volatility()

    # Current positions
    positions = get_open_positions()
    pos_lines = []
    for ticker, p in positions.items():
        pos_lines.append(
            f"  {ticker}: {p['quantity']:.4f} shares @ avg ${p['avg_cost']:.2f}"
        )
    positions_str = "\n".join(pos_lines) if pos_lines else "  No open positions"

    # Recent trades from SQLite
    import sqlite3
    recent_trades = []
    try:
        conn = sqlite3.connect(DB_PATH)
        rows = conn.execute("""
            SELECT timestamp, ticker, action, total_usd, status, confidence,
                   arbitration, signal_type, blocked_reason
            FROM trades
            ORDER BY id DESC LIMIT 20
        """).fetchall()
        conn.close()
        for r in rows:
            trade_str = (
                f"  {r[0][:10]} {r[2].upper():4} {r[1]:6} "
                f"${r[3] or 0:.0f} conf={r[5] or 0:.2f} "
                f"[{r[4]}]"
            )
            if r[8]:
                trade_str += f" blocked: {r[8][:40]}"
            recent_trades.append(trade_str)
    except Exception:
        recent_trades = ["  (no trade history yet)"]

    # Current effective parameters
    params = get_effective_params()

    # Recent parameter changes
    history = get_param_history(limit=10)
    history_lines = [
        f"  {h['timestamp'][:10]} {h['key']}: {h['old_value']} → {h['new_value']}"
        for h in history
    ] or ["  (no recent changes)"]

    # Earnings play context
    try:
        from modules.earnings_play import get_earnings_play_stats, get_active_earnings_plays
        ep_stats  = get_earnings_play_stats()
        ep_active = get_active_earnings_plays()
        earnings_context = (
            f"Total plays: {ep_stats['total_plays']}, "
            f"Active: {ep_stats['active_plays']}, "
            f"Win rate: {ep_stats['win_rate'] or 'N/A'}, "
            f"Avg P&L: {ep_stats['avg_pnl_pct'] or 'N/A'}%"
        )
        if ep_active:
            earnings_context += "\nActive: " + ", ".join(
                f"{p['ticker']} (earnings {p['earnings_date']})" for p in ep_active
            )
    except Exception:
        earnings_context = "Module not available"

    # Sector rotation context
    try:
        from modules.sector_rotation import get_sector_adjustments, _load_rotation
        rotation_data    = _load_rotation()
        rotation_summary = rotation_data.get("summary", "No analysis yet")
        adj  = get_sector_adjustments()
        hot  = [s for s, d in adj.items() if d["signal"] == "hot"]
        cold = [s for s, d in adj.items() if d["signal"] == "cold"]
        rotation_context = (
            f"Summary: {rotation_summary}\n"
            f"Hot sectors: {', '.join(hot) or 'none'}\n"
            f"Cold sectors: {', '.join(cold) or 'none'}"
        )
    except Exception:
        rotation_context = "Module not available"

    # Signal cache context
    try:
        from modules.signal_cache import get_cache_stats
        cs = get_cache_stats()
        cache_context = f"Valid entries: {cs['valid_entries']}, Total: {cs['total_entries']}"
    except Exception:
        cache_context = "Module not available"

    return f"""
TRADING BOT CONTEXT — {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')}

── PERFORMANCE (last 30 days) ──────────────────────────────
Trades analyzed:  {perf['trade_count']}
Win rate:         {perf['win_rate']:.0%}
Recent streak:    {perf['recent_streak']:+d} ({'winning' if perf['recent_streak'] > 0 else 'losing' if perf['recent_streak'] < 0 else 'neutral'})
Avg P&L per trade: {perf['avg_pnl_pct']:.2%}
Max drawdown:     {perf['max_drawdown_pct']:.2%}

── MARKET CONDITIONS ────────────────────────────────────────
Volatility level: {vol['level']} ({vol['daily_vol_pct']:.2f}% daily std)
Current risk mode: {params.get('_risk_mode', 'NORMAL')}

── CURRENT POSITIONS ────────────────────────────────────────
{positions_str}

── RECENT TRADES (last 20) ──────────────────────────────────
{chr(10).join(recent_trades[:20])}

── CURRENT PARAMETERS ───────────────────────────────────────
Total budget:     ${params.get('total_budget_usd', 0):,.0f}
Max position:     ${params.get('max_position_usd', 0):.0f}
Max positions:    {params.get('max_positions', 0)}
Stop-loss:        {params.get('stop_loss_pct', 0):.1%}
Take-profit:      {params.get('take_profit_pct', 0):.1%}
Min confidence:   {params.get('min_confidence', 0):.2f}
Adaptive risk:    {params.get('adaptive_risk', True)}
Run time (ET):    {params.get('run_time_et', '09:45')}

── WATCHLIST ────────────────────────────────────────────────
{', '.join(WATCHLIST)}

── RECENT PARAMETER CHANGES ────────────────────────────────
{chr(10).join(history_lines)}

── EARNINGS PLAYS ───────────────────────────────────────────
{earnings_context}

── SECTOR ROTATION ──────────────────────────────────────────
{rotation_context}

── SIGNAL CACHE ─────────────────────────────────────────────
{cache_context}
""".strip()


# ══════════════════════════════════════════════════════════════════════════════
# SECTION 2: Advisory queries
# ══════════════════════════════════════════════════════════════════════════════

def _ask_advisor(question: str, context: str) -> str:
    """Send a question to Claude with full bot context. Returns the response."""
    try:
        client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)
        message = client.messages.create(
            model="claude-sonnet-4-5",
            max_tokens=1500,
            system=(
                "You are a trading bot advisor reviewing a live automated trading system. "
                "You have access to the bot's full performance history, current positions, "
                "parameters, and market conditions. Give clear, actionable, specific advice. "
                "Be honest about weaknesses. Format your response with clear sections. "
                "When recommending parameter changes, always state the specific value "
                "(e.g. 'set stop_loss_pct to 0.04') not just 'reduce it'."
            ),
            messages=[
                {
                    "role": "user",
                    "content": f"{context}\n\n── YOUR QUESTION ──\n{question}",
                }
            ],
        )
        return message.content[0].text
    except anthropic.APIError as e:
        return f"Advisor unavailable: {e}"


# ══════════════════════════════════════════════════════════════════════════════
# SECTION 3: Named advisory reports
# ══════════════════════════════════════════════════════════════════════════════

def get_portfolio_review() -> str:
    """Full portfolio health review with actionable recommendations."""
    log.info("Running portfolio review...")
    ctx = _build_advisor_context()
    return _ask_advisor(
        "Please give me a full portfolio review. Cover: "
        "(1) Is the current strategy working based on the performance data? "
        "(2) Are there any positions I should be concerned about? "
        "(3) Are my current risk parameters appropriate for current market conditions? "
        "(4) What are the top 3 specific actions I should take right now?",
        ctx,
    )


def get_parameter_recommendations() -> str:
    """Targeted advice on whether to adjust any bot parameters."""
    log.info("Fetching parameter recommendations...")
    ctx = _build_advisor_context()
    return _ask_advisor(
        "Review my current bot parameters and performance. "
        "For each parameter that should be changed, state: "
        "(1) the parameter name exactly as it appears in the system, "
        "(2) the recommended new value, "
        "(3) why this change would help. "
        "If parameters look good, say so clearly. "
        "Also tell me if adaptive_risk should be enabled or disabled given current conditions.",
        ctx,
    )


def get_watchlist_recommendations() -> str:
    """Advice on adding or removing tickers from the watchlist."""
    log.info("Fetching watchlist recommendations...")
    ctx = _build_advisor_context()
    return _ask_advisor(
        "Review my current watchlist. Based on the performance data and "
        "current market conditions: "
        "(1) Which tickers should I consider removing and why? "
        "(2) What sectors or types of stocks might I be missing? "
        "(3) Are there any obvious diversification gaps? "
        "Keep recommendations practical for an automated daily-frequency bot.",
        ctx,
    )


def get_risk_assessment() -> str:
    """Identify current risk concentrations and red flags."""
    log.info("Running risk assessment...")
    ctx = _build_advisor_context()
    return _ask_advisor(
        "Perform a risk assessment of the current bot state. Look for: "
        "(1) Concentration risk — am I too heavy in one sector or ticker? "
        "(2) Drawdown risk — is the max drawdown trend concerning? "
        "(3) Parameter risk — are any settings dangerously loose or tight? "
        "(4) Execution risk — are too many trades getting blocked and why? "
        "Flag any urgent issues clearly.",
        ctx,
    )


def ask_custom(question: str) -> str:
    """Ask the advisor any custom question about the bot's state."""
    log.info(f"Custom advisor query: {question[:60]}...")
    ctx = _build_advisor_context()
    return _ask_advisor(question, ctx)


def get_full_briefing() -> str:
    """
    Complete briefing covering all advisory areas.
    Use this for a weekly review or when something feels off.
    """
    log.info("Generating full briefing...")
    ctx = _build_advisor_context()
    return _ask_advisor(
        "Give me a complete briefing covering all of the following in one response: "
        "(1) Portfolio health and performance trend. "
        "(2) Parameter recommendations with specific values. "
        "(3) Watchlist review — add/remove suggestions. "
        "(4) Risk assessment and any red flags. "
        "(5) Top 3 priority actions for this week. "
        "Be specific and actionable throughout.",
        ctx,
    )


# ══════════════════════════════════════════════════════════════════════════════
# Quick test
# ══════════════════════════════════════════════════════════════════════════════

if __name__ == "__main__":
    print("\n=== Module 9: AI Advisor — Test Run ===\n")
    print("Building context snapshot...\n")
    ctx = _build_advisor_context()
    print(ctx)
    print("\n" + "="*60)
    print("Requesting portfolio review from Claude...\n")
    review = get_portfolio_review()
    print(review)

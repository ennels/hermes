"""
main.py — Trading Bot Entry Point
───────────────────────────────────
Full schedule:
  Weekdays 9:45am ET   — main trading cycle
  Every hour market hrs — intraday stop-loss/take-profit monitor
  Daily 9:00am ET      — deposit check + bot health
  Sunday 8:00pm ET     — weekly auto-tune + rebalancer

Run with:  python main.py
"""

import os
import sys
import logging
from datetime import datetime, timezone

from apscheduler.schedulers.blocking import BlockingScheduler
from apscheduler.triggers.cron import CronTrigger
import pytz

from modules.data_fetcher    import fetch_all_tickers
from modules.signal_engine   import generate_all_signals
from modules.arbitration     import arbitrate_all
from modules.risk_filter     import filter_all_decisions, init_db, is_paused
from modules.executor        import execute_all, get_account_info
from modules.notifier        import notify_daily_digest, notify_error, notify_trade
from modules.param_manager   import get_effective_params, load_params
from modules.adaptive_risk   import compute_adaptive_params
from modules.auto_tuner      import (
    run_auto_tune, enable_auto_tune, disable_auto_tune,
    is_auto_tune_enabled, get_auto_tune_log,
)
from modules.intraday_monitor import (
    run_intraday_check, write_heartbeat, check_bot_health,
)
from modules.portfolio_guardian import (
    run_rebalancer, check_for_deposits,
    fetch_current_prices_for_positions,
)
from modules.earnings_play import (
    should_enter_earnings_play, execute_earnings_play,
    manage_post_earnings_exits, get_earnings_play_stats,
    get_active_earnings_plays,
)
from modules.sector_rotation import (
    run_sector_rotation_analysis, get_sector_adjustments,
    get_ticker_adjustments, notify_rotation_update,
)
from modules.watchlist_manager import (
    run_watchlist_manager, notify_watchlist_changes,
    get_watchlist_history,
)
from modules.watchlist_manager import (
    run_watchlist_manager, get_watchlist_history,
)

from config.settings import WATCHLIST, RUN_TIME_ET

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger(__name__)


def run_trading_cycle():
    start = datetime.now(timezone.utc)
    log.info("=" * 60)
    log.info(f"Trading cycle started at {start.strftime('%Y-%m-%d %H:%M UTC')}")
    log.info("=" * 60)
    write_heartbeat()
    if is_paused():
        log.warning("Bot is paused — skipping cycle")
        notify_error("Scheduled cycle skipped: bot is paused.")
        return
    try:
        effective = get_effective_params()
        log.info(
            f"Risk mode: {effective.get('_risk_mode','NORMAL')} | "
            f"stop={effective['stop_loss_pct']:.1%} | "
            f"conf>={effective['min_confidence']:.2f} | "
            f"max_pos=${effective['max_position_usd']:.0f}"
        )
        import config.settings as _s
        _s.STOP_LOSS_PCT    = effective["stop_loss_pct"]
        _s.TAKE_PROFIT_PCT  = effective["take_profit_pct"]
        _s.MAX_POSITION_USD = effective["max_position_usd"]
        _s.MAX_POSITIONS    = effective["max_positions"]
        _s.MIN_CONFIDENCE   = effective["min_confidence"]
        watchlist = effective.get("watchlist", WATCHLIST)
        log.info("Step 1/5: Fetching market data...")
        all_ticker_data = fetch_all_tickers(watchlist)
        if not all_ticker_data:
            raise RuntimeError("No ticker data fetched — aborting cycle")
        log.info("Step 2/5: Generating AI signals...")
        all_signals = generate_all_signals(all_ticker_data)
        log.info("Step 3/5: Arbitrating signals...")
        decisions = arbitrate_all(all_signals)
        log.info("Step 4/5: Applying risk filter...")
        filtered = filter_all_decisions(decisions, all_ticker_data)
        log.info("Step 5/5: Executing approved trades...")
        results = execute_all(filtered)

        # ── Earnings plays ────────────────────────────────────────────────────
        log.info("Checking earnings plays...")
        for ticker, decision in decisions.items():
            ticker_data = all_ticker_data.get(ticker, {})
            enter, reason = should_enter_earnings_play(ticker, ticker_data, decision)
            if enter:
                price = ticker_data.get("price", {}).get("current_price", 0)
                execute_earnings_play(ticker, reason, price, decision.get("confidence", 0.60))
        executed = [d for d in results.values() if d.get("status") == "executed"]
        blocked  = [d for d in results.values() if d.get("approved") is False and d.get("action") != "hold"]
        held     = [t for t, d in results.items() if d.get("action") == "hold"]
        for trade in executed:
            notify_trade(trade)
        account_info = get_account_info()
        notify_daily_digest(executed, blocked, held, account_info)
        elapsed = (datetime.now(timezone.utc) - start).total_seconds()
        log.info(f"Cycle complete in {elapsed:.1f}s — {len(executed)} executed, {len(blocked)} blocked, {len(held)} held")
    except Exception as e:
        log.error(f"Cycle failed: {e}", exc_info=True)
        notify_error(f"Trading cycle error: {e}")


def run_intraday():
    try:
        run_intraday_check()
        # Also manage post-earnings exits during market hours
        from modules.intraday_monitor import get_current_prices
        from modules.risk_filter import get_open_positions
        active = get_active_earnings_plays()
        if active:
            tickers = [p["ticker"] for p in active]
            prices  = get_current_prices(tickers)
            manage_post_earnings_exits(prices)
    except Exception as e:
        log.error(f"Intraday check error: {e}")


def run_morning_checks():
    try:
        log.info("Running morning checks...")
        check_for_deposits()
        check_bot_health()
        # Daily watchlist scan — runs before the 9:45am trading cycle
        run_watchlist_manager()
    except Exception as e:
        log.error(f"Morning checks error: {e}")


def run_weekly_maintenance():
    try:
        log.info("Running weekly maintenance...")
        prices = fetch_current_prices_for_positions()
        run_rebalancer(prices)
        # Sector rotation runs first — results inform watchlist manager
        result = run_sector_rotation_analysis()
        if result:
            notify_rotation_update(result)
        # Auto-tune runs last — sees updated watchlist and rotation
        # (watchlist manager now runs daily in morning checks)
        run_auto_tune()
    except Exception as e:
        log.error(f"Weekly maintenance error: {e}")


# ── Manual controls ───────────────────────────────────────────────────────────
def run_now():
    log.info("Manual run triggered")
    run_trading_cycle()

def pause(reason: str = "Manual pause via CLI"):
    from modules.risk_filter import pause_bot
    pause_bot(reason)

def resume():
    from modules.risk_filter import resume_bot
    resume_bot()

def advise():
    from modules.ai_advisor import get_full_briefing
    print(get_full_briefing())

def review():
    from modules.ai_advisor import get_portfolio_review
    print(get_portfolio_review())

def recommend_params():
    from modules.ai_advisor import get_parameter_recommendations
    print(get_parameter_recommendations())

def ask(question: str):
    from modules.ai_advisor import ask_custom
    print(ask_custom(question))

def set_param(key: str, value):
    from modules.param_manager import set_param as _set
    ok, msg = _set(key, value)
    print(msg)

def show_params():
    from modules.param_manager import get_effective_params
    params = get_effective_params()
    print(f"\nRisk mode: {params.get('_risk_mode', 'NORMAL')}")
    for k in ["stop_loss_pct", "take_profit_pct", "max_position_usd",
              "max_positions", "min_confidence", "watchlist", "adaptive_risk"]:
        print(f"  {k:<25} {params.get(k)}")

def reset_params():
    from modules.param_manager import reset_to_defaults
    reset_to_defaults()
    print("Parameters reset to defaults.")

def tune_now():
    log.info("Manual auto-tune triggered")
    run_auto_tune()

def show_tune_log(limit: int = 10):
    entries = get_auto_tune_log(limit)
    if not entries:
        print("No auto-tune history yet.")
        return
    for e in entries:
        print(f"  {e['timestamp'][:16]}  {e['param']:<25} {e['old_value']} -> {e['new_value']}")
        print(f"    reason: {e['reasoning'][:80]}")

def rebalance_now():
    prices = fetch_current_prices_for_positions()
    trimmed = run_rebalancer(prices)
    print(f"Rebalanced {len(trimmed)} positions.")

def check_deposits():
    result = check_for_deposits()
    if result:
        print(f"New deposit detected. Budget updated to ${result:.2f}")
    else:
        print("No new deposits detected.")

def intraday_now():
    triggered = run_intraday_check()
    print(f"Intraday check complete. Triggered: {len(triggered or [])} sells.")

def earnings_stats():
    """Show earnings play performance stats."""
    stats  = get_earnings_play_stats()
    active = get_active_earnings_plays()
    print(f"\nEarnings Play Stats:")
    print(f"  Total plays:  {stats['total_plays']}")
    print(f"  Active:       {stats['active_plays']}")
    print(f"  Closed:       {stats['closed_plays']}")
    print(f"  Win rate:     {stats['win_rate'] or 'N/A'}")
    print(f"  Avg P&L:      {stats['avg_pnl_pct'] or 'N/A'}%")
    if active:
        print(f"\nActive plays:")
        for p in active:
            print(f"  {p['ticker']}: opened {p['opened_at'][:10]}, earnings {p['earnings_date']}")

def sector_rotation():
    """Show current sector rotation adjustments."""
    adjustments = get_sector_adjustments()
    if not adjustments:
        print("No sector rotation data yet — runs Sunday 8pm ET.")
        return
    print("\nCurrent sector rotation adjustments:")
    for sector, adj in adjustments.items():
        bar = "▲" if adj["signal"] == "hot" else "▼" if adj["signal"] == "cold" else "─"
        print(f"  {bar} {sector:<20} conf:{adj['conf_delta']:+.2f}  "
              f"pos:{adj['pos_delta']:+.0%}  {adj['reasoning'][:60]}")

def rotation_now():
    """Trigger an immediate sector rotation analysis."""
    log.info("Manual sector rotation analysis triggered")
    result = run_sector_rotation_analysis()
    if result:
        notify_rotation_update(result)
        print(f"Analysis complete: {result['summary']}")
    else:
        print("Analysis failed — check logs")

def watchlist_now():
    """Trigger immediate watchlist discovery and cleanup."""
    log.info("Manual watchlist manager triggered")
    result = run_watchlist_manager()
    notify_watchlist_changes(result)
    print(f"Watchlist: +{len(result['additions'])} -{len(result['removals'])} = {result['new_count']} tickers")
    if result['additions']:
        print(f"Added: {', '.join(a['ticker'] for a in result['additions'])}")
    if result['removals']:
        print(f"Removed: {', '.join(r['ticker'] for r in result['removals'])}")

def watchlist_history(limit: int = 10):
    """Show recent watchlist changes."""
    history = get_watchlist_history(limit)
    if not history:
        print("No watchlist changes yet.")
        return
    for h in history:
        print(f"  {h['timestamp'][:10]} {h['action'].upper():6} {h['ticker']}: {h['reason'][:60]}")

def watchlist_now():
    """Trigger an immediate watchlist management cycle."""
    log.info("Manual watchlist manager triggered")
    changes = run_watchlist_manager()
    if changes:
        print(f"Complete: +{len(changes['adds'])} added, -{len(changes['removes'])} removed")
        for a in changes["adds"]:
            print(f"  + {a['ticker']}: {a['reason']}")
        for r in changes["removes"]:
            print(f"  - {r['ticker']}: {r['reason']}")
    else:
        print("No changes made")

def show_watchlist_log(limit: int = 10):
    """Show recent watchlist changes."""
    history = get_watchlist_history(limit)
    if not history:
        print("No watchlist changes yet.")
        return
    print(f"
Last {len(history)} watchlist changes:")
    for h in history:
        action = "+" if h["action"] == "add" else "-"
        print(f"  {h['timestamp'][:10]}  {action}{h['ticker']:<6}  {h['reason'][:60]}")


# ── Scheduler ─────────────────────────────────────────────────────────────────
if __name__ == "__main__":
    init_db()
    log.info("Trading bot starting up...")
    log.info(f"Watchlist: {', '.join(WATCHLIST)}")
    log.info(f"Scheduled run time: {RUN_TIME_ET} ET, Mon-Fri")
    log.info(f"Auto-tuner: {'ENABLED' if is_auto_tune_enabled() else 'DISABLED'}")

    hour, minute = RUN_TIME_ET.split(":")
    et_tz = pytz.timezone("America/New_York")
    scheduler = BlockingScheduler(timezone=et_tz)

    scheduler.add_job(run_trading_cycle, CronTrigger(day_of_week="mon-fri", hour=int(hour), minute=int(minute), timezone=et_tz), id="trading_cycle", name="Daily trading cycle", misfire_grace_time=300)
    scheduler.add_job(run_intraday, CronTrigger(day_of_week="mon-fri", hour="9-15", minute="30", timezone=et_tz), id="intraday_monitor", name="Intraday monitor", misfire_grace_time=600)
    scheduler.add_job(run_morning_checks, CronTrigger(day_of_week="mon-fri", hour=9, minute=0, timezone=et_tz), id="morning_checks", name="Morning checks", misfire_grace_time=300)
    scheduler.add_job(run_weekly_maintenance, CronTrigger(day_of_week="sun", hour=20, minute=0, timezone=et_tz), id="weekly_maintenance", name="Weekly maintenance", misfire_grace_time=3600)

    log.info("Scheduler started. Jobs: daily 9:45 ET | intraday hourly | morning 9am | weekly Sun 8pm")

    try:
        scheduler.start()
    except KeyboardInterrupt:
        log.info("Bot stopped by user")

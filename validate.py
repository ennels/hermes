"""
validate.py — Full System Validation
──────────────────────────────────────
Tests all modules without making any trades, parameter changes,
or real API calls that cost money. Safe to run anytime.

Run with: python validate.py
"""

import os
import sys
import json
from datetime import datetime, timezone

sys.path.append(os.path.dirname(__file__))

PASS = "✓"
FAIL = "✗"
SKIP = "○"
results = []

def check(name, fn):
    try:
        result = fn()
        status = PASS if result else FAIL
        results.append((status, name, str(result)[:80] if result else "returned falsy"))
    except Exception as e:
        results.append((FAIL, name, str(e)[:80]))

def check_pass(name, fn):
    """Use when function returns None on success — just check it doesn't crash."""
    try:
        fn()
        results.append((PASS, name, "no errors"))
    except Exception as e:
        results.append((FAIL, name, str(e)[:80]))

print("\n=== Full System Validation ===")
print(f"Time: {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')}\n")

# ── Module 1: Data Fetcher ────────────────────────────────────────────────────
print("Module 1: Data Fetcher")
try:
    from modules.data_fetcher import fetch_ticker_data
    data = fetch_ticker_data("SPY")
    if data and data.get("price", {}).get("current_price", 0) > 0:
        results.append((PASS, "Data fetcher", f"SPY @ ${data['price']['current_price']}"))
    else:
        results.append((FAIL, "Data fetcher", "No price data returned"))
except Exception as e:
    results.append((FAIL, "Data fetcher", str(e)[:80]))

# ── Module 2: Signal Engine (dry run — uses mock data, no real AI call cost) ──
print("Module 2: Signal Engine")
try:
    from modules.signal_engine import _build_prompt
    mock = {
        "ticker": "SPY", "summary": "SPY @ $500. RSI=50. MACD neutral.",
        "price": {"current_price": 500, "rsi": 50, "macd_histogram": 0},
        "news": {"article_count": 2, "sentiment_label": "neutral", "headlines": []},
        "calendar": {"earnings_soon": False},
    }
    prompt = _build_prompt(mock)
    if prompt and len(prompt) > 50:
        results.append((PASS, "Signal engine prompt builder", f"{len(prompt)} chars"))
    else:
        results.append((FAIL, "Signal engine prompt builder", "Empty prompt"))
except Exception as e:
    results.append((FAIL, "Signal engine prompt builder", str(e)[:80]))

# ── Module 3: Arbitration ─────────────────────────────────────────────────────
print("Module 3: Arbitration")
try:
    from modules.arbitration import arbitrate
    mock_signals = {
        "ticker": "SPY",
        "claude": {"ticker": "SPY", "action": "buy", "confidence": 0.72, "reasoning": "test", "signals_used": [], "model": "claude"},
        "gemini": {"ticker": "SPY", "action": "buy", "confidence": 0.80, "reasoning": "test", "signals_used": [], "model": "gemini"},
    }
    result = arbitrate(mock_signals)
    if result and result.get("action"):
        results.append((PASS, "Arbitration", f"SPY → {result['action'].upper()} @ {result['confidence']:.2f}"))
    else:
        results.append((FAIL, "Arbitration", "No decision returned"))
except Exception as e:
    results.append((FAIL, "Arbitration", str(e)[:80]))

# ── Module 4: Risk Filter ─────────────────────────────────────────────────────
print("Module 4: Risk Filter")
try:
    from modules.risk_filter import is_paused, get_open_positions
    paused    = is_paused()
    positions = get_open_positions()
    results.append((PASS, "Risk filter", f"paused={paused}, positions={len(positions)}"))
except Exception as e:
    results.append((FAIL, "Risk filter", str(e)[:80]))

# ── Module 5: Executor (read-only — no trades placed) ────────────────────────
print("Module 5: Executor")
try:
    from modules.executor import get_account_info, get_alpaca_positions
    info = get_account_info()
    if info.get("equity", 0) >= 0:
        results.append((PASS, "Executor (Alpaca connection)", f"equity=${info['equity']:,.0f}, paper={info['paper_trading']}"))
    else:
        results.append((FAIL, "Executor", "Could not read account"))
except Exception as e:
    results.append((FAIL, "Executor", str(e)[:80]))

# ── Module 6: Notifier (checks config only — no messages sent) ────────────────
print("Module 6: Notifier")
try:
    from modules.notifier import send_email, send_sms
    import os
    sg_key     = os.getenv("SENDGRID_API_KEY", "")
    twilio_sid = os.getenv("TWILIO_ACCOUNT_SID", "")
    if sg_key.startswith("SG.") and twilio_sid.startswith("AC"):
        results.append((PASS, "Notifier config", "SendGrid + Twilio keys present"))
    else:
        results.append((FAIL, "Notifier config", "Missing API keys in .env"))
except Exception as e:
    results.append((FAIL, "Notifier", str(e)[:80]))

# ── Module 7: Adaptive Risk ───────────────────────────────────────────────────
print("Module 7: Adaptive Risk")
try:
    from modules.adaptive_risk import compute_adaptive_params, get_market_volatility
    vol    = get_market_volatility()
    params = compute_adaptive_params()
    results.append((PASS, "Adaptive risk", f"mode={params.get('_risk_mode','NORMAL')}, vol={vol['level']}"))
except Exception as e:
    results.append((FAIL, "Adaptive risk", str(e)[:80]))

# ── Module 8: Parameter Manager ──────────────────────────────────────────────
print("Module 8: Parameter Manager")
try:
    from modules.param_manager import get_effective_params, load_params
    params = get_effective_params()
    if params.get("stop_loss_pct"):
        results.append((PASS, "Parameter manager", f"stop={params['stop_loss_pct']:.2%}, conf={params['min_confidence']:.2f}"))
    else:
        results.append((FAIL, "Parameter manager", "No params returned"))
except Exception as e:
    results.append((FAIL, "Parameter manager", str(e)[:80]))

# ── Module 9: AI Advisor (skipped — would make real API call) ─────────────────
results.append(("○", "AI Advisor", "skipped — would make live API call"))

# ── Module 10: Auto-Tuner (dry run only) ──────────────────────────────────────
print("Module 10: Auto-Tuner")
try:
    from modules.auto_tuner import _load_state, _check_emergency_freeze, is_auto_tune_enabled
    state    = _load_state()
    freeze, _ = _check_emergency_freeze()
    enabled  = is_auto_tune_enabled()
    results.append((PASS, "Auto-tuner state", f"enabled={enabled}, frozen={freeze}"))
except Exception as e:
    results.append((FAIL, "Auto-tuner", str(e)[:80]))

# ── Module 11: Intraday Monitor ───────────────────────────────────────────────
print("Module 11: Intraday Monitor")
try:
    from modules.intraday_monitor import is_market_open, get_current_prices, write_heartbeat
    market_open = is_market_open()
    write_heartbeat()
    positions_dict = get_open_positions() if True else {}
    tickers = list(positions_dict.keys()) or ["SPY"]
    prices  = get_current_prices(tickers[:1])
    results.append((PASS, "Intraday monitor", f"market_open={market_open}, prices={prices}"))
except Exception as e:
    results.append((FAIL, "Intraday monitor", str(e)[:80]))

# ── Module 12: Portfolio Guardian ─────────────────────────────────────────────
print("Module 12: Portfolio Guardian")
try:
    from modules.portfolio_guardian import get_sector_exposure, fetch_current_prices_for_positions
    prices   = fetch_current_prices_for_positions()
    exposure = get_sector_exposure(prices)
    results.append((PASS, "Portfolio guardian", f"sectors tracked: {list(exposure.keys()) or 'none (no positions)'}"))
except Exception as e:
    results.append((FAIL, "Portfolio guardian", str(e)[:80]))

# ── Module 13: Signal Cache ───────────────────────────────────────────────────
print("Module 13: Signal Cache")
try:
    from modules.signal_cache import get_cache_stats, should_skip_ai_calls
    stats = get_cache_stats()
    mock  = {
        "ticker": "SOXX",
        "price": {"current_price": 180, "day_change_pct": 0.1, "rsi": 51, "macd_histogram": 0.05},
        "news": {"article_count": 0, "sentiment_label": "neutral"},
        "calendar": {"earnings_soon": False},
    }
    skip, _ = should_skip_ai_calls(mock)
    results.append((PASS, "Signal cache", f"entries={stats['valid_entries']}, pre-filter working={skip}"))
except Exception as e:
    results.append((FAIL, "Signal cache", str(e)[:80]))


# ── Module 14: Earnings Play ──────────────────────────────────────────────────
print("Module 14: Earnings Play")
try:
    from modules.earnings_play import get_active_earnings_plays, get_earnings_play_stats, ETF_EXCLUSIONS
    active = get_active_earnings_plays()
    stats  = get_earnings_play_stats()
    etf_ok = "SPY" in ETF_EXCLUSIONS and "NVDA" not in ETF_EXCLUSIONS
    results.append((PASS, "Earnings play", f"active={len(active)}, etf_exclusions_ok={etf_ok}"))
except Exception as e:
    results.append((FAIL, "Earnings play", str(e)[:80]))

# ── Module 15: Sector Rotation ────────────────────────────────────────────────
print("Module 15: Sector Rotation")
try:
    from modules.sector_rotation import get_sector_adjustments, get_ticker_adjustments, TICKER_SECTOR
    adj   = get_ticker_adjustments("NVDA")
    known = "NVDA" in TICKER_SECTOR
    results.append((PASS, "Sector rotation", f"NVDA in sector map={known}, signal={adj['signal']}"))
except Exception as e:
    results.append((FAIL, "Sector rotation", str(e)[:80]))

# ── Concentration guard integration ──────────────────────────────────────────
print("Concentration guard")
try:
    from modules.portfolio_guardian import check_concentration
    ok, reason = check_concentration("NVDA", {})
    results.append((PASS, "Concentration guard", f"callable, returned ok={ok}"))
except Exception as e:
    results.append((FAIL, "Concentration guard", str(e)[:80]))

# ── Arbitration rotation integration ─────────────────────────────────────────
print("Arbitration + rotation")
try:
    from modules.arbitration import arbitrate_all
    from modules.sector_rotation import get_ticker_adjustments
    results.append((PASS, "Arbitration + rotation", "both importable"))
except Exception as e:
    results.append((FAIL, "Arbitration + rotation", str(e)[:80]))

# ── Heartbeat file ────────────────────────────────────────────────────────────
print("Heartbeat")
try:
    from modules.intraday_monitor import HEARTBEAT_PATH
    if os.path.exists(HEARTBEAT_PATH):
        with open(HEARTBEAT_PATH) as f:
            ts = f.read().strip()
        results.append((PASS, "Heartbeat file", f"last written: {ts[:16]}"))
    else:
        results.append((FAIL, "Heartbeat file", f"not found at {HEARTBEAT_PATH}"))
except Exception as e:
    results.append((FAIL, "Heartbeat file", str(e)[:80]))

# ── .env keys present ─────────────────────────────────────────────────────────
print("Environment")
try:
    from dotenv import load_dotenv
    load_dotenv()
    required = [
        "ALPACA_API_KEY", "ALPACA_SECRET_KEY",
        "ANTHROPIC_API_KEY", "GEMINI_API_KEY",
        "FINNHUB_API_KEY", "SENDGRID_API_KEY",
        "TWILIO_ACCOUNT_SID", "TWILIO_AUTH_TOKEN",
    ]
    missing = [k for k in required if not os.getenv(k)]
    if not missing:
        results.append((PASS, "Environment (.env)", "all required keys present"))
    else:
        results.append((FAIL, "Environment (.env)", f"missing: {', '.join(missing)}"))
except Exception as e:
    results.append((FAIL, "Environment", str(e)[:80]))

# ── Print results ─────────────────────────────────────────────────────────────
print("\n" + "=" * 60)
print("RESULTS")
print("=" * 60)

passed = sum(1 for r in results if r[0] == PASS)
failed = sum(1 for r in results if r[0] == FAIL)
skipped = sum(1 for r in results if r[0] == SKIP)

for status, name, detail in results:
    print(f"  {status}  {name:<35} {detail}")

print("=" * 60)
print(f"  {passed} passed  |  {failed} failed  |  {skipped} skipped")
print("=" * 60)

if failed > 0:
    print("\nFailed checks need attention before next trading cycle.")
else:
    print("\nAll systems operational.")

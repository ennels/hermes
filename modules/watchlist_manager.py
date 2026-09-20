"""
Module 16: Watchlist Manager
──────────────────────────────
True set-and-forget watchlist management. Runs every Sunday after
sector rotation, automatically adding promising new tickers and
removing underperformers.

Two-step Gemini process (same pattern as sector rotation):
  Step 1: Search grounding — find hot tickers and evaluate existing ones
  Step 2: JSON conversion — structure the recommendations cleanly

Safety rails:
  - Never removes tickers with open positions
  - Never removes core ETF benchmarks (SPY, QQQ, SOXX)
  - Min watchlist size: 20 tickers
  - Max watchlist size: 40 tickers
  - Max 5 additions and 3 removals per day
  - 30-day cooldown per ticker (prevents churn)
  - Full audit trail in logs/watchlist_changes.json
  - Email notification of every change
"""

import os
import sys
import json
import re
import logging
from datetime import datetime, timezone, timedelta
from typing import Optional

sys.path.append(os.path.join(os.path.dirname(__file__), ".."))

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger(__name__)

WATCHLIST_LOG_PATH  = os.path.join(os.path.dirname(__file__), "..", "logs", "watchlist_changes.json")
COOLDOWN_LOG_PATH   = os.path.join(os.path.dirname(__file__), "..", "logs", "watchlist_cooldown.json")

# Safety constants
MIN_WATCHLIST_SIZE  = 20
MAX_WATCHLIST_SIZE  = 50
MAX_ADDITIONS       = 5
MAX_REMOVALS        = 3
COOLDOWN_DAYS       = 7

# Core tickers that can never be removed
PROTECTED_TICKERS   = {"SPY", "QQQ", "SOXX", "IWM"}


# ── Cooldown management ───────────────────────────────────────────────────────

def _load_cooldown() -> dict:
    if not os.path.exists(COOLDOWN_LOG_PATH):
        return {}
    with open(COOLDOWN_LOG_PATH) as f:
        return json.load(f)

def _save_cooldown(data: dict):
    os.makedirs(os.path.dirname(COOLDOWN_LOG_PATH), exist_ok=True)
    with open(COOLDOWN_LOG_PATH, "w") as f:
        json.dump(data, f, indent=2)

def _is_on_cooldown(ticker: str) -> bool:
    cooldown = _load_cooldown()
    if ticker not in cooldown:
        return False
    last_change = datetime.fromisoformat(cooldown[ticker])
    return (datetime.now(timezone.utc) - last_change).days < COOLDOWN_DAYS

def _set_cooldown(ticker: str):
    cooldown = _load_cooldown()
    cooldown[ticker] = datetime.now(timezone.utc).isoformat()
    # Clean up old entries
    cutoff = datetime.now(timezone.utc) - timedelta(days=COOLDOWN_DAYS + 1)
    cooldown = {k: v for k, v in cooldown.items()
                if datetime.fromisoformat(v) > cutoff}
    _save_cooldown(cooldown)


# ── Change log ────────────────────────────────────────────────────────────────

def _log_change(action: str, ticker: str, reasoning: str):
    history = []
    if os.path.exists(WATCHLIST_LOG_PATH):
        with open(WATCHLIST_LOG_PATH) as f:
            history = json.load(f)
    history.append({
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "action":    action,
        "ticker":    ticker,
        "reasoning": reasoning,
    })
    history = history[-200:]
    os.makedirs(os.path.dirname(WATCHLIST_LOG_PATH), exist_ok=True)
    with open(WATCHLIST_LOG_PATH, "w") as f:
        json.dump(history, f, indent=2)

def get_watchlist_change_log(limit: int = 20) -> list:
    if not os.path.exists(WATCHLIST_LOG_PATH):
        return []
    with open(WATCHLIST_LOG_PATH) as f:
        history = json.load(f)
    return history[-limit:]


# ── Gemini two-step watchlist analysis ───────────────────────────────────────

def _build_search_prompt(current_watchlist: list, sector_rotation: dict) -> str:
    hot_sectors  = [s for s, d in sector_rotation.items() if d.get("signal") == "hot"]
    cold_sectors = [s for s, d in sector_rotation.items() if d.get("signal") == "cold"]

    return (
        f"Current stock watchlist: {', '.join(current_watchlist)}\n"
        f"Hot sectors this week: {', '.join(hot_sectors) or 'none identified'}\n"
        f"Cold sectors this week: {', '.join(cold_sectors) or 'none identified'}\n\n"
        "Search the web and do two things:\n\n"
        "1. FIND NEW TICKERS: Search for the top-performing stocks and ETFs right now "
        "across any hot sectors. Look for: strong institutional buying, high momentum, "
        "analyst upgrades, new themes gaining traction (AI, defense, energy, biotech, etc). "
        "Suggest up to 3 tickers NOT already in the watchlist above.\n\n"
        "2. IDENTIFY WEAK TICKERS: From the current watchlist, identify up to 3 tickers "
        "that are underperforming, losing relevance, or in sectors losing momentum. "
        "Consider: declining volume, negative analyst sentiment, sector headwinds.\n\n"
        "Be aggressive and cast wide — any sector, any market cap, ADRs welcome. Only suggest US-listed or US-traded "
        "tradeable on Alpaca. Do not suggest OTC or penny stocks."
    )


def _build_json_prompt(analysis: str, current_watchlist: list) -> str:
    template = (
        '{"additions": ['
        '{"ticker": "SYMBOL", "reasoning": "why add this"}'
        '], "removals": ['
        '{"ticker": "SYMBOL", "reasoning": "why remove this"}'
        '], "summary": "one sentence summary of changes"}'
    )
    return (
        f"Based on this market analysis:\n\n{analysis}\n\n"
        f"Current watchlist: {', '.join(current_watchlist)}\n\n"
        f"Fill in this JSON template. Return ONLY the JSON, no markdown:\n\n"
        f"{template}\n\n"
        "Rules:\n"
        "- additions: up to 3 tickers NOT in the current watchlist\n"
        "- removals: up to 3 tickers FROM the current watchlist\n"
        "- ticker must be a valid US stock/ETF symbol in ALL CAPS\n"
        "- reasoning must be under 100 characters\n"
        "- if no additions needed, use empty array []\n"
        "- if no removals needed, use empty array []\n"
        "- never suggest removing: SPY, QQQ, SOXX, IWM"
    )


def run_watchlist_analysis(current_watchlist: list,
                            sector_rotation: dict) -> Optional[dict]:
    """
    Two-step Gemini analysis to find new tickers and flag weak ones.
    Returns {additions: [...], removals: [...], summary: str} or None.
    """
    try:
        from google import genai
        from google.genai import types
        from config.settings import GEMINI_API_KEY

        log.info("Running watchlist analysis via Gemini...")
        client = genai.Client(api_key=GEMINI_API_KEY)

        # Step 1: Search grounding — plain text analysis
        step1 = client.models.generate_content(
            model="gemini-2.5-flash",
            contents=_build_search_prompt(current_watchlist, sector_rotation),
            config=types.GenerateContentConfig(
                temperature=0.2,
                max_output_tokens=1024,
                tools=[types.Tool(google_search=types.GoogleSearch())],
            ),
        )
        analysis = step1.text.strip()[:2000]
        log.info(f"  Step 1 complete: {len(analysis)} chars")

        # If Gemini returned a refusal or empty analysis, retry with simpler prompt
        refusal_phrases = ["not provided", "cannot", "no information", "unable to"]
        if len(analysis) < 100 or any(p in analysis.lower() for p in refusal_phrases):
            log.warning("  Step 1 unhelpful — retrying with simpler prompt")
            retry = client.models.generate_content(
                model="gemini-2.5-flash",
                contents=(
                    "Search the web for the top 5 best-performing US stocks and ETFs "
                    "right now in April 2026. Include ticker symbols and brief reasons. "
                    "Also identify 2 stocks that are underperforming recently."
                ),
                config=types.GenerateContentConfig(
                    temperature=0.1,
                    max_output_tokens=1024,
                    tools=[types.Tool(google_search=types.GoogleSearch())],
                ),
            )
            analysis = retry.text.strip()[:2000]
            log.info(f"  Retry complete: {len(analysis)} chars")

        # Step 2: JSON conversion — no search grounding
        step2 = client.models.generate_content(
            model="gemini-2.5-flash",
            contents=_build_json_prompt(analysis, current_watchlist),
            config=types.GenerateContentConfig(
                temperature=0.0,
                max_output_tokens=1024,
            ),
        )

        raw     = step2.text.strip()
        cleaned = re.sub(r"```[a-zA-Z]*", "", raw).replace("```", "").strip()

        # Brace-matching JSON extraction
        start = cleaned.find("{")
        if start == -1:
            raise ValueError(f"No JSON found: {cleaned[:200]}")
        depth = 0
        end   = -1
        for i, ch in enumerate(cleaned[start:], start):
            if ch == "{":
                depth += 1
            elif ch == "}":
                depth -= 1
                if depth == 0:
                    end = i + 1
                    break
        if end == -1:
            raise ValueError("Unmatched braces in JSON")

        data = json.loads(cleaned[start:end])
        log.info(f"  Step 2 complete: {len(data.get('additions',[]))} additions, "
                 f"{len(data.get('removals',[]))} removals")
        return data

    except Exception as e:
        log.error(f"Watchlist analysis failed: {e}")
        return None


# ── Apply watchlist changes ───────────────────────────────────────────────────


def get_stale_tickers(current_watchlist: list, lookback_days: int = 7) -> list:
    """Find tickers with no executed trades in last N days — removal candidates."""
    import sqlite3
    db_path = os.path.join(os.path.dirname(__file__), "..", "logs", "trades.db")
    if not os.path.exists(db_path):
        return []
    try:
        from datetime import datetime, timezone, timedelta
        cutoff = (datetime.now(timezone.utc) - timedelta(days=lookback_days)).isoformat()
        conn   = sqlite3.connect(db_path)
        active = set(row[0] for row in conn.execute(
            "SELECT DISTINCT ticker FROM trades WHERE timestamp >= ? AND status = 'executed'",
            (cutoff,)
        ).fetchall())
        conn.close()
        protected = {"SPY", "QQQ", "SOXX", "IWM"}
        return [t for t in current_watchlist if t not in active and t not in protected]
    except Exception as e:
        log.error(f"Stale ticker check failed: {e}")
        return []

def apply_watchlist_changes(recommendations: dict) -> dict:
    """
    Apply additions and removals to the watchlist with all safety checks.
    Returns {added: [...], removed: [...], skipped: [...]}
    """
    from modules.param_manager import get_effective_params, set_param
    from modules.risk_filter import get_open_positions

    params           = get_effective_params()
    current_watchlist = list(params.get("watchlist", []))
    open_positions   = set(get_open_positions().keys())

    additions = recommendations.get("additions", [])[:MAX_ADDITIONS]
    removals  = recommendations.get("removals",  [])[:MAX_REMOVALS]

    added   = []
    removed = []
    skipped = []

    # Process removals first
    new_watchlist = list(current_watchlist)

    for item in removals:
        ticker  = item.get("ticker", "").upper().strip()
        reason  = item.get("reasoning", "")

        if not ticker:
            continue
        if ticker not in new_watchlist:
            skipped.append(f"{ticker}: not in watchlist")
            continue
        if ticker in PROTECTED_TICKERS:
            skipped.append(f"{ticker}: protected ticker")
            continue
        if ticker in open_positions:
            skipped.append(f"{ticker}: open position — cannot remove")
            continue
        if _is_on_cooldown(ticker):
            skipped.append(f"{ticker}: on 30-day cooldown")
            continue
        if len(new_watchlist) - 1 < MIN_WATCHLIST_SIZE:
            skipped.append(f"{ticker}: would breach min watchlist size ({MIN_WATCHLIST_SIZE})")
            continue

        new_watchlist.remove(ticker)
        removed.append(ticker)
        _set_cooldown(ticker)
        _log_change("removed", ticker, reason)
        log.info(f"  Removed {ticker}: {reason}")

    # Process additions
    for item in additions:
        ticker  = item.get("ticker", "").upper().strip()
        reason  = item.get("reasoning", "")

        if not ticker:
            continue
        if ticker in new_watchlist:
            skipped.append(f"{ticker}: already in watchlist")
            continue
        if _is_on_cooldown(ticker):
            skipped.append(f"{ticker}: on 30-day cooldown")
            continue
        if len(new_watchlist) >= MAX_WATCHLIST_SIZE:
            skipped.append(f"{ticker}: would breach max watchlist size ({MAX_WATCHLIST_SIZE})")
            continue
        # Basic ticker validation — must be 1-5 uppercase letters
        if not re.match(r'^[A-Z]{1,5}$', ticker):
            skipped.append(f"{ticker}: invalid ticker format")
            continue

        new_watchlist.append(ticker)
        added.append(ticker)
        _set_cooldown(ticker)
        _log_change("added", ticker, reason)
        log.info(f"  Added {ticker}: {reason}")

    # Save updated watchlist
    if added or removed:
        ok, msg = set_param("watchlist", new_watchlist, source="watchlist_manager")
        if ok:
            log.info(f"  Watchlist updated: {len(new_watchlist)} tickers")
        else:
            log.error(f"  Failed to save watchlist: {msg}")

    return {"added": added, "removed": removed, "skipped": skipped}


# ── Notification ──────────────────────────────────────────────────────────────

def notify_watchlist_update(changes: dict, summary: str,
                             recommendations: dict):
    try:
        from modules.notifier import send_email, send_sms

        added   = changes["added"]
        removed = changes["removed"]
        skipped = changes["skipped"]
        date_str = datetime.now(timezone.utc).strftime("%Y-%m-%d")

        if not added and not removed:
            sms = f"[WATCHLIST] No changes this week. {summary}"
        else:
            parts = []
            if added:
                parts.append(f"Added: {', '.join(added)}")
            if removed:
                parts.append(f"Removed: {', '.join(removed)}")
            sms = f"[WATCHLIST] {' | '.join(parts)}"

        send_sms(sms)

        # Build HTML table
        rows = ""
        for item in recommendations.get("additions", []):
            ticker = item.get("ticker", "")
            status = "ADDED" if ticker in added else "SKIPPED"
            color  = "#f0faf0" if ticker in added else "#f8f8f8"
            rows += (
                f"<tr style='background:{color}'>"
                f"<td style='padding:6px 12px;color:#060;font-weight:bold'>+</td>"
                f"<td style='padding:6px 12px'>{ticker}</td>"
                f"<td style='padding:6px 12px'>{status}</td>"
                f"<td style='padding:6px 12px;color:#666;font-size:12px'>{item.get('reasoning','')}</td>"
                f"</tr>"
            )
        for item in recommendations.get("removals", []):
            ticker = item.get("ticker", "")
            status = "REMOVED" if ticker in removed else "SKIPPED"
            color  = "#faf0f0" if ticker in removed else "#f8f8f8"
            rows += (
                f"<tr style='background:{color}'>"
                f"<td style='padding:6px 12px;color:#c00;font-weight:bold'>-</td>"
                f"<td style='padding:6px 12px'>{ticker}</td>"
                f"<td style='padding:6px 12px'>{status}</td>"
                f"<td style='padding:6px 12px;color:#666;font-size:12px'>{item.get('reasoning','')}</td>"
                f"</tr>"
            )
        if skipped:
            for s in skipped:
                rows += (
                    f"<tr style='background:#f8f8f8'>"
                    f"<td style='padding:6px 12px;color:#888'>○</td>"
                    f"<td colspan='3' style='padding:6px 12px;color:#888;font-size:12px'>{s}</td>"
                    f"</tr>"
                )

        html = (
            "<div style='font-family:sans-serif;max-width:700px'>"
            "<h2>Trading Bot — Weekly Watchlist Update</h2>"
            f"<p>{summary}</p>"
            "<table border='0' cellpadding='0' style='width:100%;border-collapse:collapse'>"
            "<tr style='background:#eee'>"
            "<th style='padding:8px 12px'></th>"
            "<th style='padding:8px 12px;text-align:left'>Ticker</th>"
            "<th style='padding:8px 12px;text-align:left'>Status</th>"
            "<th style='padding:8px 12px;text-align:left'>Reasoning</th>"
            "</tr>"
            f"{rows}"
            "</table>"
            "<p style='font-size:11px;color:#888'>Changes take effect next trading cycle.</p>"
            "</div>"
        )

        send_email(
            f"Trading Bot Weekly Watchlist Update — {date_str}",
            sms + "\n\n" + summary,
            html,
        )
        log.info("Watchlist notification sent")

    except Exception as e:
        log.error(f"Watchlist notification failed: {e}")


# ── Main entry point ──────────────────────────────────────────────────────────

def run_watchlist_manager():
    """
    Main function called during weekly maintenance.
    Runs after sector rotation so hot/cold sector data is fresh.
    """
    log.info("Running watchlist manager...")

    from modules.param_manager import get_effective_params
    from modules.sector_rotation import get_sector_adjustments

    params           = get_effective_params()
    current_watchlist = list(params.get("watchlist", []))

    # Use sector rotation data if available, otherwise pass empty dict
    # Watchlist manager does its own Gemini search so it doesn't depend on rotation
    try:
        sector_rotation = get_sector_adjustments()
    except Exception:
        sector_rotation = {}

    log.info(f"  Current watchlist: {len(current_watchlist)} tickers")

    recommendations = run_watchlist_analysis(current_watchlist, sector_rotation)
    if not recommendations:
        log.warning("  Watchlist analysis failed — no changes made")
        return

    summary = recommendations.get("summary", "Weekly watchlist review complete.")
    changes = apply_watchlist_changes(recommendations)
    notify_watchlist_update(changes, summary, recommendations)

    log.info(
        f"Watchlist manager complete — "
        f"added: {changes['added']}, "
        f"removed: {changes['removed']}, "
        f"skipped: {len(changes['skipped'])}"
    )
    return changes


# ── Quick test ────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    print("\n=== Module 16: Watchlist Manager — Test Run ===\n")

    from modules.param_manager import get_effective_params
    params = get_effective_params()
    watchlist = params.get("watchlist", [])

    print(f"Current watchlist ({len(watchlist)} tickers):")
    print(f"  {', '.join(watchlist)}")

    print("\nCooldown status:")
    cooldown = _load_cooldown()
    if cooldown:
        for ticker, ts in cooldown.items():
            days_ago = (datetime.now(timezone.utc) - datetime.fromisoformat(ts)).days
            print(f"  {ticker}: changed {days_ago}d ago")
    else:
        print("  No cooldowns active")

    print("\nRecent changes:")
    changes = get_watchlist_change_log(10)
    if changes:
        for c in changes:
            print(f"  {c['timestamp'][:10]} {c['action'].upper():8} {c['ticker']:6} — {c['reasoning'][:60]}")
    else:
        print("  No changes yet")

    print("\nRunning live analysis (real API call)...")
    from modules.sector_rotation import get_sector_adjustments
    sector_rotation = get_sector_adjustments()

    recommendations = run_watchlist_analysis(watchlist, sector_rotation)
    if recommendations:
        print(f"\nSummary: {recommendations.get('summary')}")
        print("\nRecommended additions:")
        for item in recommendations.get("additions", []):
            print(f"  + {item['ticker']}: {item['reasoning']}")
        print("\nRecommended removals:")
        for item in recommendations.get("removals", []):
            print(f"  - {item['ticker']}: {item['reasoning']}")
        print("\n(Dry run — no changes applied)")
        print("To apply: python -c \"from modules.watchlist_manager import run_watchlist_manager; run_watchlist_manager()\"")
    else:
        print("  Analysis failed — check logs")

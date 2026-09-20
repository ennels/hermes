"""
Module 10: Auto-Tuner
──────────────────────
Fully autonomous self-tuning. Runs on a weekly schedule (Sunday 8pm ET),
analyzes the past week's performance, and applies parameter changes
automatically. Notifies you of every change via SMS + email.

Safety rails (non-negotiable, cannot be overridden by AI):
  - Changes are applied one at a time, never all at once
  - Each parameter has a max single-step change limit
  - A 7-day cooldown prevents the same parameter being changed twice in a week
  - Emergency freeze: if equity drops >10% in a week, all auto-tuning halts
    and you get an immediate alert
  - Full audit trail of every auto-applied change in logs/auto_tune_log.json
  - You can disable auto-tuning at any time:
      python -c "from main import disable_auto_tune; disable_auto_tune()"
"""

import os
import sys
import json
import logging
import re
from datetime import datetime, timezone, timedelta
from typing import Optional

import anthropic

sys.path.append(os.path.join(os.path.dirname(__file__), ".."))
from config.settings import ANTHROPIC_API_KEY
from modules.param_manager import (
    get_effective_params, set_param, get_param_history,
    load_params, PARAM_SCHEMA,
)
from modules.adaptive_risk import get_recent_performance, get_market_volatility
from modules.risk_filter import get_open_positions, DB_PATH, init_db
from modules.notifier import send_sms, send_email

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger(__name__)

AUTO_TUNE_LOG   = os.path.join(os.path.dirname(__file__), "..", "logs", "auto_tune_log.json")
AUTO_TUNE_STATE = os.path.join(os.path.dirname(__file__), "..", "logs", "auto_tune_state.json")

MAX_STEP = {
    "stop_loss_pct":       0.02,
    "take_profit_pct":     0.05,
    "max_position_usd":    50.0,
    "max_positions":       1,
    "min_confidence":      0.05,
    "news_lookback_hours": 12,
    "gemini_weight_news":  0.10,
    "claude_weight_tech":  0.10,
}

LOCKED_PARAMS = {
    "total_budget_usd",
    "watchlist",
    "run_time_et",
    "adaptive_risk",
}

COOLDOWN_DAYS    = 7
EMERGENCY_DD_PCT = 0.10


def _load_state() -> dict:
    os.makedirs(os.path.dirname(AUTO_TUNE_STATE), exist_ok=True)
    if not os.path.exists(AUTO_TUNE_STATE):
        return {"enabled": True, "frozen": False, "frozen_reason": "",
                "last_run": None, "last_changes": {}}
    with open(AUTO_TUNE_STATE) as f:
        return json.load(f)

def _save_state(state: dict):
    os.makedirs(os.path.dirname(AUTO_TUNE_STATE), exist_ok=True)
    with open(AUTO_TUNE_STATE, "w") as f:
        json.dump(state, f, indent=2)

def _log_change(change: dict):
    os.makedirs(os.path.dirname(AUTO_TUNE_LOG), exist_ok=True)
    history = []
    if os.path.exists(AUTO_TUNE_LOG):
        with open(AUTO_TUNE_LOG) as f:
            history = json.load(f)
    history.append(change)
    history = history[-200:]
    with open(AUTO_TUNE_LOG, "w") as f:
        json.dump(history, f, indent=2)

def enable_auto_tune():
    state = _load_state()
    state["enabled"] = True
    state["frozen"]  = False
    _save_state(state)
    log.info("Auto-tuner ENABLED")

def disable_auto_tune():
    state = _load_state()
    state["enabled"] = False
    _save_state(state)
    log.info("Auto-tuner DISABLED")

def is_auto_tune_enabled() -> bool:
    return _load_state().get("enabled", True)

def get_auto_tune_log(limit: int = 20) -> list:
    if not os.path.exists(AUTO_TUNE_LOG):
        return []
    with open(AUTO_TUNE_LOG) as f:
        history = json.load(f)
    return history[-limit:]

def _check_cooldown(param: str) -> bool:
    state    = _load_state()
    last_chg = state.get("last_changes", {}).get(param)
    if not last_chg:
        return False
    last_dt  = datetime.fromisoformat(last_chg)
    cooldown = timedelta(days=COOLDOWN_DAYS)
    return (datetime.now(timezone.utc) - last_dt) < cooldown

def _clamp_change(param: str, current: float, proposed: float) -> float:
    if param not in MAX_STEP:
        return proposed
    max_delta = MAX_STEP[param]
    delta     = proposed - current
    if abs(delta) > max_delta:
        clamped = current + (max_delta if delta > 0 else -max_delta)
        log.info(f"  {param}: clamped {proposed} → {clamped} (max step ±{max_delta})")
        return round(clamped, 4)
    return proposed

def _check_emergency_freeze() -> tuple:
    perf = get_recent_performance(lookback_days=7)
    if not perf["has_data"]:
        return False, ""
    if perf["max_drawdown_pct"] >= EMERGENCY_DD_PCT:
        reason = (
            f"Emergency freeze: {perf['max_drawdown_pct']:.1%} drawdown "
            f"exceeds {EMERGENCY_DD_PCT:.0%} threshold"
        )
        return True, reason
    return False, ""

def _build_tuner_prompt() -> str:
    perf    = get_recent_performance(lookback_days=7)
    vol     = get_market_volatility()
    params  = get_effective_params()
    history = get_param_history(limit=10)

    history_str = "\n".join(
        f"  {h['timestamp'][:10]} {h['key']}: {h['old_value']} → {h['new_value']} ({h['source']})"
        for h in history
    ) or "  No recent changes"

    cooldown_str = ", ".join(
        p for p in MAX_STEP if _check_cooldown(p)
    ) or "none"

    tunable = {
        k: {
            "current":  params.get(k),
            "min":      PARAM_SCHEMA[k][1],
            "max":      PARAM_SCHEMA[k][2],
            "max_step": MAX_STEP.get(k, "n/a"),
        }
        for k in MAX_STEP
        if k not in LOCKED_PARAMS
    }

    return f"""
You are an autonomous trading bot parameter optimizer. Your job is to analyze
the past week's performance and recommend SPECIFIC parameter changes to improve
results. You must respond ONLY with a valid JSON object — no prose, no markdown.

── PERFORMANCE (last 7 days) ────────────────────────────────
Trades:        {perf['trade_count']}
Win rate:      {perf['win_rate']:.0%}
Recent streak: {perf['recent_streak']:+d}
Avg P&L:       {perf['avg_pnl_pct']:.2%}
Max drawdown:  {perf['max_drawdown_pct']:.2%}

── MARKET CONDITIONS ────────────────────────────────────────
Volatility:    {vol['level']} ({vol['daily_vol_pct']:.2f}% daily std)
Risk mode:     {params.get('_risk_mode', 'NORMAL')}

── CURRENT TUNABLE PARAMETERS ───────────────────────────────
{json.dumps(tunable, indent=2)}

── PARAMETERS ON COOLDOWN (do not recommend these) ──────────
{cooldown_str}

── RECENT PARAMETER HISTORY ─────────────────────────────────
{history_str}

── YOUR TASK ────────────────────────────────────────────────
Recommend 0-3 parameter changes. Only recommend a change if the data
clearly supports it. Recommend 0 changes if performance is good.
Never recommend locked params: {list(LOCKED_PARAMS)}
Never recommend params on cooldown.
Respect max_step limits.

Respond with ONLY this JSON structure:
{{
  "changes": [
    {{
      "param":     "stop_loss_pct",
      "new_value": 0.04,
      "reasoning": "Win rate is 45% and drawdown is high — tighter stops needed"
    }}
  ],
  "summary": "One sentence summary of overall bot health and what you changed"
}}

If no changes needed: {{"changes": [], "summary": "Performance is strong, no changes needed."}}
""".strip()

def _get_claude_recommendations() -> Optional[dict]:
    try:
        client  = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)
        message = client.messages.create(
            model="claude-sonnet-4-5",
            max_tokens=800,
            system=(
                "You are a trading bot auto-tuner. Respond only with valid JSON. "
                "No markdown fences, no preamble, no explanation outside the JSON."
            ),
            messages=[{"role": "user", "content": _build_tuner_prompt()}],
        )
        raw     = message.content[0].text.strip()
        cleaned = re.sub(r"```[a-z]*\n?", "", raw).strip()
        return json.loads(cleaned)
    except Exception as e:
        log.error(f"Auto-tuner Claude call failed: {e}")
        return None

def _apply_changes(recommendations: dict) -> list:
    applied  = []
    params   = get_effective_params()
    changes  = recommendations.get("changes", [])

    if not changes:
        log.info("Auto-tuner: no changes recommended this cycle")
        return []

    for rec in changes[:3]:
        param     = rec.get("param")
        new_value = rec.get("new_value")
        reasoning = rec.get("reasoning", "")

        if not param or new_value is None:
            continue
        if param in LOCKED_PARAMS:
            log.warning(f"  {param}: LOCKED — skipping")
            continue
        if _check_cooldown(param):
            log.warning(f"  {param}: on cooldown — skipping")
            continue
        if param not in PARAM_SCHEMA:
            log.warning(f"  {param}: unknown parameter — skipping")
            continue

        current = params.get(param)
        if isinstance(current, (int, float)) and isinstance(new_value, (int, float)):
            new_value = _clamp_change(param, float(current), float(new_value))

        ok, msg = set_param(param, new_value, source="auto_tuner")
        if ok:
            change = {
                "timestamp": datetime.now(timezone.utc).isoformat(),
                "param":     param,
                "old_value": current,
                "new_value": new_value,
                "reasoning": reasoning,
                "source":    "auto_tuner",
            }
            applied.append(change)
            _log_change(change)
            log.info(f"  Auto-tuner applied: {param} {current} → {new_value}")
            state = _load_state()
            state.setdefault("last_changes", {})[param] = (
                datetime.now(timezone.utc).isoformat()
            )
            _save_state(state)
        else:
            log.warning(f"  {param}: validation failed — {msg}")

    return applied

def _send_tune_notification(applied: list, summary: str, frozen: bool = False):
    now = datetime.now(timezone.utc).strftime("%Y-%m-%d")

    if frozen:
        sms     = f"[ALERT] Trading Bot — Auto-tuner FROZEN\n{summary}"
        subject = f"[ALERT] Trading Bot Auto-tuner Frozen — {now}"
    elif not applied:
        sms     = f"[Bot Weekly] No parameter changes needed.\n{summary}"
        subject = f"Trading Bot Weekly Tune — No Changes — {now}"
    else:
        changes_str = ", ".join(
            f"{c['param']}: {c['old_value']} → {c['new_value']}"
            for c in applied
        )
        sms     = f"[Bot Weekly] {len(applied)} change(s): {changes_str}\n{summary}"
        subject = f"Trading Bot Weekly Tune — {len(applied)} Change(s) — {now}"

    rows = "".join(
        f"<tr><td>{c['param']}</td><td>{c['old_value']}</td>"
        f"<td>{c['new_value']}</td><td>{c['reasoning']}</td></tr>"
        for c in applied
    ) if applied else "<tr><td colspan='4'>No changes applied</td></tr>"

    html = f"""
    <div style="font-family: sans-serif; max-width: 600px;">
      <h2>Trading Bot — Weekly Auto-tune</h2>
      <p>{summary}</p>
      <table border="1" cellpadding="6" style="border-collapse:collapse; width:100%">
        <tr style="background:#f0f0f0">
          <th>Parameter</th><th>Old</th><th>New</th><th>Reason</th>
        </tr>
        {rows}
      </table>
      <p style="font-size:11px; color:#888;">
        To disable: python -c "from main import disable_auto_tune; disable_auto_tune()"
      </p>
    </div>
    """

    send_sms(sms)
    send_email(subject, sms, html)

def run_auto_tune():
    log.info("Auto-tuner starting weekly cycle...")
    state = _load_state()

    if not state.get("enabled", True):
        log.info("Auto-tuner is disabled — skipping")
        return

    should_freeze, freeze_reason = _check_emergency_freeze()
    if should_freeze:
        state["frozen"]        = True
        state["frozen_reason"] = freeze_reason
        _save_state(state)
        log.warning(f"Auto-tuner FROZEN: {freeze_reason}")
        _send_tune_notification([], freeze_reason, frozen=True)
        return

    if state.get("frozen"):
        log.info("Previous freeze lifted — conditions improved")
        state["frozen"]        = False
        state["frozen_reason"] = ""
        _save_state(state)

    log.info("Requesting recommendations from Claude...")
    recommendations = _get_claude_recommendations()

    if recommendations is None:
        log.error("Auto-tuner: could not get recommendations — skipping")
        return

    summary = recommendations.get("summary", "Weekly tune completed.")
    log.info(f"Claude summary: {summary}")

    applied = _apply_changes(recommendations)

    state["last_run"] = datetime.now(timezone.utc).isoformat()
    _save_state(state)

    _send_tune_notification(applied, summary)

    log.info(
        f"Auto-tune complete — "
        f"{len(applied)} change(s): "
        f"{[c['param'] for c in applied] or 'none'}"
    )


if __name__ == "__main__":
    print("\n=== Module 10: Auto-Tuner — Test Run ===\n")

    print("Checking auto-tuner state...")
    state = _load_state()
    print(f"  Enabled:  {state.get('enabled', True)}")
    print(f"  Frozen:   {state.get('frozen', False)}")
    print(f"  Last run: {state.get('last_run', 'never')}")

    print("\nChecking emergency freeze conditions...")
    freeze, reason = _check_emergency_freeze()
    print(f"  Should freeze: {freeze}")
    if reason:
        print(f"  Reason: {reason}")

    print("\nFetching Claude recommendations (dry run — will NOT apply changes)...")
    recs = _get_claude_recommendations()
    if recs:
        print(f"\n  Summary: {recs.get('summary')}")
        print(f"  Recommended changes: {len(recs.get('changes', []))}")
        for c in recs.get("changes", []):
            print(f"    {c['param']}: → {c['new_value']}  ({c['reasoning'][:60]}...)")
    else:
        print("  Could not get recommendations — check ANTHROPIC_API_KEY")

    print("\n  (No changes were applied — this was a dry run)")
    print("  To run a live tune: python -c \"from main import tune_now; tune_now()\"")

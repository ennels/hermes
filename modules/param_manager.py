"""
Module 8: Parameter Manager
─────────────────────────────
Allows live adjustment of all bot parameters without restarting.
Parameters are stored in logs/params.json and take effect on the
next trading cycle.

Supports:
  - Manual override of any individual parameter
  - Full reset to config/settings.py defaults
  - Parameter history (audit trail of every change)
  - Validation — rejects values outside safe bounds
  - Adaptive risk integration — shows auto vs manual values side-by-side
"""

import os
import sys
import json
import logging
from datetime import datetime, timezone
from typing import Any, Optional

sys.path.append(os.path.join(os.path.dirname(__file__), ".."))
from config import settings

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger(__name__)

PARAMS_PATH   = os.path.join(os.path.dirname(__file__), "..", "logs", "params.json")
HISTORY_PATH  = os.path.join(os.path.dirname(__file__), "..", "logs", "params_history.json")

# ── Parameter definitions: name → (type, min, max, description) ───────────────
PARAM_SCHEMA = {
    "total_budget_usd":  (float, 10.0,   100_000.0, "Total capital the bot can deploy ($)"),
    "max_position_usd":  (float, 5.0,    10_000.0,  "Max per-ticker position size ($)"),
    "max_positions":     (int,   1,       20,        "Max simultaneous open positions"),
    "stop_loss_pct":     (float, 0.01,   0.20,      "Stop-loss threshold (0.05 = 5%)"),
    "take_profit_pct":   (float, 0.02,   0.50,      "Take-profit threshold (0.15 = 15%)"),
    "min_confidence":    (float, 0.50,   0.95,      "Minimum AI confidence to trade (0–1)"),
    "run_time_et":       (str,   None,   None,       "Daily run time in ET, format HH:MM"),
    "watchlist":         (list,  None,   None,       "List of ticker symbols to watch"),
    "adaptive_risk":     (bool,  None,   None,       "Enable/disable adaptive risk module"),
    "news_lookback_hours": (int, 1,      168,        "Hours of news history to pull"),
    "gemini_weight_news":  (float, 0.0,  1.0,       "Gemini weight for news-driven signals"),
    "claude_weight_tech":  (float, 0.0,  1.0,       "Claude weight for technical signals"),
}


# ══════════════════════════════════════════════════════════════════════════════
# SECTION 1: Load / save
# ══════════════════════════════════════════════════════════════════════════════

def _defaults() -> dict:
    """Return the baseline parameters from config/settings.py."""
    return {
        "total_budget_usd":    settings.TOTAL_BUDGET_USD,
        "max_position_usd":    settings.MAX_POSITION_USD,
        "max_positions":       settings.MAX_POSITIONS,
        "stop_loss_pct":       settings.STOP_LOSS_PCT,
        "take_profit_pct":     settings.TAKE_PROFIT_PCT,
        "min_confidence":      settings.MIN_CONFIDENCE,
        "run_time_et":         settings.RUN_TIME_ET,
        "watchlist":           list(settings.WATCHLIST),
        "adaptive_risk":       True,
        "news_lookback_hours": settings.NEWS_LOOKBACK_HOURS,
        "gemini_weight_news":  0.70,
        "claude_weight_tech":  0.70,
        "_source":             "defaults",
        "_updated_at":         datetime.now(timezone.utc).isoformat(),
    }

def load_params() -> dict:
    """
    Load active parameters. Returns saved params if they exist,
    otherwise returns defaults from settings.py.
    """
    os.makedirs(os.path.dirname(PARAMS_PATH), exist_ok=True)
    if not os.path.exists(PARAMS_PATH):
        defaults = _defaults()
        _save_params(defaults)
        return defaults
    with open(PARAMS_PATH) as f:
        return json.load(f)

def _save_params(params: dict):
    os.makedirs(os.path.dirname(PARAMS_PATH), exist_ok=True)
    with open(PARAMS_PATH, "w") as f:
        json.dump(params, f, indent=2)

def _append_history(change: dict):
    """Append a parameter change to the audit trail."""
    os.makedirs(os.path.dirname(HISTORY_PATH), exist_ok=True)
    history = []
    if os.path.exists(HISTORY_PATH):
        with open(HISTORY_PATH) as f:
            history = json.load(f)
    history.append(change)
    # Keep last 500 changes
    history = history[-500:]
    with open(HISTORY_PATH, "w") as f:
        json.dump(history, f, indent=2)


# ══════════════════════════════════════════════════════════════════════════════
# SECTION 2: Validation
# ══════════════════════════════════════════════════════════════════════════════

def validate_param(key: str, value: Any) -> tuple[bool, str]:
    """
    Validate a parameter value against the schema.
    Returns (is_valid, error_message).
    """
    if key not in PARAM_SCHEMA:
        return False, f"Unknown parameter '{key}'. Valid: {list(PARAM_SCHEMA.keys())}"

    dtype, min_val, max_val, desc = PARAM_SCHEMA[key]

    if dtype == bool:
        if not isinstance(value, bool):
            return False, f"'{key}' must be true or false"
        return True, ""

    if dtype == list:
        if not isinstance(value, list):
            return False, f"'{key}' must be a list of strings"
        if not all(isinstance(t, str) for t in value):
            return False, f"'{key}' must be a list of ticker strings"
        if len(value) == 0:
            return False, f"'{key}' cannot be empty"
        return True, ""

    if dtype == str:
        if key == "run_time_et":
            parts = str(value).split(":")
            if len(parts) != 2:
                return False, "run_time_et must be HH:MM format (e.g. '09:45')"
            h, m = parts
            if not (h.isdigit() and m.isdigit() and
                    0 <= int(h) <= 23 and 0 <= int(m) <= 59):
                return False, "run_time_et must be a valid time (e.g. '09:45')"
        return True, ""

    # Numeric
    try:
        value = dtype(value)
    except (ValueError, TypeError):
        return False, f"'{key}' must be a {dtype.__name__}"

    if min_val is not None and value < min_val:
        return False, f"'{key}' minimum is {min_val}"
    if max_val is not None and value > max_val:
        return False, f"'{key}' maximum is {max_val}"

    return True, ""


# ══════════════════════════════════════════════════════════════════════════════
# SECTION 3: Set / get / reset
# ══════════════════════════════════════════════════════════════════════════════

def set_param(key: str, value: Any, source: str = "manual") -> tuple[bool, str]:
    """
    Set a single parameter. Validates, saves, and logs the change.
    Returns (success, message).
    """
    valid, err = validate_param(key, value)
    if not valid:
        return False, err

    params   = load_params()
    old_val  = params.get(key)

    # Type coerce
    dtype = PARAM_SCHEMA[key][0]
    if dtype in (int, float):
        value = dtype(value)

    params[key]          = value
    params["_source"]    = source
    params["_updated_at"] = datetime.now(timezone.utc).isoformat()
    _save_params(params)

    change = {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "key":       key,
        "old_value": old_val,
        "new_value": value,
        "source":    source,
    }
    _append_history(change)

    msg = f"Set {key}: {old_val} → {value}"
    log.info(msg)
    return True, msg


def set_params(updates: dict, source: str = "manual") -> list:
    """
    Set multiple parameters at once.
    Returns list of (success, message) tuples.
    """
    results = []
    for key, value in updates.items():
        results.append(set_param(key, value, source=source))
    return results


def get_param(key: str) -> Any:
    """Get the current value of a single parameter."""
    return load_params().get(key)


def get_all_params() -> dict:
    """Get all current parameters with their defaults for comparison."""
    current  = load_params()
    defaults = _defaults()
    result   = {}

    for key in PARAM_SCHEMA:
        schema_type, min_v, max_v, desc = PARAM_SCHEMA[key]
        curr_val    = current.get(key, defaults.get(key))
        default_val = defaults.get(key)
        result[key] = {
            "value":       curr_val,
            "default":     default_val,
            "modified":    curr_val != default_val,
            "description": desc,
            "min":         min_v,
            "max":         max_v,
            "type":        schema_type.__name__,
        }
    return result


def reset_to_defaults(source: str = "manual_reset") -> dict:
    """Reset all parameters to config/settings.py values."""
    defaults = _defaults()
    defaults["_source"]     = source
    defaults["_updated_at"] = datetime.now(timezone.utc).isoformat()
    _save_params(defaults)
    _append_history({
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "key":       "__all__",
        "old_value": "various",
        "new_value": "defaults",
        "source":    source,
    })
    log.info("All parameters reset to defaults")
    return defaults


def get_param_history(limit: int = 20) -> list:
    """Return the last N parameter changes."""
    if not os.path.exists(HISTORY_PATH):
        return []
    with open(HISTORY_PATH) as f:
        history = json.load(f)
    return history[-limit:]


# ══════════════════════════════════════════════════════════════════════════════
# SECTION 4: Adaptive risk integration
# ══════════════════════════════════════════════════════════════════════════════

def get_effective_params() -> dict:
    """
    Return the parameters the bot will ACTUALLY use this cycle.

    Logic:
      - If adaptive_risk is True: use adaptive_risk.py computed values,
        but allow manual overrides to take precedence on a per-key basis.
      - If adaptive_risk is False: use manual/saved params only.

    This means you can have adaptive risk on globally but still pin
    specific values (e.g. always use stop_loss_pct=0.05 regardless).
    """
    saved = load_params()

    if not saved.get("adaptive_risk", True):
        log.info("Adaptive risk disabled — using saved parameters")
        return saved

    try:
        from modules.adaptive_risk import compute_adaptive_params
        adaptive = compute_adaptive_params()
    except Exception as e:
        log.warning(f"Adaptive risk computation failed ({e}) — using saved params")
        return saved

    defaults = _defaults()

    # Adaptive values are the base
    effective = {**saved}
    adaptive_keys = {
        "stop_loss_pct":    adaptive["stop_loss_pct"],
        "take_profit_pct":  adaptive["take_profit_pct"],
        "max_position_usd": adaptive["max_position_usd"],
        "max_positions":    adaptive["max_positions"],
        "min_confidence":   adaptive["min_confidence"],
    }

    for key, adaptive_val in adaptive_keys.items():
        saved_val   = saved.get(key)
        default_val = defaults.get(key)
        # If user has manually changed this key, respect their override
        if saved_val != default_val:
            log.info(f"  {key}: manual override ({saved_val}) kept over adaptive ({adaptive_val})")
        else:
            effective[key] = adaptive_val

    effective["_risk_mode"]   = adaptive.get("risk_mode", "NORMAL")
    effective["_adaptive"]    = True
    effective["_performance"] = adaptive.get("performance", {})
    effective["_volatility"]  = adaptive.get("volatility", {})

    return effective


# ══════════════════════════════════════════════════════════════════════════════
# Quick test
# ══════════════════════════════════════════════════════════════════════════════

if __name__ == "__main__":
    print("\n=== Module 8: Parameter Manager — Test Run ===\n")

    print("Current parameters:")
    all_params = get_all_params()
    for key, info in all_params.items():
        modified = " *" if info["modified"] else ""
        print(f"  {key:<25} {str(info['value']):<20} (default: {info['default']}){modified}")

    print("\nTesting set_param...")
    ok, msg = set_param("stop_loss_pct", 0.04)
    print(f"  {msg} — {'OK' if ok else 'FAILED'}")

    ok, msg = set_param("stop_loss_pct", 0.99)  # should fail
    print(f"  Invalid test: {msg} — {'OK' if not ok else 'SHOULD HAVE FAILED'}")

    print("\nRecent parameter history:")
    for h in get_param_history(5):
        print(f"  {h['timestamp'][:19]}  {h['key']}: {h['old_value']} → {h['new_value']}")

    print("\nResetting to defaults...")
    reset_to_defaults()
    print("  Done.")

    print("\nEffective parameters (with adaptive risk):")
    effective = get_effective_params()
    print(f"  Risk mode:      {effective.get('_risk_mode', 'N/A')}")
    print(f"  Stop-loss:      {effective['stop_loss_pct']:.1%}")
    print(f"  Take-profit:    {effective['take_profit_pct']:.1%}")
    print(f"  Max position:   ${effective['max_position_usd']:.0f}")
    print(f"  Min confidence: {effective['min_confidence']:.2f}")

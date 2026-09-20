"""
Module 3: Arbitration Layer
────────────────────────────
Takes the raw Claude + Gemini signals and produces a single
arbitrated decision per ticker.

Rules (in order of precedence):
  1. Both models agree on action → act, weight confidence by signal type
  2. Same direction, different confidence → act, use weighted average
  3. Models disagree (buy vs sell) → abstain (hold)
  4. One model failed/missing → use the other at reduced confidence
  5. Both failed → skip ticker this cycle

Model weights by signal type:
  - News/sentiment-driven signals → Gemini weighted 70%, Claude 30%
  - Technical/price-driven signals → Claude 70%, Gemini 30%
  The "dominant signal type" is inferred from which signals each model flagged.
"""

import os
import sys
import logging
from typing import Optional

sys.path.append(os.path.join(os.path.dirname(__file__), ".."))
from config.settings import MIN_CONFIDENCE

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger(__name__)

# Signals that indicate a news/sentiment-driven decision
NEWS_SIGNALS = {
    "positive_sentiment", "negative_sentiment", "high_buzz",
    "sector_momentum", "analyst_upgrade", "analyst_downgrade",
    "earnings_risk", "macro_event",
}

# Signals that indicate a technical decision
TECHNICAL_SIGNALS = {
    "rsi_oversold", "rsi_overbought", "macd_bullish", "macd_bearish",
    "above_ema20", "below_ema20", "price_breakout", "price_breakdown",
    "volume_spike",
}

# How much to reduce confidence when falling back to a single model
SINGLE_MODEL_CONFIDENCE_PENALTY = 0.15

# Minimum gap between buy and sell confidence to avoid wishy-washy trades
CONFLICT_ABSTAIN_THRESHOLD = 0.10


def _infer_signal_type(signals_used: list) -> str:
    """
    Determine whether a signal list is primarily news-driven or technical.
    Returns "news", "technical", or "mixed".
    """
    news_count  = sum(1 for s in signals_used if s in NEWS_SIGNALS)
    tech_count  = sum(1 for s in signals_used if s in TECHNICAL_SIGNALS)
    if news_count > tech_count:
        return "news"
    elif tech_count > news_count:
        return "technical"
    return "mixed"


def _weighted_confidence(
    claude_signal: dict,
    gemini_signal: dict,
) -> float:
    """
    Compute a weighted average confidence based on which model is stronger
    for the detected signal type.

    Weights:
      News-driven:      Gemini 0.70, Claude 0.30
      Technical-driven: Claude 0.70, Gemini 0.30
      Mixed:            Claude 0.50, Gemini 0.50
    """
    # Determine dominant signal type from both signal lists
    all_signals = (
        claude_signal.get("signals_used", []) +
        gemini_signal.get("signals_used", [])
    )
    signal_type = _infer_signal_type(all_signals)

    weights = {
        "news":      {"claude": 0.30, "gemini": 0.70},
        "technical": {"claude": 0.70, "gemini": 0.30},
        "mixed":     {"claude": 0.50, "gemini": 0.50},
    }[signal_type]

    weighted = (
        claude_signal["confidence"] * weights["claude"] +
        gemini_signal["confidence"] * weights["gemini"]
    )
    return round(weighted, 3), signal_type


def arbitrate(signals: dict) -> Optional[dict]:
    """
    Core arbitration logic for a single ticker's signal pair.

    Input:  { "ticker": "NVDA", "claude": {...}|None, "gemini": {...}|None }
    Output: {
        "ticker":        "NVDA",
        "action":        "buy"|"sell"|"hold",
        "confidence":    0.0–1.0,
        "reasoning":     "...",
        "signal_type":   "news"|"technical"|"mixed",
        "arbitration":   "agreement"|"weighted"|"fallback_claude"|
                         "fallback_gemini"|"conflict_abstain"|"no_data",
        "claude_signal": {...}|None,
        "gemini_signal": {...}|None,
    }
    """
    ticker  = signals["ticker"]
    claude  = signals.get("claude")
    gemini  = signals.get("gemini")

    # ── Case 4/5: one or both models missing ─────────────────────────────────
    if claude is None and gemini is None:
        log.warning(f"  {ticker}: both models failed — skipping")
        return {
            "ticker": ticker, "action": "hold", "confidence": 0.0,
            "reasoning": "Both models failed to produce a signal.",
            "signal_type": "unknown", "arbitration": "no_data",
            "claude_signal": None, "gemini_signal": None,
        }

    if claude is None:
        log.warning(f"  {ticker}: Claude failed — falling back to Gemini only")
        confidence = max(0.0, gemini["confidence"] - SINGLE_MODEL_CONFIDENCE_PENALTY)
        return {
            "ticker": ticker,
            "action": gemini["action"],
            "confidence": round(confidence, 3),
            "reasoning": f"[Gemini only — Claude unavailable] {gemini['reasoning']}",
            "signal_type": _infer_signal_type(gemini.get("signals_used", [])),
            "arbitration": "fallback_gemini",
            "claude_signal": None,
            "gemini_signal": gemini,
        }

    if gemini is None:
        log.warning(f"  {ticker}: Gemini failed — falling back to Claude only")
        confidence = max(0.0, claude["confidence"] - SINGLE_MODEL_CONFIDENCE_PENALTY)
        return {
            "ticker": ticker,
            "action": claude["action"],
            "confidence": round(confidence, 3),
            "reasoning": f"[Claude only — Gemini unavailable] {claude['reasoning']}",
            "signal_type": _infer_signal_type(claude.get("signals_used", [])),
            "arbitration": "fallback_claude",
            "claude_signal": claude,
            "gemini_signal": None,
        }

    # ── Case 1/2: both models present ────────────────────────────────────────
    claude_action = claude["action"]
    gemini_action = gemini["action"]

    # ── Case 3: direct conflict (buy vs sell) ─────────────────────────────────
    opposites = {("buy", "sell"), ("sell", "buy")}
    if (claude_action, gemini_action) in opposites:
        conf_gap = abs(claude["confidence"] - gemini["confidence"])
        if conf_gap < CONFLICT_ABSTAIN_THRESHOLD:
            # Neither model is confident enough to override — abstain
            log.info(
                f"  {ticker}: conflict (Claude={claude_action}, "
                f"Gemini={gemini_action}), gap={conf_gap:.2f} — abstaining"
            )
            return {
                "ticker": ticker, "action": "hold",
                "confidence": round(min(claude["confidence"], gemini["confidence"]), 3),
                "reasoning": (
                    f"Models disagree: Claude says {claude_action} "
                    f"({claude['confidence']:.2f}), Gemini says {gemini_action} "
                    f"({gemini['confidence']:.2f}). Abstaining."
                ),
                "signal_type": "conflict",
                "arbitration": "conflict_abstain",
                "claude_signal": claude,
                "gemini_signal": gemini,
            }
        else:
            # One model is significantly more confident — use the stronger one
            winner = claude if claude["confidence"] > gemini["confidence"] else gemini
            winner_name = "Claude" if winner is claude else "Gemini"
            confidence  = max(0.0, winner["confidence"] - SINGLE_MODEL_CONFIDENCE_PENALTY)
            log.info(
                f"  {ticker}: conflict resolved by confidence gap "
                f"({winner_name} wins with {winner['confidence']:.2f})"
            )
            return {
                "ticker": ticker,
                "action": winner["action"],
                "confidence": round(confidence, 3),
                "reasoning": (
                    f"[Conflict resolved — {winner_name} more confident] "
                    f"{winner['reasoning']}"
                ),
                "signal_type": _infer_signal_type(winner.get("signals_used", [])),
                "arbitration": f"conflict_resolved_{winner_name.lower()}",
                "claude_signal": claude,
                "gemini_signal": gemini,
            }

    # ── Case 1/2: agreement or same-direction ────────────────────────────────
    # "hold" from either model counts as agreement for the direction
    # e.g. Claude=buy, Gemini=hold → still proceed as buy at lower confidence
    if claude_action == gemini_action:
        confidence, signal_type = _weighted_confidence(claude, gemini)
        arbitration_type = "agreement"
    else:
        # One says buy/sell, other says hold — proceed with active signal
        # but reduce confidence slightly
        active = claude if claude_action != "hold" else gemini
        passive_conf = gemini["confidence"] if active is claude else claude["confidence"]
        confidence, signal_type = _weighted_confidence(claude, gemini)
        # Blend passive "hold" signal reduces conviction slightly
        confidence = round(confidence * 0.85, 3)
        arbitration_type = "weighted"

    # Determine final action (prefer non-hold if there's agreement on direction)
    if claude_action == gemini_action:
        final_action = claude_action
    else:
        final_action = claude_action if claude_action != "hold" else gemini_action

    # Combined reasoning
    reasoning = (
        f"Claude ({claude_action}, {claude['confidence']:.2f}): {claude['reasoning']} | "
        f"Gemini ({gemini_action}, {gemini['confidence']:.2f}): {gemini['reasoning']}"
    )

    result = {
        "ticker":        ticker,
        "action":        final_action,
        "confidence":    confidence,
        "reasoning":     reasoning,
        "signal_type":   signal_type,
        "arbitration":   arbitration_type,
        "claude_signal": claude,
        "gemini_signal": gemini,
    }

    log.info(
        f"  {ticker}: {final_action.upper()} "
        f"(confidence={confidence:.2f}, "
        f"arbitration={arbitration_type}, "
        f"signal_type={signal_type})"
    )
    return result


def arbitrate_all(all_signals: dict) -> dict:
    """
    Run arbitration on every ticker's signal pair.
    Filters out decisions below MIN_CONFIDENCE threshold.

    Returns dict of actionable decisions keyed by ticker.
    Only includes tickers where action != "hold" AND confidence >= MIN_CONFIDENCE,
    plus all holds for reference.
    """
    decisions    = {}
    actionable   = 0

    # Load sector rotation if available
    try:
        from modules.sector_rotation import get_ticker_adjustments
        ROTATION_ENABLED = True
    except ImportError:
        ROTATION_ENABLED = False

    log.info(f"Running arbitration on {len(all_signals)} tickers...")

    for ticker, signals in all_signals.items():
        decision = arbitrate(signals)

        # Apply sector rotation confidence adjustment
        if ROTATION_ENABLED and decision.get("action") != "hold":
            adj        = get_ticker_adjustments(ticker)
            conf_delta = adj.get("conf_delta", 0.0)
            if conf_delta != 0.0:
                old_conf = decision["confidence"]
                new_conf = round(max(0.0, min(1.0, old_conf + conf_delta)), 3)
                decision = {**decision, "confidence": new_conf, "rotation_adj": conf_delta}
                log.info(f"  {ticker}: rotation={adj.get('signal')} conf {old_conf:.2f}->{new_conf:.2f}")

        decisions[ticker] = decision

        if decision["action"] != "hold" and decision["confidence"] >= MIN_CONFIDENCE:
            actionable += 1
            log.info(
                f"  ACTIONABLE: {ticker} → {decision['action'].upper()} "
                f"@ confidence {decision['confidence']:.2f}"
            )

    log.info(
        f"Arbitration complete — "
        f"{actionable} actionable signals out of {len(decisions)} tickers"
    )
    return decisions


# ══════════════════════════════════════════════════════════════════════════════
# Quick test
# ══════════════════════════════════════════════════════════════════════════════

if __name__ == "__main__":
    print("\n=== Module 3: Arbitration — Test Run ===\n")

    # Simulate a variety of signal combinations
    test_cases = [
        {
            "label": "Agreement (both buy)",
            "signals": {
                "ticker": "NVDA",
                "claude": {"ticker": "NVDA", "action": "buy",  "confidence": 0.78,
                           "reasoning": "Strong MACD crossover, above EMA-20.",
                           "signals_used": ["macd_bullish", "above_ema20"]},
                "gemini": {"ticker": "NVDA", "action": "buy",  "confidence": 0.82,
                           "reasoning": "Positive news surge, analyst upgrades.",
                           "signals_used": ["positive_sentiment", "analyst_upgrade"]},
            }
        },
        {
            "label": "Conflict, close confidence (abstain)",
            "signals": {
                "ticker": "AMD",
                "claude": {"ticker": "AMD", "action": "sell", "confidence": 0.65,
                           "reasoning": "RSI overbought, MACD turning bearish.",
                           "signals_used": ["rsi_overbought", "macd_bearish"]},
                "gemini": {"ticker": "AMD", "action": "buy",  "confidence": 0.60,
                           "reasoning": "Positive sector news for semiconductors.",
                           "signals_used": ["positive_sentiment", "sector_momentum"]},
            }
        },
        {
            "label": "Gemini missing (fallback to Claude)",
            "signals": {
                "ticker": "MSFT",
                "claude": {"ticker": "MSFT", "action": "hold", "confidence": 0.55,
                           "reasoning": "No clear technical signal.",
                           "signals_used": []},
                "gemini": None,
            }
        },
        {
            "label": "One buy, one hold (weighted)",
            "signals": {
                "ticker": "GOOGL",
                "claude": {"ticker": "GOOGL", "action": "buy",  "confidence": 0.74,
                           "reasoning": "RSI recovering from oversold, MACD bullish.",
                           "signals_used": ["rsi_oversold", "macd_bullish"]},
                "gemini": {"ticker": "GOOGL", "action": "hold", "confidence": 0.50,
                           "reasoning": "Neutral news sentiment, no catalyst.",
                           "signals_used": []},
            }
        },
    ]

    for case in test_cases:
        print(f"\n── {case['label']} ─────────────────────────────────")
        decision = arbitrate(case["signals"])
        print(f"  Ticker:      {decision['ticker']}")
        print(f"  Action:      {decision['action'].upper()}")
        print(f"  Confidence:  {decision['confidence']:.2f}")
        print(f"  Arbitration: {decision['arbitration']}")
        print(f"  Signal type: {decision['signal_type']}")
        print(f"  Reasoning:   {decision['reasoning'][:120]}...")

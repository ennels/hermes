"""
Module 2: Signal Engine
───────────────────────
Sends pre-processed ticker data to both Gemini and Claude in parallel.
Each model returns a structured JSON signal independently.
Results are passed to the arbitration layer (Module 3).

Gemini 2.5 Flash  → news sentiment, sector momentum, live search grounding
Claude Sonnet   → technical reasoning, risk nuance, fundamental context

Each model returns:
  {
    "ticker":     "NVDA",
    "action":     "buy" | "sell" | "hold",
    "confidence": 0.0–1.0,
    "reasoning":  "...",
    "signals_used": ["rsi_oversold", "positive_sentiment", ...]
  }
"""

import os
import sys
import json
import logging
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Optional

import anthropic
from google import genai
from google.genai import types

sys.path.append(os.path.join(os.path.dirname(__file__), ".."))
try:
    from modules.signal_cache import (
        get_cached_signal, cache_signals,
        should_skip_ai_calls, get_skip_signal,
    )
    CACHE_ENABLED = True
except ImportError:
    CACHE_ENABLED = False

from config.settings import (
    ANTHROPIC_API_KEY,
    GEMINI_API_KEY,
    MIN_CONFIDENCE,
)

# ── Logging ───────────────────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger(__name__)

# ── Clients ───────────────────────────────────────────────────────────────────
_anthropic_client = None
_gemini_model     = None

def _get_anthropic():
    global _anthropic_client
    if _anthropic_client is None:
        _anthropic_client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)
    return _anthropic_client

def _get_gemini():
    global _gemini_model
    if _gemini_model is None:
        _gemini_model = genai.Client(api_key=GEMINI_API_KEY)
    return _gemini_model


# ══════════════════════════════════════════════════════════════════════════════
# SECTION 1: Shared prompt builder
# ══════════════════════════════════════════════════════════════════════════════

def _build_prompt(ticker_data: dict, model_role: str) -> str:
    """
    Builds the prompt sent to each model.
    model_role: "news_sentiment" (Gemini) or "technical_reasoning" (Claude)
    """
    t  = ticker_data
    p  = t["price"]
    n  = t["news"]
    c  = t["calendar"]

    role_instruction = {
        "news_sentiment": (
            "You are a news-driven trading analyst. Your PRIMARY strength is "
            "interpreting recent news, sentiment momentum, and sector trends. "
            "Weight news sentiment and sector momentum heavily. Use technical "
            "indicators only as secondary confirmation."
        ),
        "technical_reasoning": (
            "You are a technically-focused trading analyst. Your PRIMARY strength is "
            "interpreting price action, technical indicators, and risk. "
            "Weight RSI, MACD, and price structure heavily. Use news sentiment "
            "only as secondary context."
        ),
    }[model_role]

    headlines_block = "\n".join(
        f"  - {h}" for h in n["headlines"][:5]
    ) if n["headlines"] else "  (no recent headlines)"

    earnings_block = (
        f"EARNINGS IN {c['earnings_date']} — elevated uncertainty, reduce confidence."
        if c["earnings_soon"] else "No earnings event in the next 7 days."
    )

    return f"""
{role_instruction}

Analyze the following data for {t['ticker']} and decide: BUY, SELL, or HOLD.

── PRICE & TECHNICALS ──────────────────────────────────────
Current price:    ${p['current_price']} ({p['day_change_pct']:+.2f}% today)
RSI ({p['rsi']}):       {'OVERSOLD — potential bounce' if p['rsi_oversold'] else 'OVERBOUGHT — potential pullback' if p['rsi_overbought'] else 'neutral range'}
MACD:             {'BULLISH crossover' if p['macd_bullish'] else 'BEARISH crossover'} (histogram: {p['macd_histogram']})
EMA-20:           ${p['ema_20']} — price is {'ABOVE' if p['above_ema20'] else 'BELOW'} the 20-day moving average

── NEWS & SENTIMENT ────────────────────────────────────────
Sentiment:        {n['sentiment_label'].upper()} (score: {n['sentiment_score']}, {n['article_count']} articles)
Recent headlines:
{headlines_block}

── CALENDAR ────────────────────────────────────────────────
{earnings_block}

── YOUR TASK ────────────────────────────────────────────────
Return ONLY a valid JSON object. No markdown, no explanation outside the JSON.
Keys required:
  - ticker (string): the ticker symbol
  - action (string): exactly "buy", "sell", or "hold"
  - confidence (float): 0.0 to 1.0 — how confident you are (be conservative)
  - reasoning (string): 1-2 sentences explaining your decision
  - signals_used (list of strings): which signals drove the decision

Example format:
{{
  "ticker": "{t['ticker']}",
  "action": "hold",
  "confidence": 0.72,
  "reasoning": "RSI is neutral and MACD shows mild bearish momentum. Sentiment is positive but not strong enough to overcome technical weakness.",
  "signals_used": ["macd_bearish", "positive_sentiment", "above_ema20"]
}}
""".strip()


# ══════════════════════════════════════════════════════════════════════════════
# SECTION 2: Individual model callers
# ══════════════════════════════════════════════════════════════════════════════

def _parse_signal_response(raw: str, ticker: str, model_name: str) -> Optional[dict]:
    """
    Safely parse JSON from a model response.
    Handles markdown fences, whitespace, and malformed output gracefully.
    """
    # Strip markdown code fences if present
    cleaned = raw.strip()
    if cleaned.startswith("```"):
        lines = cleaned.split("\n")
        cleaned = "\n".join(
            l for l in lines
            if not l.strip().startswith("```")
        ).strip()

    try:
        signal = json.loads(cleaned)

        # Validate required fields
        required = {"ticker", "action", "confidence", "reasoning", "signals_used"}
        missing  = required - set(signal.keys())
        if missing:
            log.warning(f"  {model_name}/{ticker}: missing fields {missing}")
            return None

        # Normalize
        signal["action"]     = signal["action"].lower().strip()
        signal["confidence"] = max(0.0, min(1.0, float(signal["confidence"])))
        signal["model"]      = model_name

        if signal["action"] not in ("buy", "sell", "hold"):
            log.warning(f"  {model_name}/{ticker}: invalid action '{signal['action']}'")
            return None

        return signal

    except (json.JSONDecodeError, ValueError, TypeError) as e:
        log.error(f"  {model_name}/{ticker}: JSON parse failed — {e}")
        log.debug(f"  Raw response: {raw[:300]}")
        return None


def call_claude(ticker_data: dict) -> Optional[dict]:
    """
    Call Claude Sonnet for technical reasoning signal.
    Returns parsed signal dict or None on failure.
    """
    ticker = ticker_data["ticker"]
    prompt = _build_prompt(ticker_data, "technical_reasoning")

    log.info(f"  Calling Claude for {ticker}...")
    try:
        client = _get_anthropic()
        message = client.messages.create(
            model="claude-sonnet-4-5",
            max_tokens=512,
            system=(
                "You are a disciplined trading analyst. You always respond with "
                "valid JSON only — no markdown, no preamble, no explanation outside "
                "the JSON object. Be conservative with confidence scores."
            ),
            messages=[{"role": "user", "content": prompt}],
        )
        raw = message.content[0].text
        signal = _parse_signal_response(raw, ticker, "claude")
        if signal:
            log.info(f"  Claude → {ticker}: {signal['action'].upper()} "
                     f"(confidence={signal['confidence']:.2f})")
        return signal

    except anthropic.APIError as e:
        log.error(f"  Claude API error for {ticker}: {e}")
        return None


def call_gemini(ticker_data: dict) -> Optional[dict]:
    """
    Call Gemini 2.5 Flash for news/sentiment signal.
    Returns parsed signal dict or None on failure.
    """
    ticker = ticker_data["ticker"]
    prompt = _build_prompt(ticker_data, "news_sentiment")
    log.info(f"  Calling Gemini for {ticker}...")
    try:
        client   = _get_gemini()
        response = client.models.generate_content(
            model="gemini-2.5-flash",
            contents=prompt,
            config=types.GenerateContentConfig(
                temperature=0.2,
                max_output_tokens=4096,
                tools=[types.Tool(google_search=types.GoogleSearch())],
            ),
        )
        raw    = response.text
        signal = _parse_signal_response(raw, ticker, "gemini")
        if signal:
            log.info(f"  Gemini → {ticker}: {signal['action'].upper()} "
                     f"(confidence={signal['confidence']:.2f})")
        return signal
    except Exception as e:
        log.error(f"  Gemini API error for {ticker}: {e}")
        return None


# ══════════════════════════════════════════════════════════════════════════════
# SECTION 3: Parallel signal generation for one ticker
# ══════════════════════════════════════════════════════════════════════════════

def generate_signals(ticker_data: dict) -> dict:
    """
    Call both models in parallel for a single ticker.
    Returns:
      {
        "ticker":  "NVDA",
        "claude":  { ...signal... } | None,
        "gemini":  { ...signal... } | None,
      }
    """
    ticker = ticker_data["ticker"]
    results = {"ticker": ticker, "claude": None, "gemini": None}

    with ThreadPoolExecutor(max_workers=2) as executor:
        futures = {
            executor.submit(call_claude, ticker_data): "claude",
            executor.submit(call_gemini, ticker_data): "gemini",
        }
        for future in as_completed(futures):
            model_name = futures[future]
            try:
                results[model_name] = future.result()
            except Exception as e:
                log.error(f"  {model_name}/{ticker}: unexpected error — {e}")

    return results


# ══════════════════════════════════════════════════════════════════════════════
# SECTION 4: Batch signal generation for all tickers
# ══════════════════════════════════════════════════════════════════════════════

def generate_all_signals(
    all_ticker_data: dict,
    delay_between: float = 1.0,
) -> dict:
    """
    Generate signals for every ticker in the fetched data dict.
    Adds a short delay between tickers to respect API rate limits.

    Returns a dict keyed by ticker:
      {
        "NVDA": { "ticker": "NVDA", "claude": {...}, "gemini": {...} },
        "AMD":  { "ticker": "AMD",  "claude": {...}, "gemini": None },
        ...
      }
    """
    all_signals = {}
    tickers = list(all_ticker_data.keys())
    log.info(f"Generating signals for {len(tickers)} tickers...")

    skipped = 0
    cached  = 0

    for i, ticker in enumerate(tickers):
        log.info(f"[{i+1}/{len(tickers)}] {ticker}")
        ticker_data = all_ticker_data[ticker]

        # Check pre-filter — skip AI calls for quiet tickers
        if CACHE_ENABLED:
            skip, skip_reason = should_skip_ai_calls(ticker_data)
            if skip:
                all_signals[ticker] = get_skip_signal(ticker)
                skipped += 1
                continue

            # Check cache — reuse if data hasn't changed
            cached_signal = get_cached_signal(ticker, ticker_data)
            if cached_signal:
                all_signals[ticker] = cached_signal
                cached += 1
                continue

        signals = generate_signals(ticker_data)
        all_signals[ticker] = signals

        # Store in cache for next cycle
        if CACHE_ENABLED:
            cache_signals(ticker, ticker_data, signals)

        # Rate limit buffer between tickers (not needed within parallel calls)
        if i < len(tickers) - 1:
            time.sleep(delay_between)

    # Summary
    claude_ok = sum(1 for s in all_signals.values() if s.get("claude") is not None)
    gemini_ok = sum(1 for s in all_signals.values() if s.get("gemini") is not None)
    log.info(
        f"Signal generation complete — "
        f"Claude: {claude_ok}/{len(tickers)}, "
        f"Gemini: {gemini_ok}/{len(tickers)}, "
        f"cached: {cached}, skipped: {skipped}"
    )

    return all_signals


# ══════════════════════════════════════════════════════════════════════════════
# Quick test — run this file directly to verify API keys and signal output
# ══════════════════════════════════════════════════════════════════════════════

if __name__ == "__main__":
    print("\n=== Module 2: Signal Engine — Test Run ===\n")

    # Synthetic ticker data so you can test without running Module 1 first
    mock_ticker_data = {
        "ticker":    "NVDA",
        "timestamp": "2025-01-01T09:45:00+00:00",
        "price": {
            "current_price":  135.50,
            "day_change_pct": 1.82,
            "rsi":            58.4,
            "macd":           1.23,
            "macd_signal":    0.95,
            "macd_histogram": 0.28,
            "ema_20":         131.10,
            "rsi_oversold":   False,
            "rsi_overbought": False,
            "macd_bullish":   True,
            "above_ema20":    True,
        },
        "news": {
            "headlines": [
                "Nvidia announces next-gen Blackwell GPU shipping ahead of schedule",
                "AI data center demand continues to surge in Q4",
                "Morgan Stanley raises NVDA price target to $175",
            ],
            "sentiment_score":  0.72,
            "sentiment_label":  "positive",
            "article_count":    8,
        },
        "calendar": {
            "earnings_soon":    False,
            "earnings_date":    None,
            "earnings_estimate": None,
        },
        "summary": (
            "NVDA @ $135.50 (+1.82% today). RSI=58.4 (neutral). "
            "MACD bullish (histogram=0.28). News sentiment: positive "
            "(score=0.72, 8 articles)."
        ),
    }

    print(f"Testing with mock data for NVDA...\n")
    result = generate_signals(mock_ticker_data)

    print("\n── Claude signal ───────────────────────────────")
    if result["claude"]:
        for k, v in result["claude"].items():
            print(f"  {k:<15} {v}")
    else:
        print("  [failed or API key not set]")

    print("\n── Gemini signal ───────────────────────────────")
    if result["gemini"]:
        for k, v in result["gemini"].items():
            print(f"  {k:<15} {v}")
    else:
        print("  [failed or API key not set]")

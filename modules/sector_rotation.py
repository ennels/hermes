"""
Module 15: Sector Rotation Detection
──────────────────────────────────────
Weekly Gemini analysis detects sector money flow and adjusts
confidence + position size per sector automatically.

Hot sector:  conf +0.03 to +0.05, position size +15% to +25%
Cold sector: conf -0.03 to -0.05, position size -15% to -25%
Neutral:     no change

Runs Sunday 8pm ET as part of weekly maintenance.
"""

import os
import sys
import json
import re
import logging
from datetime import datetime, timezone
from typing import Optional

sys.path.append(os.path.join(os.path.dirname(__file__), ".."))

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger(__name__)

ROTATION_PATH     = os.path.join(os.path.dirname(__file__), "..", "logs", "sector_rotation.json")
ROTATION_TTL_DAYS = 8

SECTORS = {
    "semiconductors": ["NVDA", "AMD", "TSM", "AVGO"],
    "memory":         ["MU", "WDC", "MRVL"],
    "networking":     ["CRDO", "ALAB", "ANET", "LITE"],
    "infrastructure": ["VRT", "ETN", "GEV", "NVT", "EME", "PWR"],
    "clean_energy":   ["NEE", "CEG", "FLNC", "ENPH", "BEP"],
    "cloud":          ["MSFT", "GOOGL", "AMZN", "META"],
    "software":       ["NOW", "CRM", "SNOW"],
    "index":          ["SPY", "QQQ", "SOXX", "IWM"],
}

TICKER_SECTOR = {
    ticker: sector
    for sector, tickers in SECTORS.items()
    for ticker in tickers
}

MAX_CONF_DELTA     = 0.05
MAX_POSITION_DELTA = 0.25


# ── State management ──────────────────────────────────────────────────────────

def _load_rotation() -> dict:
    if not os.path.exists(ROTATION_PATH):
        return {"updated_at": None, "sectors": {}, "summary": ""}
    with open(ROTATION_PATH) as f:
        return json.load(f)

def _save_rotation(data: dict):
    os.makedirs(os.path.dirname(ROTATION_PATH), exist_ok=True)
    with open(ROTATION_PATH, "w") as f:
        json.dump(data, f, indent=2)

def get_sector_adjustments() -> dict:
    data = _load_rotation()
    if not data.get("updated_at"):
        return {}
    updated_at = datetime.fromisoformat(data["updated_at"])
    age_days   = (datetime.now(timezone.utc) - updated_at).days
    if age_days > ROTATION_TTL_DAYS:
        log.warning(f"Sector rotation data is {age_days} days old — using neutral")
        return {}
    return data.get("sectors", {})

def get_ticker_adjustments(ticker: str) -> dict:
    sector = TICKER_SECTOR.get(ticker)
    if not sector:
        return {"conf_delta": 0.0, "pos_delta": 0.0, "signal": "neutral"}
    return get_sector_adjustments().get(
        sector, {"conf_delta": 0.0, "pos_delta": 0.0, "signal": "neutral"}
    )


# ── Gemini two-step analysis ──────────────────────────────────────────────────

def run_sector_rotation_analysis() -> Optional[dict]:
    """
    Step 1: Gemini + search grounding gets plain text sector analysis.
    Step 2: Gemini (no search) converts plain text to clean JSON.
    Two-step avoids search grounding overriding JSON formatting.
    """
    try:
        from google import genai
        from google.genai import types
        from config.settings import GEMINI_API_KEY

        log.info("Running sector rotation analysis via Gemini...")
        client = genai.Client(api_key=GEMINI_API_KEY)

        # Step 1: plain text with search grounding
        step1 = client.models.generate_content(
            model="gemini-2.5-flash",
            contents=(
                "Search the web and summarize in plain text which stock market sectors "
                "are hot (institutional money flowing in) or cold (flowing out) right now. "
                "Cover: semiconductors, memory/storage chips, AI networking, data center "
                "infrastructure, clean energy, cloud computing, enterprise software. "
                "Be brief, 2-3 sentences per sector max."
            ),
            config=types.GenerateContentConfig(
                temperature=0.1,
                max_output_tokens=1024,
                tools=[types.Tool(google_search=types.GoogleSearch())],
            ),
        )
        analysis = step1.text.strip()[:1500]
        log.info(f"  Step 1 complete: {len(analysis)} chars")

        # Step 2: JSON conversion without search grounding
        template = (
            '{"semiconductors":{"signal":"neutral","conf_delta":0.0,"pos_delta":0.0,"reasoning":"reason"},'
            '"memory":{"signal":"neutral","conf_delta":0.0,"pos_delta":0.0,"reasoning":"reason"},'
            '"networking":{"signal":"neutral","conf_delta":0.0,"pos_delta":0.0,"reasoning":"reason"},'
            '"infrastructure":{"signal":"neutral","conf_delta":0.0,"pos_delta":0.0,"reasoning":"reason"},'
            '"clean_energy":{"signal":"neutral","conf_delta":0.0,"pos_delta":0.0,"reasoning":"reason"},'
            '"cloud":{"signal":"neutral","conf_delta":0.0,"pos_delta":0.0,"reasoning":"reason"},'
            '"software":{"signal":"neutral","conf_delta":0.0,"pos_delta":0.0,"reasoning":"reason"},'
            '"index":{"signal":"neutral","conf_delta":0.0,"pos_delta":0.0,"reasoning":"ETFs not adjusted"},'
            '"summary":"one sentence summary"}'
        )

        step2_prompt = (
            "Based on this sector analysis:\n\n"
            + analysis
            + "\n\nFill in this JSON template with real values. "
            "Return ONLY the JSON, no markdown, no explanation:\n\n"
            + template
            + "\n\nRules:\n"
            "- signal must be exactly: hot, cold, or neutral\n"
            "- hot: conf_delta +0.03 to +0.05, pos_delta +0.15 to +0.25\n"
            "- cold: conf_delta -0.03 to -0.05, pos_delta -0.15 to -0.25\n"
            "- neutral: both 0.0\n"
            "- Only mark hot/cold with clear evidence\n"
            "- reasoning must be under 80 characters"
        )

        step2 = client.models.generate_content(
            model="gemini-2.5-flash",
            contents=step2_prompt,
            config=types.GenerateContentConfig(
                temperature=0.0,
                max_output_tokens=4096,
            ),
        )

        raw     = step2.text.strip()
        cleaned = re.sub(r"```[a-zA-Z]*", "", raw).replace("```", "").strip()

        # Brace-matching JSON extraction
        start = cleaned.find("{")
        if start == -1:
            raise ValueError(f"No JSON found: {cleaned[:300]}")
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
            raise ValueError("Unmatched braces in JSON response")

        data    = json.loads(cleaned[start:end])
        summary = data.pop("summary", "")

        # Validate and clamp
        sectors = {}
        for sector in SECTORS:
            s = data.get(sector, {})
            sectors[sector] = {
                "signal":     s.get("signal", "neutral"),
                "conf_delta": max(-MAX_CONF_DELTA,
                                  min(MAX_CONF_DELTA,
                                      float(s.get("conf_delta", 0)))),
                "pos_delta":  max(-MAX_POSITION_DELTA,
                                  min(MAX_POSITION_DELTA,
                                      float(s.get("pos_delta", 0)))),
                "reasoning":  str(s.get("reasoning", "No data"))[:120],
            }

        result = {
            "updated_at": datetime.now(timezone.utc).isoformat(),
            "sectors":    sectors,
            "summary":    summary,
        }
        _save_rotation(result)
        log.info(f"Sector rotation complete: {summary[:80]}")
        return result

    except Exception as e:
        log.error(f"Sector rotation analysis failed: {e}")
        return None


# ── Apply adjustments to signals ──────────────────────────────────────────────

def apply_rotation_to_signal(ticker: str, signal: dict,
                              max_position_usd: float) -> tuple:
    adj        = get_ticker_adjustments(ticker)
    conf_delta = adj.get("conf_delta", 0.0)
    pos_delta  = adj.get("pos_delta", 0.0)
    signal_str = adj.get("signal", "neutral")

    if conf_delta == 0.0 and pos_delta == 0.0:
        return signal, max_position_usd

    old_conf    = signal.get("confidence", 0.65)
    new_conf    = round(max(0.0, min(1.0, old_conf + conf_delta)), 3)
    signal      = {**signal, "confidence": new_conf}
    new_max_pos = round(max_position_usd * (1 + pos_delta), 2)
    sector      = TICKER_SECTOR.get(ticker, "unknown")

    log.info(
        f"  {ticker} [{sector}] rotation={signal_str}: "
        f"conf {old_conf:.2f}->{new_conf:.2f}, "
        f"max_pos ${max_position_usd:.0f}->${new_max_pos:.0f}"
    )
    return signal, new_max_pos


# ── Notification ──────────────────────────────────────────────────────────────

def notify_rotation_update(result: dict):
    try:
        from modules.notifier import send_email, send_sms

        sectors  = result.get("sectors", {})
        summary  = result.get("summary", "")
        date_str = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        hot      = [s for s, d in sectors.items() if d["signal"] == "hot"]
        cold     = [s for s, d in sectors.items() if d["signal"] == "cold"]
        sms      = f"[ROTATION] Hot: {', '.join(hot) or 'none'} | Cold: {', '.join(cold) or 'none'}"

        rows = ""
        for s, d in sectors.items():
            sig = d["signal"]
            bg  = "#f0faf0" if sig == "hot" else "#faf0f0" if sig == "cold" else "#f8f8f8"
            fc  = "#060"    if sig == "hot" else "#c00"    if sig == "cold" else "#888"
            rows += (
                f"<tr style='background:{bg}'>"
                f"<td style='padding:6px 12px'>{s}</td>"
                f"<td style='padding:6px 12px;font-weight:bold;color:{fc}'>{sig.upper()}</td>"
                f"<td style='padding:6px 12px'>{d['conf_delta']:+.2f}</td>"
                f"<td style='padding:6px 12px'>{d['pos_delta']:+.0%}</td>"
                f"<td style='padding:6px 12px;color:#666;font-size:12px'>{d['reasoning']}</td>"
                f"</tr>"
            )

        html = (
            "<div style='font-family:sans-serif;max-width:700px'>"
            "<h2>Trading Bot — Weekly Sector Rotation</h2>"
            f"<p>{summary}</p>"
            "<table border='0' cellpadding='0' style='width:100%;border-collapse:collapse'>"
            "<tr style='background:#eee'>"
            "<th style='padding:8px 12px;text-align:left'>Sector</th>"
            "<th style='padding:8px 12px;text-align:left'>Signal</th>"
            "<th style='padding:8px 12px;text-align:left'>Conf adj</th>"
            "<th style='padding:8px 12px;text-align:left'>Size adj</th>"
            "<th style='padding:8px 12px;text-align:left'>Reasoning</th>"
            "</tr>"
            f"{rows}"
            "</table>"
            "<p style='font-size:11px;color:#888'>Active until next Sunday.</p>"
            "</div>"
        )

        send_sms(sms)
        send_email(
            f"Trading Bot Weekly Sector Rotation — {date_str}",
            sms + "\n\n" + summary,
            html,
        )
        log.info("Sector rotation notification sent")

    except Exception as e:
        log.error(f"Rotation notification failed: {e}")


# ── Quick test ────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    print("\n=== Module 15: Sector Rotation Detection — Test Run ===\n")

    print("Current sector adjustments (from saved data):")
    adjustments = get_sector_adjustments()
    if adjustments:
        for sector, adj in adjustments.items():
            sig = adj["signal"]
            bar = "UP" if sig == "hot" else "DN" if sig == "cold" else "--"
            print(f"  {bar} {sector:<20} conf:{adj['conf_delta']:+.2f}  "
                  f"pos:{adj['pos_delta']:+.0%}  {adj['reasoning'][:50]}")
    else:
        print("  No saved data — will run fresh analysis on Sunday")

    print("\nTicker adjustment examples:")
    for ticker in ["NVDA", "MU", "NEE", "MSFT", "SPY"]:
        adj    = get_ticker_adjustments(ticker)
        sector = TICKER_SECTOR.get(ticker, "unknown")
        print(f"  {ticker:<6} [{sector:<15}] signal={adj['signal']:<8} "
              f"conf:{adj['conf_delta']:+.2f}  pos:{adj['pos_delta']:+.0%}")

    print("\nRunning live Gemini analysis (real API call)...")
    result = run_sector_rotation_analysis()
    if result:
        print(f"\nSummary: {result['summary']}")
        print("\nSector signals:")
        for sector, adj in result["sectors"].items():
            sig = adj["signal"]
            bar = "UP" if sig == "hot" else "DN" if sig == "cold" else "--"
            print(f"  {bar} {sector:<20} conf:{adj['conf_delta']:+.2f}  "
                  f"pos:{adj['pos_delta']:+.0%}  {adj['reasoning'][:60]}")
        notify_rotation_update(result)
        print("\nNotification sent.")
    else:
        print("  Analysis failed — check logs")

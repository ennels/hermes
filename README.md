# Hermes

Autonomous paper-trading bot. Pulls market data and news, scores candidates with Claude and Gemini, filters them through layered risk rules, and executes through Alpaca — on a schedule, unattended.

**Status:** ran on a DigitalOcean droplet March–[month] 2026, passing 18/19 self-validation checks in its last audit. Droplet decommissioned; code is complete and runnable. Paper trading only.

## What it does

Every weekday:

| Time (ET) | Job |
|---|---|
| 9:00 | Morning checks — account health, sector rotation, portfolio rebalance, watchlist refresh |
| 9:45 | Main trading cycle → daily digest email |
| 9:30–15:30, hourly | Intraday stop-loss / take-profit monitor |
| Sat 9:00 | Weekly validation report |
| Sun 20:00 | Auto-tune strategy parameters |

The watchlist maintains itself: Gemini proposes tickers across sectors, stale tickers are pruned, recently traded tickers sit out a cooldown.

## Architecture

```
main.py — APScheduler (BackgroundScheduler) + keepalive loop
  │
  ├─ data_fetcher        Alpaca market data, Finnhub news/sentiment, Polygon fallback
  ├─ watchlist_manager   candidate universe, cooldowns, pruning
  ├─ sector_rotation     sector-level signals (Gemini, search-grounded)
  ├─ earnings_play       earnings-calendar setups
  ├─ signal_engine       technical signals → scored candidates
  ├─ signal_cache        dedupe and TTL for signals
  ├─ ai_advisor          per-ticker analysis (Claude) → structured recommendation
  ├─ risk_filter         confidence thresholds, position limits
  ├─ adaptive_risk       adjusts thresholds from recent performance
  ├─ portfolio_guardian  exposure and drawdown guards
  ├─ arbitration         resolves conflicting signals into a final order set
  ├─ executor            Alpaca order placement and account state
  ├─ intraday_monitor    stop-loss / take-profit checks during market hours
  ├─ param_manager       persisted strategy parameters
  ├─ auto_tuner          weekly parameter adjustment
  └─ notifier            SendGrid digest email; Twilio SMS

validate.py   — end-to-end health check (19 checks: keys, connectivity, module wiring)
dashboard.py  — CLI dashboard: positions, P&L, recent signals
config/       — settings and strategy parameters
docs/         — deployment manual
```

Runs as a `systemd` service (`Restart=always`). Cron couldn't reliably source the venv or survive SIGHUP; systemd handles restarts, logging, and boot persistence.

## Things I learned building it

- **Gemini search grounding and structured output don't mix in one call.** Grounded requests return prose, which breaks JSON parsing. Fix: grounded call for analysis, second ungrounded call to convert to JSON.
- **`BlockingScheduler` dies without a controlling terminal.** `BackgroundScheduler` plus a `while True: sleep(60)` keepalive is the pattern for a headless service.
- **Wiring drifts silently.** After adding modules, adaptive-risk adjustments weren't reaching the advisor — no errors, just quietly wrong decisions. `validate.py` exists because of this; every new module gets an end-to-end check.
- **Size swap conservatively.** 1 GB RAM + 2 GB swap was plenty for this workload.

## Stack

Python 3.12 · alpaca-py · anthropic · google-genai · finnhub-python · APScheduler · SendGrid · Twilio · systemd · Ubuntu 24.04 on DigitalOcean

## Run it

```
git clone https://github.com/ennels/hermes
cd hermes
python -m venv venv && source venv/bin/activate
pip install -r requirements.txt
cp .env.example .env     # fill in keys
python validate.py       # confirm everything is wired before trading
python main.py
```

`docs/SETUP_MANUAL.md` covers droplet provisioning and the systemd unit.

## Disclaimer

Paper trading only. Nothing here is financial advice. The strategy parameters are tuned for an aggressive paper account, not real money.

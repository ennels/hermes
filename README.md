# Hermes

A paper-trading bot I built and ran on a DigitalOcean droplet from March to April 2026. It runs on a schedule with nobody watching. It pulls market data and news, has Claude and Gemini each make a call on every ticker, runs the results through a set of risk rules, and places orders through Alpaca.

The droplet is shut down now. On its last validation run it passed 18 of 19 checks. It only ever traded on a paper account, never real money.

## Schedule

All times are Eastern, Monday through Friday unless noted.

- **9:00**: morning checks. Looks for new deposits, checks that the bot is healthy, and updates the watchlist.
- **9:45**: main trading cycle. Sends a text and an email for each trade it makes, then a daily digest.
- **9:30 to 15:30, every hour**: checks open positions against stop-loss and take-profit levels and closes out earnings trades.
- **Sunday 20:00**: weekly maintenance. Rebalances the portfolio, runs the sector rotation analysis, then auto-tunes the strategy parameters.

The watchlist mostly takes care of itself. Gemini, with Google Search grounding, suggests tickers to add and flags weak ones to drop. It never drops a ticker with an open position or one of the core ETFs (SPY, QQQ, SOXX, IWM). Any ticker that was just added or removed sits out a 7-day cooldown, so the list doesn't keep flipping.

## How it's put together

```
main.py               scheduled jobs + CLI helpers
modules/
  data_fetcher        Alpaca prices and indicators, Finnhub news and sentiment, Polygon as backup
  signal_engine       sends each ticker to Claude and Gemini in parallel
  signal_cache        dedupes signals and expires old ones
  arbitration         merges the two models' calls into one decision per ticker
  risk_filter         confidence floor, budget, position limits
  adaptive_risk       tightens or loosens risk based on recent results and volatility
  portfolio_guardian  deposit checks, rebalancing, concentration limits
  executor            places orders and reads account state from Alpaca
  intraday_monitor    stop-loss and take-profit checks during market hours
  earnings_play       trades around earnings dates and their exits
  sector_rotation     sector-level signals from Gemini with search grounding
  watchlist_manager   adds and drops tickers, with cooldowns
  param_manager       stores strategy parameters so I can change them without editing code
  auto_tuner          weekly parameter adjustments
  ai_advisor          Claude review of the portfolio that I run by hand (not part of the trading loop)
  notifier            SendGrid email and Twilio texts
validate.py           end-to-end check of keys, connections, and module wiring (19 checks)
dashboard.py          draws 7 performance charts from the trade log and saves them as a PNG
config/settings.py    watchlist, budget, and default strategy parameters
docs/SETUP_MANUAL.md  setup and deployment notes
```

On the droplet it ran as a systemd service with `Restart=always`. I tried cron first, but it couldn't reliably load the venv and the process didn't survive a SIGHUP. systemd takes care of restarts and logging, and starts the bot on boot.

## What I learned

- **Gemini can't do search grounding and structured JSON output in one call.** A grounded request comes back as prose, which broke my JSON parsing. I split it into two calls: a grounded one for the analysis, then an ungrounded one that converts that text to JSON.
- **APScheduler's `BlockingScheduler` dies without a terminal attached.** For a headless service I switched to `BackgroundScheduler` with a `while True: sleep(60)` loop to keep the process alive.
- **Modules can stop talking to each other without throwing errors.** After I added more modules, the adaptive risk adjustments stopped reaching the advisor. Nothing crashed, so I only caught it from the decisions it was making. That's why `validate.py` exists, and every new module gets a check in it.
- **A 1 GB droplet was enough** once I added 2 GB of swap.

## Stack

Python 3.12, alpaca-py, anthropic, google-genai, Finnhub, APScheduler, SendGrid, Twilio, systemd, Ubuntu 24.04 on DigitalOcean

## Running it

```
git clone https://github.com/ennels/hermes
cd hermes
python -m venv venv && source venv/bin/activate
pip install -r requirements.txt
cp .env.example .env     # add your API keys
python validate.py       # make sure everything is connected before it trades
python main.py
```

`docs/SETUP_MANUAL.md` covers the droplet setup, the systemd unit, and the CLI helpers.

## Disclaimer

Don't put your mortgage on this thing. The default parameters are aggressive on purpose, since no real money was at stake.

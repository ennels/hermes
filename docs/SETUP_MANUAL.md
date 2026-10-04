# Setup and operations (Written with Claude)

How to get Hermes running locally and on a server. Everything here assumes an Alpaca paper account.

## API keys

You'll need keys for:

- **Alpaca**: a paper trading key ID and secret. The secret is only shown once.
- **Finnhub**: news and sentiment.
- **Polygon**: backup market data.
- **Anthropic** and **Google AI Studio** (Gemini).
- **Twilio**: account SID, auth token, and a trial phone number for texts.
- **SendGrid**: an API key, plus a verified single sender for the FROM address. Email won't send until the sender is verified.

Copy `.env.example` to `.env`, fill it in, and lock it down with `chmod 600 .env`.

## Local setup

```bash
python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt
python validate.py
```

`validate.py` checks each module, the connections between them, and that every key in `.env` is set. Fix anything marked ✗ before you run a cycle.

The defaults live in `config/settings.py`:

```python
TOTAL_BUDGET_USD = 1000.0   # total capital the bot can use
MAX_POSITION_USD = 200.0    # max per ticker
MAX_POSITIONS    = 7        # max open positions at once
STOP_LOSS_PCT    = 0.05     # sell at -5%
TAKE_PROFIT_PCT  = 0.15     # sell at +15%
MIN_CONFIDENCE   = 0.65     # skip trades below this confidence
RUN_TIME_ET      = "09:45"  # daily trading cycle
```

## Running it

Run one cycle right away (good for a first test):

```bash
python -c "from main import run_now; run_now()"
```

Start the scheduler:

```bash
python main.py
```

Alpaca's free data only returns price bars during market hours (9:30 to 16:00 ET). If you test outside those hours you'll get "No price bars returned", which is expected.

## Adaptive risk

At the start of every cycle the bot picks a risk mode based on its recent streak, drawdown, win rate, and market volatility. It checks them in order, top to bottom:

| Mode | When | What changes |
|---|---|---|
| DEFENSIVE | drawdown over 15%, or 4+ losses in a row | 40% position size, tighter stops, higher confidence bar, max 2 positions |
| CAUTIOUS | drawdown over 8%, 2+ losses in a row, or elevated/high volatility | 65% position size, slightly tighter stops, max 3 positions |
| AGGRESSIVE | 4+ wins in a row, win rate over 65%, low volatility | 140% position size, looser stops, lower confidence bar |
| CONFIDENT | 2+ wins in a row, win rate over 58%, volatility not high | 120% position size, slightly looser stops |
| NORMAL | everything else | values from `settings.py` |

To turn it off and use fixed parameters:

```bash
python -c "from main import set_param; set_param('adaptive_risk', False)"
```

## Changing parameters

Parameters can be changed while the bot is running. Changes apply on the next cycle.

```bash
python -c "from main import show_params; show_params()"
python -c "from main import set_param; set_param('stop_loss_pct', 0.03)"
python -c "from main import set_param; set_param('watchlist', ['NVDA','MSFT','SPY'])"
python -c "from main import reset_params; reset_params()"
```

| Parameter | Range | Notes |
|---|---|---|
| total_budget_usd | 10 to 100,000 | total capital ($) |
| max_position_usd | 5 to 10,000 | max per ticker ($) |
| max_positions | 1 to 20 | |
| stop_loss_pct | 0.01 to 0.20 | 0.05 = 5% |
| take_profit_pct | 0.02 to 0.50 | 0.15 = 15% |
| min_confidence | 0.50 to 0.95 | |
| news_lookback_hours | 1 to 168 | |
| gemini_weight_news | 0.0 to 1.0 | how much Gemini's call counts on news signals |
| claude_weight_tech | 0.0 to 1.0 | how much Claude's call counts on technical signals |
| run_time_et | HH:MM | |
| watchlist | list of tickers | |
| adaptive_risk | true / false | |

## Other CLI helpers

Everything below is a function in `main.py`. Call it with `python -c "from main import NAME; NAME()"`.

| Function | What it does |
|---|---|
| `pause("reason")`, `resume()` | stop or restart scheduled trading |
| `advise()`, `review()`, `recommend_params()` | Claude reviews the portfolio, trade history, and parameters |
| `ask("question")` | ask Claude anything about the current setup |
| `tune_now()`, `show_tune_log()` | run the auto-tuner now, or see what it changed |
| `rebalance_now()`, `rotation_now()`, `watchlist_now()`, `intraday_now()` | run one of the scheduled jobs on demand |
| `check_deposits()`, `earnings_stats()`, `sector_rotation()`, `watchlist_history()` | status and history |

## Monitoring

`python dashboard.py` saves 7 performance charts to `logs/performance_dashboard.png`.

Trades are logged to `logs/trades.db`:

```bash
sqlite3 logs/trades.db "SELECT timestamp, ticker, action, total_usd, confidence, status FROM trades ORDER BY id DESC LIMIT 20;"
```

## Deploying to a droplet

I used a $6/month Basic droplet (1 GB RAM) running Ubuntu 24.04, with 2 GB of swap added.

```bash
apt update && apt upgrade -y
apt install python3-venv python3-pip -y

# 2 GB swap
fallocate -l 2G /swapfile && chmod 600 /swapfile
mkswap /swapfile && swapon /swapfile
echo '/swapfile none swap sw 0 0' >> /etc/fstab

git clone https://github.com/ennels/hermes /root/hermes
cd /root/hermes
python3 -m venv venv && source venv/bin/activate
pip install -r requirements.txt
# copy your .env up with scp, then:
python validate.py
```

Run it as a systemd service so it restarts on its own and comes back after a reboot. I tried cron first and it was unreliable.

```bash
cat > /etc/systemd/system/hermes.service << 'EOF'
[Unit]
Description=Hermes trading bot
After=network.target

[Service]
Type=simple
User=root
WorkingDirectory=/root/hermes
ExecStart=/root/hermes/venv/bin/python main.py
Restart=always
RestartSec=30

[Install]
WantedBy=multi-user.target
EOF

systemctl daemon-reload
systemctl enable --now hermes
journalctl -u hermes -f   # follow the logs
```

## Troubleshooting

- **"No price bars returned"**: you're outside market hours.
- **Texts arrive but email doesn't**: the SendGrid sender isn't verified yet.
- **"Budget exhausted" is blocking trades**: the whole budget is in open positions. Wait for some to close, or raise `total_budget_usd`.
- **Every signal is "hold"**: the confidence floor is probably too high. Try `set_param('min_confidence', 0.60)`, or ask the advisor why with `ask('Why are all signals hold?')`.

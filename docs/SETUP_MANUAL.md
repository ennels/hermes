# Trading Bot — macOS Setup & Operations Manual

---

## What This Bot Does

This bot automatically trades stocks once per market day (9:45am ET).
It fetches live market data and news, runs both Claude and Gemini AI in parallel,
reconciles their signals, applies adaptive safety rules, and places approved trades
on Alpaca. You get an SMS + email after every trade plus a daily summary.

Three new systems:
- **Adaptive risk** — auto-tightens/loosens stops based on win streak and volatility
- **Parameter manager** — change any setting live without touching code
- **AI advisor** — ask Claude for strategic guidance on your portfolio anytime

---

## PART 1 — Mac Environment Setup

### 1.1 Install Homebrew

Open Terminal (Cmd+Space → "Terminal" → Enter):

```bash
/bin/bash -c "$(curl -fsSL https://raw.githubusercontent.com/Homebrew/install/HEAD/install.sh)"
```

**Apple Silicon (M1/M2/M3/M4) only:** After install, run the two `echo`/`eval` commands
Homebrew prints before continuing.

Verify: `brew --version`

### 1.2 Install Python 3.11

```bash
brew install python@3.11
python3.11 --version   # should print Python 3.11.x
```

---

## PART 2 — Account Setup (all free tiers)

### 2.1 Alpaca (Broker)
1. https://alpaca.markets → Sign Up → Individual account
2. Profile icon → Your API Keys → Paper Trading → Generate New Key
3. Save both Key ID and Secret Key (secret shown once only)

### 2.2 Finnhub (News)
1. https://finnhub.io → Get free API key → copy key from dashboard

### 2.3 Anthropic / Claude
1. https://console.anthropic.com → API Keys → Create Key → name "trading-bot"
2. Copy key (starts with sk-ant-)

### 2.4 Google Gemini
1. https://aistudio.google.com → Get API key → Create API key
2. Copy key (starts with AIza)

### 2.5 Twilio (SMS)
1. https://twilio.com → Sign up → verify your mobile number
2. Dashboard: copy Account SID and Auth Token
3. Get a Trial Phone Number → copy it

### 2.6 SendGrid (Email)
1. https://sendgrid.com → Start for Free
2. Settings → API Keys → Create API Key → Full Access → copy key (starts with SG.)
3. Settings → Sender Authentication → Verify a Single Sender → verify your FROM address

---

## PART 3 — Project Setup

```bash
mkdir -p ~/trading_bot/logs ~/trading_bot/data
cd ~/trading_bot
```

Place all project files in ~/trading_bot/ matching this structure:

```
~/trading_bot/
├── config/
│   ├── __init__.py
│   └── settings.py
├── modules/
│   ├── __init__.py
│   ├── data_fetcher.py
│   ├── signal_engine.py
│   ├── arbitration.py
│   ├── risk_filter.py
│   ├── executor.py
│   ├── notifier.py
│   ├── adaptive_risk.py
│   ├── param_manager.py
│   └── ai_advisor.py
├── logs/
├── data/
├── main.py
├── requirements.txt
└── .env.example
```

### Create virtual environment

```bash
cd ~/trading_bot
python3.11 -m venv venv
source venv/bin/activate    # prompt shows (venv) when active
pip install -r requirements.txt
```

You must run `source venv/bin/activate` every time you open a new Terminal for this project.

### Create your .env file

```bash
cp .env.example .env
open -e .env
```

Fill in all values, save (Cmd+S), close TextEdit. Then:

```bash
chmod 600 ~/trading_bot/.env   # hide from other users
```

---

## PART 4 — Configuration

```bash
open -e ~/trading_bot/config/settings.py
```

Key settings to review:

```python
WATCHLIST        = ["NVDA", "MSFT", "AMD", ...]   # tickers to watch
TOTAL_BUDGET_USD = 1000.0    # total capital to deploy
MAX_POSITION_USD = 200.0     # max per stock
MAX_POSITIONS    = 5         # max open at once
STOP_LOSS_PCT    = 0.05      # sell if down 5%
TAKE_PROFIT_PCT  = 0.15      # sell if up 15%
MIN_CONFIDENCE   = 0.65      # min AI confidence to trade
RUN_TIME_ET      = "09:45"   # 9:45am ET = 6:45am Hawaii Time
```

---

## PART 5 — Test Each Module

Always activate venv first: `source venv/bin/activate`

```bash
python modules/data_fetcher.py    # price, RSI, MACD, news for NVDA
python modules/signal_engine.py   # Claude + Gemini signals (needs API keys)
python modules/arbitration.py     # conflict resolution logic (no keys needed)
python modules/risk_filter.py     # budget/stop-loss rules (no keys needed)
python modules/executor.py        # Alpaca account info
python modules/notifier.py        # sends test SMS + email
python modules/adaptive_risk.py   # risk mode + adjusted parameters
python modules/param_manager.py   # parameter CRUD + history
python modules/ai_advisor.py      # full Claude portfolio review
```

Note: data_fetcher and executor only return data during market hours
(6:30am–1:00pm Hawaii Time / 9:30am–4:00pm ET).

---

## PART 6 — Running the Bot

### Run one cycle immediately (do this first)

```bash
cd ~/trading_bot && source venv/bin/activate
python -c "from main import run_now; run_now()"
```

Watch the terminal for the full step-by-step output.

### Start the scheduled bot

```bash
python main.py
```

Runs Mon–Fri at 9:45am ET (6:45am Hawaii). Keep Terminal open or deploy
to DigitalOcean (Part 10) for 24/7 operation.

---

## PART 7 — Adaptive Risk (Automatic)

Runs automatically at the start of every cycle. No action needed.

| Mode       | Trigger                          | Effect                           |
|------------|----------------------------------|----------------------------------|
| DEFENSIVE  | 4+ losses or >15% drawdown       | 40% position size, tight stops  |
| CAUTIOUS   | 2+ losses or high volatility     | 65% position size, tighter stops|
| NORMAL     | Baseline                         | settings.py values               |
| CONFIDENT  | 2+ win streak, >58% win rate     | 120% position size               |
| AGGRESSIVE | 4+ win streak, low volatility    | 140% position size               |

Check current mode:
```bash
python -c "from main import show_params; show_params()"
```

Disable adaptive risk (use fixed params only):
```bash
python -c "from main import set_param; set_param('adaptive_risk', False)"
```

---

## PART 8 — Manual Parameter Adjustment

Change any setting live — takes effect on the next cycle.

```bash
# View all current parameters
python -c "from main import show_params; show_params()"

# Examples
python -c "from main import set_param; set_param('stop_loss_pct', 0.03)"
python -c "from main import set_param; set_param('min_confidence', 0.75)"
python -c "from main import set_param; set_param('max_position_usd', 150.0)"
python -c "from main import set_param; set_param('watchlist', ['NVDA','MSFT','SPY'])"
python -c "from main import set_param; set_param('run_time_et', '10:00')"

# Reset everything to defaults
python -c "from main import reset_params; reset_params()"
```

All adjustable parameters and their safe ranges:

| Parameter           | Min    | Max       | Description                   |
|---------------------|--------|-----------|-------------------------------|
| total_budget_usd    | 10     | 100,000   | Total capital ($)             |
| max_position_usd    | 5      | 10,000    | Max per ticker ($)            |
| max_positions       | 1      | 20        | Max open positions            |
| stop_loss_pct       | 0.01   | 0.20      | Stop-loss (0.05 = 5%)         |
| take_profit_pct     | 0.02   | 0.50      | Take-profit (0.15 = 15%)      |
| min_confidence      | 0.50   | 0.95      | Min AI confidence             |
| run_time_et         | —      | —         | HH:MM format                  |
| watchlist           | —      | —         | List of ticker strings        |
| adaptive_risk       | —      | —         | true / false                  |
| news_lookback_hours | 1      | 168       | Hours of news history         |
| gemini_weight_news  | 0.0    | 1.0       | Gemini news signal weight     |
| claude_weight_tech  | 0.0    | 1.0       | Claude technical signal weight|

---

## PART 9 — AI Advisor

Ask Claude for strategic guidance anytime. It has full visibility into your
trade history, positions, parameters, and market conditions.

```bash
# Full weekly briefing (do this every Sunday)
python -c "from main import advise; advise()"

# Portfolio health review
python -c "from main import review; review()"

# Should I change my parameters?
python -c "from main import recommend_params; recommend_params()"

# Ask anything
python -c "from main import ask; ask('Is my stop-loss too tight right now?')"
python -c "from main import ask; ask('Why are so many trades being blocked?')"
python -c "from main import ask; ask('Should I add energy ETFs to my watchlist?')"
python -c "from main import ask; ask('What sectors are trending this month?')"
```

---

## PART 10 — Run 24/7 on DigitalOcean

### Create a Droplet
1. https://digitalocean.com → Create → Droplets
2. Ubuntu 24.04 LTS · Basic · Regular · $6/month · San Francisco
3. Set a root password → Create Droplet
4. Copy the IP address

### Connect and set up
```bash
# From your Mac:
ssh root@YOUR_DROPLET_IP

# On the server:
apt update && apt upgrade -y
apt install python3.11 python3.11-venv python3-pip screen -y
```

### Upload project from your Mac
```bash
# New Terminal tab on Mac (Cmd+T):
scp -r ~/trading_bot root@YOUR_DROPLET_IP:/root/trading_bot
```

### Install and run on server
```bash
# Back on server:
cd /root/trading_bot
python3.11 -m venv venv
source venv/bin/activate
pip install -r requirements.txt

screen -S bot
python main.py
# Press Ctrl+A then D to detach
```

To check on it: `screen -r bot`

### Optional: run as a service (survives reboots)
```bash
cat > /etc/systemd/system/trading-bot.service << 'EOF'
[Unit]
Description=Trading Bot
After=network.target

[Service]
Type=simple
User=root
WorkingDirectory=/root/trading_bot
ExecStart=/root/trading_bot/venv/bin/python main.py
Restart=on-failure
RestartSec=30

[Install]
WantedBy=multi-user.target
EOF

systemctl daemon-reload
systemctl enable trading-bot
systemctl start trading-bot
```

---

## PART 11 — Switching to Live Trading

After 4+ weeks of satisfactory paper trading results:

1. Complete identity verification at https://app.alpaca.markets
2. Fund your account via ACH transfer
3. Generate Live API keys (separate from paper keys)
4. Update .env:
   ```
   ALPACA_API_KEY=YOUR_LIVE_KEY
   ALPACA_SECRET_KEY=YOUR_LIVE_SECRET
   ALPACA_BASE_URL=https://api.alpaca.markets
   ```
5. Restart bot. Start with a small budget ($500).

---

## PART 12 — Monitoring

```bash
# Last 20 trades
python -c "
import sqlite3
conn = sqlite3.connect('logs/trades.db')
for r in conn.execute('SELECT timestamp, ticker, action, total_usd, confidence, status FROM trades ORDER BY id DESC LIMIT 20'):
    print(f'{r[0][:16]}  {r[2].upper():4}  {r[1]:6}  \${r[3] or 0:.0f}  conf={r[4] or 0:.2f}  [{r[5]}]')
"

# Open positions
python -c "
from modules.executor import get_alpaca_positions
for p in get_alpaca_positions():
    print(f'{p[\"ticker\"]}: {p[\"quantity\"]:.4f} shares  P&L {p[\"unrealized_plpc\"]:+.1f}%')
"

# Account summary
python -c "
from modules.executor import get_account_info
i = get_account_info()
print(f'Equity: \${i[\"equity\"]:,.2f}  Cash: \${i[\"cash\"]:,.2f}  P&L today: \${i[\"pnl_today\"]:+,.2f}')
"
```

---

## PART 13 — Troubleshooting

**"command not found: python3.11"**
Run `brew install python@3.11` then try again.

**"source: No such file"**
You're not in the right folder. Run `cd ~/trading_bot` first.

**"No price bars returned"**
Alpaca free data only serves during market hours: 6:30am–1:00pm Hawaii.
This is fine — the bot runs during those hours automatically.

**SMS arrives but email doesn't**
Verify your FROM address: sendgrid.com → Settings → Sender Authentication.

**"Budget exhausted" blocking trades**
Your full budget is deployed. Wait for positions to close, or:
`python -c "from main import set_param; set_param('total_budget_usd', 2000.0)"`

**All signals are "hold"**
Confidence floor may be too high:
`python -c "from main import set_param; set_param('min_confidence', 0.60)"`
Or ask the advisor: `python -c "from main import ask; ask('Why are all signals hold?')"`

---

## Quick Reference

```
DAILY OPERATIONS
python main.py                              Start scheduled bot
python -c "from main import run_now; run_now()"       Run now
python -c "from main import pause; pause('reason')"   Pause
python -c "from main import resume; resume()"         Resume

PARAMETERS
python -c "from main import show_params; show_params()"
python -c "from main import set_param; set_param('KEY', VALUE)"
python -c "from main import reset_params; reset_params()"

AI ADVISOR
python -c "from main import advise; advise()"
python -c "from main import review; review()"
python -c "from main import recommend_params; recommend_params()"
python -c "from main import ask; ask('your question')"

MODULE TESTS
python modules/data_fetcher.py
python modules/signal_engine.py
python modules/executor.py
python modules/notifier.py
python modules/adaptive_risk.py
python modules/ai_advisor.py
```

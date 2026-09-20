import os
from dotenv import load_dotenv

load_dotenv()

# ── API Keys ────────────────────────────────────────────────
ALPACA_API_KEY    = os.getenv("ALPACA_API_KEY")
ALPACA_SECRET_KEY = os.getenv("ALPACA_SECRET_KEY")
ALPACA_BASE_URL   = os.getenv("ALPACA_BASE_URL", "https://paper-api.alpaca.markets")

FINNHUB_API_KEY   = os.getenv("FINNHUB_API_KEY")
POLYGON_API_KEY   = os.getenv("POLYGON_API_KEY")

GEMINI_API_KEY    = os.getenv("GEMINI_API_KEY")
ANTHROPIC_API_KEY = os.getenv("ANTHROPIC_API_KEY")

TWILIO_ACCOUNT_SID  = os.getenv("TWILIO_ACCOUNT_SID")
TWILIO_AUTH_TOKEN   = os.getenv("TWILIO_AUTH_TOKEN")
TWILIO_FROM_NUMBER  = os.getenv("TWILIO_FROM_NUMBER")
ALERT_PHONE_NUMBER  = os.getenv("ALERT_PHONE_NUMBER")

SENDGRID_API_KEY    = os.getenv("SENDGRID_API_KEY")
ALERT_EMAIL_FROM    = os.getenv("ALERT_EMAIL_FROM")
ALERT_EMAIL_TO      = os.getenv("ALERT_EMAIL_TO")

# ── Tickers to watch ────────────────────────────────────────
# Edit this list freely — these are just starter suggestions
# across sectors you flagged: AI/tech, semiconductors, energy
WATCHLIST = [

    # ── Memory & Storage (Micron focus) ───────────────────────────
    "MU",     # Micron — HBM3E powers Nvidia GPUs, key AI memory play
    "WDC",    # Western Digital — eSSD demand surging with AI data growth
    "MRVL",   # Marvell — custom AI chips for AWS + Microsoft, HBM interconnects

    # ── AI Chips & Compute ────────────────────────────────────────
    "NVDA",   # Nvidia — undisputed AI infrastructure leader
    "AMD",    # AMD — GPU alternative gaining data center share
    "TSM",    # TSMC — manufactures virtually every advanced AI chip
    "AVGO",   # Broadcom — custom AI ASICs for Google + Meta

    # ── Data Center Power & Cooling ───────────────────────────────
    "VRT",    # Vertiv — liquid cooling + power systems for AI racks
    "ETN",    # Eaton — electrical systems managing data center power
    "GEV",    # GE Vernova — turbines + power generation, surging AI orders
    "NVT",    # nVent Electric — high-density power distribution for AI

    # ── Clean Energy & Storage (AI power demand) ──────────────────
    "NEE",    # NextEra Energy — largest US renewables operator
    "CEG",    # Constellation Energy — nuclear baseload for data centers
    "FLNC",   # Fluence Energy — battery storage paired with renewables
    "ENPH",   # Enphase — solar + battery storage systems
    "BEP",    # Brookfield Renewable — wind/solar/hydro at scale

    # ── AI Networking & Connectivity ──────────────────────────────
    "CRDO",   # Credo Technology — high-speed copper cables for data centers
    "ALAB",   # Astera Labs — PCIe/CXL connectivity chips, tripling revenue
    "ANET",   # Arista Networks — 800G switches for AI GPU clusters
    "LITE",   # Lumentum — fiber optic transceivers + lasers for AI networks

    # ── Cloud Hyperscalers (AI integration partnerships) ──────────
    "MSFT",   # Microsoft — OpenAI partnership, Azure AI fastest growing
    "GOOGL",  # Google — Gemini, TPUs, $75B capex 2026
    "AMZN",   # Amazon — AWS Trainium/Inferentia custom AI chips
    "META",   # Meta — building 2GW AI data center, Llama open source

    # ── AI Software & Applications (enterprise adoption) ──────────
    "NOW",    # ServiceNow — AI agents embedded across enterprise workflows
    "CRM",    # Salesforce — Agentforce AI, largest enterprise AI rollout
    "SNOW",   # Snowflake — AI data cloud, partnerships with all major LLMs

    # ── Data Center Construction & Infrastructure ─────────────────
    "EME",    # EMCOR — builds data center electrical infrastructure
    "PWR",    # Quanta Services — utility grid + data center construction

    # ── Broad Market Hedge ────────────────────────────────────────
    "SPY",    # S&P 500 ETF — market baseline
    "QQQ",    # Nasdaq 100 — tech-weighted hedge
    "SOXX",   # iShares Semiconductor ETF — broad chip sector exposure
]

# ── Bot behavior ─────────────────────────────────────────────
TOTAL_BUDGET_USD     = 1000.0   # total capital this bot can deploy
MAX_POSITION_USD     = 200.0    # max per single ticker
MAX_POSITIONS        = 7        # max simultaneous open positions
STOP_LOSS_PCT        = 0.05     # sell if a position drops 5%
TAKE_PROFIT_PCT      = 0.15     # sell if a position gains 15%
MIN_CONFIDENCE       = 0.65     # don't trade below this confidence score

# ── Data fetching ─────────────────────────────────────────────
NEWS_LOOKBACK_HOURS  = 24       # how far back to pull news
RSI_PERIOD           = 14       # standard RSI window
MACD_FAST            = 12
MACD_SLOW            = 26
MACD_SIGNAL          = 9

# ── Scheduling ────────────────────────────────────────────────
# Run the bot once per market day at 9:45am ET (after open volatility settles)
RUN_TIME_ET          = "09:45"

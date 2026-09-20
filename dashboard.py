"""
Performance Dashboard
──────────────────────
Generates 7 charts from your trade history and saves them as a PNG.

Run with:
  python dashboard.py

Output: logs/performance_dashboard.png
"""

import os
import sys
import json
import sqlite3
from datetime import datetime, timezone, timedelta

import matplotlib
matplotlib.use("Agg")  # non-interactive backend — works on server with no display
import matplotlib.pyplot as plt
import matplotlib.dates as mdates
import matplotlib.gridspec as gridspec
import requests
import pandas as pd
import numpy as np

sys.path.append(os.path.dirname(__file__))
from config.settings import (
    ALPACA_API_KEY, ALPACA_SECRET_KEY, ALPACA_BASE_URL
)

DB_PATH        = os.path.join(os.path.dirname(__file__), "logs", "trades.db")
PARAMS_HISTORY = os.path.join(os.path.dirname(__file__), "logs", "params_history.json")
AUTO_TUNE_LOG  = os.path.join(os.path.dirname(__file__), "logs", "auto_tune_log.json")
OUTPUT_PATH    = os.path.join(os.path.dirname(__file__), "logs", "performance_dashboard.png")

# ── Style ─────────────────────────────────────────────────────────────────────
DARK_BG    = "#0f1117"
PANEL_BG   = "#1a1d2e"
ACCENT     = "#7c6af7"
GREEN      = "#2ecc71"
RED        = "#e74c3c"
AMBER      = "#f39c12"
GRAY       = "#4a4d6a"
TEXT       = "#e0e0f0"
TEXT_DIM   = "#8888aa"

plt.rcParams.update({
    "figure.facecolor":  DARK_BG,
    "axes.facecolor":    PANEL_BG,
    "axes.edgecolor":    GRAY,
    "axes.labelcolor":   TEXT,
    "xtick.color":       TEXT_DIM,
    "ytick.color":       TEXT_DIM,
    "text.color":        TEXT,
    "grid.color":        GRAY,
    "grid.alpha":        0.3,
    "font.family":       "monospace",
    "font.size":         9,
})


# ══════════════════════════════════════════════════════════════════════════════
# SECTION 1: Data loaders
# ══════════════════════════════════════════════════════════════════════════════

def load_trades() -> pd.DataFrame:
    """Load all executed trades from SQLite."""
    if not os.path.exists(DB_PATH):
        return pd.DataFrame()
    conn = sqlite3.connect(DB_PATH)
    df   = pd.read_sql_query("""
        SELECT timestamp, ticker, action, quantity, price, total_usd,
               confidence, signal_type, arbitration, status
        FROM trades
        WHERE status = 'executed'
        ORDER BY timestamp ASC
    """, conn)
    conn.close()
    if df.empty:
        return df
    df["timestamp"] = pd.to_datetime(df["timestamp"])
    df["total_usd"] = pd.to_numeric(df["total_usd"], errors="coerce").fillna(0)
    df["price"]     = pd.to_numeric(df["price"],     errors="coerce").fillna(0)
    return df


def load_account_history() -> pd.DataFrame:
    """
    Fetch portfolio history from Alpaca.
    Falls back to reconstructing from trades if unavailable.
    """
    try:
        paper  = "paper" in ALPACA_BASE_URL
        base   = "https://paper-api.alpaca.markets" if paper else "https://api.alpaca.markets"
        resp   = requests.get(
            f"{base}/v2/account/portfolio/history",
            headers={
                "APCA-API-KEY-ID":     ALPACA_API_KEY,
                "APCA-API-SECRET-KEY": ALPACA_SECRET_KEY,
            },
            params={"period": "1M", "timeframe": "1D"},
            timeout=10,
        )
        resp.raise_for_status()
        data   = resp.json()
        timestamps = [datetime.fromtimestamp(t) for t in data.get("timestamp", [])]
        equity     = data.get("equity", [])
        if timestamps and equity:
            df = pd.DataFrame({"timestamp": timestamps, "equity": equity})
            df["equity"] = pd.to_numeric(df["equity"], errors="coerce")
            return df
    except Exception as e:
        print(f"  Could not fetch Alpaca history: {e}")
    return pd.DataFrame()


def load_spy_returns(days: int = 30) -> pd.DataFrame:
    """Fetch SPY price history from Alpaca for benchmark comparison."""
    try:
        end   = datetime.now(timezone.utc)
        start = end - timedelta(days=days + 5)
        resp  = requests.get(
            "https://data.alpaca.markets/v2/stocks/SPY/bars",
            headers={
                "APCA-API-KEY-ID":     ALPACA_API_KEY,
                "APCA-API-SECRET-KEY": ALPACA_SECRET_KEY,
            },
            params={
                "start":     start.strftime("%Y-%m-%dT%H:%M:%SZ"),
                "end":       end.strftime("%Y-%m-%dT%H:%M:%SZ"),
                "timeframe": "1Day",
                "feed":      "iex",
            },
            timeout=10,
        )
        resp.raise_for_status()
        bars = resp.json().get("bars", [])
        if bars:
            df = pd.DataFrame(bars)
            df["t"] = pd.to_datetime(df["t"])
            df       = df.rename(columns={"t": "timestamp", "c": "close"})
            return df[["timestamp", "close"]]
    except Exception as e:
        print(f"  Could not fetch SPY data: {e}")
    return pd.DataFrame()


def load_auto_tune_log() -> list:
    if not os.path.exists(AUTO_TUNE_LOG):
        return []
    with open(AUTO_TUNE_LOG) as f:
        return json.load(f)


def load_params_history() -> list:
    if not os.path.exists(PARAMS_HISTORY):
        return []
    with open(PARAMS_HISTORY) as f:
        return json.load(f)


def load_earnings_plays() -> list:
    """Load earnings play history."""
    path = os.path.join(os.path.dirname(__file__), "logs", "earnings_plays.json")
    if not os.path.exists(path):
        return []
    with open(path) as f:
        return json.load(f)

def load_sector_rotation() -> dict:
    """Load latest sector rotation data."""
    path = os.path.join(os.path.dirname(__file__), "logs", "sector_rotation.json")
    if not os.path.exists(path):
        return {}
    with open(path) as f:
        return json.load(f)

def load_watchlist_history() -> list:
    """Load watchlist change history."""
    path = os.path.join(os.path.dirname(__file__), "logs", "watchlist_changes.json")
    if not os.path.exists(path):
        return []
    with open(path) as f:
        return json.load(f)

def compute_pnl(trades: pd.DataFrame) -> pd.DataFrame:
    """
    Match buys to subsequent sells to compute per-trade P&L.
    Returns DataFrame with columns: ticker, buy_date, sell_date,
    cost, proceeds, pnl_usd, pnl_pct.
    """
    if trades.empty:
        return pd.DataFrame()

    positions = {}
    results   = []

    for _, row in trades.iterrows():
        ticker = row["ticker"]
        if row["action"] == "buy":
            positions[ticker] = {
                "cost":     row["total_usd"],
                "date":     row["timestamp"],
                "qty":      row["quantity"],
            }
        elif row["action"] == "sell" and ticker in positions:
            pos       = positions.pop(ticker)
            pnl_usd   = row["total_usd"] - pos["cost"]
            pnl_pct   = pnl_usd / pos["cost"] if pos["cost"] else 0
            results.append({
                "ticker":    ticker,
                "buy_date":  pos["date"],
                "sell_date": row["timestamp"],
                "cost":      pos["cost"],
                "proceeds":  row["total_usd"],
                "pnl_usd":   round(pnl_usd, 2),
                "pnl_pct":   round(pnl_pct * 100, 2),
            })

    return pd.DataFrame(results) if results else pd.DataFrame()


# ══════════════════════════════════════════════════════════════════════════════
# SECTION 2: Chart builders
# ══════════════════════════════════════════════════════════════════════════════

def _no_data(ax, label: str):
    """Render a placeholder when data isn't available yet."""
    ax.text(0.5, 0.5, f"No data yet\n({label})",
            ha="center", va="center", color=TEXT_DIM,
            fontsize=10, transform=ax.transAxes)
    ax.set_xticks([])
    ax.set_yticks([])


def chart_equity(ax, history: pd.DataFrame, title="Portfolio equity"):
    ax.set_title(title, color=TEXT, pad=8)
    if history.empty:
        _no_data(ax, "needs trading history")
        return
    ax.plot(history["timestamp"], history["equity"],
            color=ACCENT, linewidth=1.5)
    ax.fill_between(history["timestamp"], history["equity"],
                    history["equity"].min() * 0.995,
                    alpha=0.15, color=ACCENT)
    ax.xaxis.set_major_formatter(mdates.DateFormatter("%m/%d"))
    ax.yaxis.set_major_formatter(plt.FuncFormatter(lambda x, _: f"${x:,.0f}"))
    ax.grid(True, axis="y")
    # Annotate latest value
    latest = history["equity"].iloc[-1]
    first  = history["equity"].iloc[0]
    delta  = latest - first
    color  = GREEN if delta >= 0 else RED
    ax.annotate(f"${latest:,.0f}  ({'+' if delta>=0 else ''}{delta:,.0f})",
                xy=(history["timestamp"].iloc[-1], latest),
                xytext=(-80, 10), textcoords="offset points",
                color=color, fontsize=8,
                arrowprops=dict(arrowstyle="->", color=color, lw=0.8))


def chart_pnl_per_trade(ax, pnl: pd.DataFrame, title="P&L per trade"):
    ax.set_title(title, color=TEXT, pad=8)
    if pnl.empty:
        _no_data(ax, "needs closed trades")
        return
    colors = [GREEN if v >= 0 else RED for v in pnl["pnl_usd"]]
    bars   = ax.bar(range(len(pnl)), pnl["pnl_usd"], color=colors, width=0.6)
    ax.axhline(0, color=GRAY, linewidth=0.8)
    ax.set_xticks(range(len(pnl)))
    ax.set_xticklabels(pnl["ticker"], rotation=45, ha="right", fontsize=8)
    ax.yaxis.set_major_formatter(plt.FuncFormatter(lambda x, _: f"${x:+.0f}"))
    ax.grid(True, axis="y")
    # Value labels on bars
    for bar, val in zip(bars, pnl["pnl_usd"]):
        ax.text(bar.get_x() + bar.get_width() / 2,
                bar.get_height() + (0.5 if val >= 0 else -1.5),
                f"${val:+.0f}", ha="center", fontsize=7,
                color=GREEN if val >= 0 else RED)


def chart_win_loss(ax, pnl: pd.DataFrame, title="Win / loss ratio"):
    ax.set_title(title, color=TEXT, pad=8)
    if pnl.empty:
        _no_data(ax, "needs closed trades")
        return
    wins   = (pnl["pnl_usd"] > 0).sum()
    losses = (pnl["pnl_usd"] <= 0).sum()
    if wins + losses == 0:
        _no_data(ax, "needs closed trades")
        return
    wedges, texts, autotexts = ax.pie(
        [wins, losses],
        labels=[f"Wins ({wins})", f"Losses ({losses})"],
        colors=[GREEN, RED],
        autopct="%1.0f%%",
        startangle=90,
        wedgeprops={"edgecolor": DARK_BG, "linewidth": 2},
    )
    for t in texts + autotexts:
        t.set_color(TEXT)
        t.set_fontsize(9)
    total_pnl = pnl["pnl_usd"].sum()
    ax.text(0, -1.4, f"Total P&L: ${total_pnl:+,.2f}",
            ha="center", color=GREEN if total_pnl >= 0 else RED, fontsize=9)


def chart_ticker_breakdown(ax, pnl: pd.DataFrame, title="Per-ticker P&L"):
    ax.set_title(title, color=TEXT, pad=8)
    if pnl.empty:
        _no_data(ax, "needs closed trades")
        return
    by_ticker = pnl.groupby("ticker")["pnl_usd"].sum().sort_values()
    colors    = [GREEN if v >= 0 else RED for v in by_ticker]
    by_ticker.plot(kind="barh", ax=ax, color=colors)
    ax.axvline(0, color=GRAY, linewidth=0.8)
    ax.xaxis.set_major_formatter(plt.FuncFormatter(lambda x, _: f"${x:+.0f}"))
    ax.grid(True, axis="x")


def chart_vs_spy(ax, history: pd.DataFrame, spy: pd.DataFrame, title="Portfolio vs SPY"):
    ax.set_title(title, color=TEXT, pad=8)
    if history.empty or spy.empty:
        _no_data(ax, "needs trading history")
        return

    # Normalize both to 100 at start
    port = history.copy()
    port = port.set_index("timestamp")["equity"]
    port = (port / port.iloc[0]) * 100

    spy_norm = spy.set_index("timestamp")["close"]
    spy_norm = (spy_norm / spy_norm.iloc[0]) * 100

    ax.plot(port.index, port.values, color=ACCENT,  linewidth=1.5, label="Portfolio")
    ax.plot(spy_norm.index, spy_norm.values, color=AMBER, linewidth=1.5,
            linestyle="--", label="SPY")
    ax.axhline(100, color=GRAY, linewidth=0.5, linestyle=":")
    ax.xaxis.set_major_formatter(mdates.DateFormatter("%m/%d"))
    ax.yaxis.set_major_formatter(plt.FuncFormatter(lambda x, _: f"{x:.0f}"))
    ax.legend(facecolor=PANEL_BG, edgecolor=GRAY, labelcolor=TEXT, fontsize=8)
    ax.grid(True, axis="y")


def chart_risk_mode(ax, params_history: list, title="Risk mode history"):
    ax.set_title(title, color=TEXT, pad=8)
    # Filter to auto_tuner and adaptive_risk changes only
    relevant = [
        h for h in params_history
        if h.get("source") in ("auto_tuner", "adaptive_risk")
        or h.get("key") in ("stop_loss_pct", "min_confidence", "max_position_usd")
    ]
    if not relevant:
        _no_data(ax, "needs auto-tune history")
        return

    # Plot stop_loss_pct over time as a proxy for risk mode
    stop_changes = [h for h in params_history if h.get("key") == "stop_loss_pct"]
    if not stop_changes:
        _no_data(ax, "needs parameter history")
        return

    dates  = [datetime.fromisoformat(h["timestamp"]) for h in stop_changes]
    values = [float(h["new_value"]) * 100 for h in stop_changes]

    ax.step(dates, values, color=AMBER, linewidth=1.5, where="post")
    ax.scatter(dates, values, color=AMBER, s=30, zorder=5)
    ax.set_ylabel("Stop-loss %", color=TEXT_DIM, fontsize=8)
    ax.xaxis.set_major_formatter(mdates.DateFormatter("%m/%d"))
    ax.yaxis.set_major_formatter(plt.FuncFormatter(lambda x, _: f"{x:.1f}%"))
    ax.grid(True, axis="y")
    for d, v, h in zip(dates, values, stop_changes):
        ax.annotate(f"{v:.1f}%", (d, v), textcoords="offset points",
                    xytext=(4, 4), fontsize=7, color=AMBER)


def chart_auto_tune(ax, tune_log: list, title="Auto-tuner changes"):
    ax.set_title(title, color=TEXT, pad=8)
    if not tune_log:
        _no_data(ax, "needs auto-tune history")
        return

    df = pd.DataFrame(tune_log)
    df["timestamp"] = pd.to_datetime(df["timestamp"])
    df["label"]     = df["param"] + "\n" + df["old_value"].astype(str) + "→" + df["new_value"].astype(str)

    ax.scatter(df["timestamp"], df["param"],
               color=ACCENT, s=60, zorder=5)
    for _, row in df.iterrows():
        ax.annotate(
            f"{row['old_value']}→{row['new_value']}",
            (row["timestamp"], row["param"]),
            textcoords="offset points",
            xytext=(6, 0), fontsize=7, color=TEXT_DIM,
        )
    ax.xaxis.set_major_formatter(mdates.DateFormatter("%m/%d"))
    ax.grid(True, axis="x")
    ax.set_yticks(df["param"].unique())
    ax.tick_params(axis="y", labelsize=8)


def chart_sector_rotation(ax, rotation_data: dict, title="Sector rotation"):
    ax.set_title(title, color=TEXT, pad=8)
    sectors = rotation_data.get("sectors", {})
    if not sectors:
        _no_data(ax, "runs Sunday 8pm ET")
        return

    names  = list(sectors.keys())
    confs  = [sectors[s]["conf_delta"] for s in names]
    colors = [GREEN if c > 0 else RED if c < 0 else GRAY for c in confs]

    bars = ax.barh(names, confs, color=colors, height=0.6)
    ax.axvline(0, color=GRAY, linewidth=0.8)
    ax.xaxis.set_major_formatter(plt.FuncFormatter(lambda x, _: f"{x:+.2f}"))
    ax.grid(True, axis="x")
    summary = rotation_data.get("summary", "")
    if summary:
        ax.set_xlabel(summary[:60], color=TEXT_DIM, fontsize=7)

    for bar, val, name in zip(bars, confs, names):
        sig = sectors[name]["signal"]
        label = "HOT" if sig == "hot" else "COLD" if sig == "cold" else ""
        if label:
            ax.text(val + (0.002 if val >= 0 else -0.002),
                    bar.get_y() + bar.get_height()/2,
                    label, va="center",
                    ha="left" if val >= 0 else "right",
                    fontsize=7,
                    color=GREEN if sig == "hot" else RED)


def chart_earnings_plays(ax, plays: list, title="Earnings plays"):
    ax.set_title(title, color=TEXT, pad=8)
    closed = [p for p in plays if p.get("status") == "closed" and p.get("pnl_pct") is not None]
    active = [p for p in plays if p.get("status") == "open"]

    if not closed and not active:
        _no_data(ax, "no earnings plays yet")
        return

    if closed:
        tickers = [p["ticker"] for p in closed]
        pnls    = [p["pnl_pct"] for p in closed]
        colors  = [GREEN if p >= 0 else RED for p in pnls]
        ax.bar(range(len(closed)), pnls, color=colors, width=0.6)
        ax.axhline(0, color=GRAY, linewidth=0.8)
        ax.set_xticks(range(len(closed)))
        ax.set_xticklabels(tickers, rotation=45, ha="right", fontsize=8)
        ax.yaxis.set_major_formatter(plt.FuncFormatter(lambda x, _: f"{x:+.1f}%"))
        ax.grid(True, axis="y")

    if active:
        ax.set_xlabel(
            f"Active: {', '.join(p['ticker'] for p in active)}",
            color=AMBER, fontsize=8
        )


def chart_watchlist_changes(ax, history: list, title="Watchlist changes"):
    ax.set_title(title, color=TEXT, pad=8)
    if not history:
        _no_data(ax, "no watchlist changes yet")
        return

    recent = history[-20:]
    dates  = [datetime.fromisoformat(h["timestamp"]) for h in recent]
    tickers = [h["ticker"] for h in recent]
    colors  = [GREEN if h["action"] == "add" else RED for h in recent]

    ax.scatter(dates, tickers, c=colors, s=60, zorder=5)
    ax.xaxis.set_major_formatter(mdates.DateFormatter("%m/%d"))
    ax.grid(True, axis="x")
    ax.tick_params(axis="y", labelsize=7)

    # Legend
    from matplotlib.patches import Patch
    ax.legend(
        handles=[Patch(color=GREEN, label="Added"), Patch(color=RED, label="Removed")],
        facecolor=PANEL_BG, edgecolor=GRAY, labelcolor=TEXT, fontsize=7,
        loc="upper left"
    )


# ══════════════════════════════════════════════════════════════════════════════
# SECTION 3: Main — compose all charts
# ══════════════════════════════════════════════════════════════════════════════

def generate_dashboard():
    print("\nGenerating performance dashboard...")

    # Load all data
    print("  Loading trade history...")
    trades  = load_trades()
    pnl     = compute_pnl(trades)

    print("  Loading account history from Alpaca...")
    history = load_account_history()

    print("  Loading SPY benchmark...")
    spy     = load_spy_returns()

    print("  Loading parameter history...")
    params_history   = load_params_history()
    tune_log         = load_auto_tune_log()

    print("  Loading earnings plays...")
    earnings_plays   = load_earnings_plays()

    print("  Loading sector rotation...")
    rotation_data    = load_sector_rotation()

    print("  Loading watchlist history...")
    watchlist_history = load_watchlist_history()

    # Summary stats
    total_trades   = len(trades)
    closed_trades  = len(pnl)
    wins           = (pnl["pnl_usd"] > 0).sum() if not pnl.empty else 0
    total_pnl      = pnl["pnl_usd"].sum() if not pnl.empty else 0
    win_rate       = f"{wins/closed_trades:.0%}" if closed_trades > 0 else "N/A"

    print(f"\n  Summary:")
    print(f"    Total trades executed: {total_trades}")
    print(f"    Closed positions:      {closed_trades}")
    print(f"    Win rate:              {win_rate}")
    print(f"    Total P&L:             ${total_pnl:+,.2f}")

    # Build figure — 5 rows, 2 cols
    fig = plt.figure(figsize=(16, 26), facecolor=DARK_BG)
    fig.suptitle(
        f"Trading Bot — Performance Dashboard\n"
        f"Generated {datetime.now().strftime('%Y-%m-%d %H:%M')}  |  "
        f"{total_trades} trades  |  Win rate: {win_rate}  |  P&L: ${total_pnl:+,.2f}",
        color=TEXT, fontsize=12, y=0.99
    )

    gs = gridspec.GridSpec(5, 2, figure=fig,
                           hspace=0.45, wspace=0.35,
                           top=0.97, bottom=0.03,
                           left=0.07, right=0.97)

    ax1 = fig.add_subplot(gs[0, :])    # full width — equity curve
    ax2 = fig.add_subplot(gs[1, :])    # full width — P&L per trade
    ax3 = fig.add_subplot(gs[2, 0])    # win/loss pie
    ax4 = fig.add_subplot(gs[2, 1])    # ticker breakdown
    ax5 = fig.add_subplot(gs[3, 0])    # vs SPY
    ax6 = fig.add_subplot(gs[3, 1])    # auto-tune
    ax7 = fig.add_subplot(gs[4, 0])    # sector rotation
    ax8 = fig.add_subplot(gs[4, 1])    # earnings plays / watchlist

    chart_equity(ax1, history)
    chart_pnl_per_trade(ax2, pnl)
    chart_win_loss(ax3, pnl)
    chart_ticker_breakdown(ax4, pnl)
    chart_vs_spy(ax5, history, spy)

    if tune_log:
        chart_auto_tune(ax6, tune_log)
    else:
        chart_risk_mode(ax6, params_history)

    chart_sector_rotation(ax7, rotation_data)

    if earnings_plays:
        chart_earnings_plays(ax8, earnings_plays)
    else:
        chart_watchlist_changes(ax8, watchlist_history)

    # Save
    os.makedirs(os.path.dirname(OUTPUT_PATH), exist_ok=True)
    plt.savefig(OUTPUT_PATH, dpi=150, bbox_inches="tight",
                facecolor=DARK_BG)
    plt.close()

    print(f"\n  Dashboard saved to: {OUTPUT_PATH}")
    print(f"  Open with: open {OUTPUT_PATH}")
    return OUTPUT_PATH


if __name__ == "__main__":
    path = generate_dashboard()
    # Auto-open on Mac
    if sys.platform == "darwin":
        os.system(f"open '{path}'")

"""
Web dashboard for the trading bot.
Runs as a Flask server on PORT (set by Railway) in a background thread.
Access it via your Railway service's public URL.
"""
import csv
import json
import logging
import os
import threading
import time
from datetime import datetime

from flask import Flask

from performance import get_stats
from config import TRAILING_STOP_PCT
import db

logger = logging.getLogger(__name__)

app = Flask(__name__)

# Shared references — set by start_dashboard()
_trader     = None
_live       = None
_start_time = time.time()

# ETF state — updated by bot.py after each ETF strategy run
_etf_universe: list = []
_etf_signals:  list = []

def update_etf_state(universe: list, signals: list) -> None:
    """Called by bot.py after each ETF run so the dashboard can display current ETF info."""
    global _etf_universe, _etf_signals
    _etf_universe = universe
    _etf_signals  = signals


def _account_data() -> dict:
    try:
        account = _trader.get_account()
        equity      = float(account.equity)
        cash        = float(account.cash)
        last_equity = float(account.last_equity)
        daily_pnl   = equity - last_equity
        daily_pct   = daily_pnl / last_equity * 100 if last_equity else 0
        return {
            "equity":    equity,
            "cash":      cash,
            "daily_pnl": daily_pnl,
            "daily_pct": daily_pct,
            "start":     2000.0,
            "total_pnl": equity - 2000.0,
            "total_pct": (equity - 2000.0) / 2000.0 * 100,
        }
    except Exception as e:
        return {"error": str(e)}


def _positions_data() -> list:
    try:
        positions = _trader.get_positions()
        rows = []
        for sym, pos in positions.items():
            entry  = float(pos.avg_entry_price)
            curr   = float(pos.current_price)
            qty    = float(pos.qty)
            pnl    = float(pos.unrealized_pl)
            pnl_pct = float(pos.unrealized_plpc) * 100
            rows.append({
                "symbol":    sym,
                "side":      str(pos.side).replace("PositionSide.", ""),
                "qty":       qty,
                "entry":     entry,
                "current":   curr,
                "pnl":       pnl,
                "pnl_pct":   pnl_pct,
                "value":     abs(qty * curr),
            })
        return sorted(rows, key=lambda x: x["pnl_pct"], reverse=True)
    except Exception as e:
        return [{"error": str(e)}]


def _recent_trades(n: int = 30) -> list:
    # Try DB first
    rows = db.get_trades(n)
    if rows:
        return rows
    # Fallback: CSV
    journal = "trade_journal.csv"
    if not os.path.exists(journal):
        return []
    try:
        with open(journal, newline="") as f:
            rows = list(csv.DictReader(f))
        return list(reversed(rows[-n:]))
    except Exception:
        return []


def _market_status() -> dict:
    """Return ETF market open/closed status and countdown (US Eastern time)."""
    from datetime import timezone, timedelta
    now_utc = datetime.now(timezone.utc)
    # EDT = UTC-4 (Mar–Nov), EST = UTC-5 (Nov–Mar). March 31 = EDT.
    # Simple rule: UTC offset is -4 from second Sunday of March to first Sunday of November
    month = now_utc.month
    et_offset = timedelta(hours=-4) if 3 <= month <= 10 else timedelta(hours=-5)
    now_et = now_utc + et_offset

    is_weekday   = now_et.weekday() < 5   # Mon=0 … Fri=4
    market_open  = now_et.replace(hour=9,  minute=30, second=0, microsecond=0)
    market_close = now_et.replace(hour=16, minute=0,  second=0, microsecond=0)
    is_open      = is_weekday and market_open <= now_et <= market_close

    def _fmt(td) -> str:
        total = int(td.total_seconds())
        h, r  = divmod(total, 3600)
        m, s  = divmod(r, 60)
        return f"{h}h {m}m" if h else f"{m}m {s}s"

    if is_open:
        return {"open": True,  "label": "OPEN",   "sub": f"closes in {_fmt(market_close - now_et)}",
                "et_time": now_et.strftime("%H:%M ET")}
    elif is_weekday and now_et < market_open:
        return {"open": False, "label": "CLOSED", "sub": f"opens in {_fmt(market_open - now_et)}",
                "et_time": now_et.strftime("%H:%M ET")}
    else:
        # Weekend — find next Monday open
        days_ahead = (7 - now_et.weekday()) % 7 or 7
        next_open  = (now_et + timedelta(days=days_ahead)).replace(
                         hour=9, minute=30, second=0, microsecond=0)
        return {"open": False, "label": "CLOSED", "sub": f"opens in {_fmt(next_open - now_et)} (Mon)",
                "et_time": now_et.strftime("%H:%M ET")}


def _bot_status() -> dict:
    uptime_s     = int(time.time() - _start_time)
    h, r         = divmod(uptime_s, 3600)
    m, s         = divmod(r, 60)
    refreshing   = _live._refreshing if _live else False
    last_refresh = _live._last_refresh if _live else 0.0
    active       = sorted(_live._active_symbols) if _live and _live._active_symbols else []
    entries      = dict(_live._entries) if _live else {}
    return {
        "uptime":         f"{h}h {m}m {s}s",
        "active_symbols": active,
        "open_entries":   entries,
        "refreshing":     refreshing,
        "last_refresh":   (
            datetime.fromtimestamp(last_refresh).strftime("%H:%M:%S")
            if last_refresh > 0 else ("Loading..." if refreshing else "Pending first refresh")
        ),
        "daily_halt":     _live._daily_halt if _live else False,
    }


def _color(val: float, positive="green", negative="red") -> str:
    return positive if val >= 0 else negative


def _pnl_badge(pct: float) -> str:
    color = "#2ecc71" if pct >= 0 else "#e74c3c"
    sign  = "+" if pct >= 0 else ""
    return f'<span style="color:{color};font-weight:600">{sign}{pct:.2f}%</span>'


@app.route("/")
def index():
    account  = _account_data()
    positions = _positions_data()
    trades   = _recent_trades()
    stats    = get_stats()
    status   = _bot_status()

    # ---- Build HTML ----
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    # Account cards
    if "error" in account:
        account_html = f'<p style="color:#e74c3c">Alpaca error: {account["error"]}</p>'
    else:
        def card(label, value, sub=""):
            return f"""
            <div class="card">
                <div class="card-label">{label}</div>
                <div class="card-value">{value}</div>
                {"<div class='card-sub'>" + sub + "</div>" if sub else ""}
            </div>"""

        daily_color = "#2ecc71" if account["daily_pnl"] >= 0 else "#e74c3c"
        total_color = "#2ecc71" if account["total_pnl"] >= 0 else "#e74c3c"
        account_html = f"""
        <div class="cards">
            {card("Portfolio Value", f"${account['equity']:,.2f}")}
            {card("Cash", f"${account['cash']:,.2f}")}
            {card("Daily P&L",
                  f'<span style="color:{daily_color}">${account["daily_pnl"]:+,.2f}</span>',
                  f'<span style="color:{daily_color}">{account["daily_pct"]:+.2f}%</span>')}
            {card("Total P&L",
                  f'<span style="color:{total_color}">${account["total_pnl"]:+,.2f}</span>',
                  f'<span style="color:{total_color}">{account["total_pct"]:+.2f}% from $2,000')}
        </div>"""

    # Positions table
    if not positions:
        pos_html = '<p style="color:#888">No open positions</p>'
    elif "error" in positions[0]:
        pos_html = f'<p style="color:#e74c3c">{positions[0]["error"]}</p>'
    else:
        rows = ""
        for p in positions:
            color = "#2ecc71" if p["pnl_pct"] >= 0 else "#e74c3c"
            rows += f"""<tr>
                <td><b>{p["symbol"]}</b></td>
                <td style="color:#aaa">{p["side"]}</td>
                <td>{p["qty"]}</td>
                <td>${p["entry"]:.4f}</td>
                <td>${p["current"]:.4f}</td>
                <td style="color:{color}">${p["pnl"]:+.2f}</td>
                <td style="color:{color}">{_pnl_badge(p["pnl_pct"])}</td>
                <td>${p["value"]:,.2f}</td>
            </tr>"""
        pos_html = f"""
        <table>
            <thead><tr>
                <th>Symbol</th><th>Side</th><th>Qty</th>
                <th>Entry</th><th>Current</th><th>P&L $</th><th>P&L %</th><th>Value</th>
            </tr></thead>
            <tbody>{rows}</tbody>
        </table>"""

    # Performance stats
    if "error" in stats:
        perf_html = f'<p style="color:#888">{stats["error"]}</p>'
    elif stats.get("completed", 0) == 0:
        perf_html = f'<p style="color:#888">{stats.get("message", "No closed trades yet")} ({stats.get("total_entries",0)} entries opened)</p>'
    else:
        def stat_row(label, value, target="", good=True):
            icon = "✅" if good else "⚠️"
            return f"""<tr>
                <td>{label}</td>
                <td><b>{value}</b></td>
                <td>{icon + " " + target if target else ""}</td>
            </tr>"""

        wr_good = stats["win_rate"] >= 45
        pf_good = stats["profit_factor"] >= 1.5
        perf_html = f"""
        <table>
            <thead><tr><th>Metric</th><th>Value</th><th>Target</th></tr></thead>
            <tbody>
                {stat_row("Entries opened", stats["total_entries"])}
                {stat_row("Closed trades", f"{stats['completed']} ({stats['wins']}W / {stats['losses']}L)")}
                {stat_row("Win rate", f"{stats['win_rate']}%", "≥ 45%", wr_good)}
                {stat_row("Avg win", f"+{stats['avg_win_pct']}%")}
                {stat_row("Avg loss", f"{stats['avg_loss_pct']}%")}
                {stat_row("Profit factor", stats['profit_factor'], "≥ 1.5", pf_good)}
                {stat_row("Total closed P&L", f"{stats['total_pnl_pct']:+.2f}%")}
            </tbody>
        </table>"""

    # Recent trades table
    if not trades:
        trades_html = '<p style="color:#888">No trades logged yet</p>'
    else:
        rows = ""
        for t in trades:
            action = t.get("action", "")
            color  = {"BUY": "#2ecc71", "SELL": "#e74c3c", "SHORT": "#e67e22",
                      "COVER": "#3498db", "CLOSE": "#9b59b6"}.get(action, "#aaa")
            rows += f"""<tr>
                <td style="color:#888;font-size:12px">{t.get("timestamp","")}</td>
                <td style="color:{color};font-weight:600">{action}</td>
                <td><b>{t.get("symbol","")}</b></td>
                <td>{t.get("qty","")}</td>
                <td>${float(t["price"]):.4f}</td>
                <td style="color:#aaa;font-size:12px">{t.get("note","")[:60]}</td>
            </tr>"""
        trades_html = f"""
        <table>
            <thead><tr>
                <th>Time</th><th>Action</th><th>Symbol</th><th>Qty</th><th>Price</th><th>Note</th>
            </tr></thead>
            <tbody>{rows}</tbody>
        </table>"""

    # Bot + market status
    halt_badge  = '<span style="color:#e74c3c;font-weight:600">⛔ HALTED</span>' if status["daily_halt"] else '<span style="color:#2ecc71">✅ ACTIVE</span>'
    active_syms = ", ".join(status["active_symbols"]) or (
        '<span style="color:#f39c12">⏳ Loading data...</span>' if status["refreshing"]
        else '<span style="color:#888">Pending first refresh</span>'
    )
    mkt         = _market_status()
    mkt_color   = "#2ecc71" if mkt["open"] else "#e74c3c"
    mkt_badge   = f'<span style="color:{mkt_color};font-weight:600">{mkt["label"]}</span>'

    # Active ETF universe
    if _etf_signals:
        etf_rows = ""
        for sig in sorted(_etf_signals, key=lambda x: x["score"], reverse=True):
            sc    = sig["score"]
            sc_color = "#2ecc71" if sc > 0 else ("#e74c3c" if sc < 0 else "#888")
            sig_color = {"BUY": "#2ecc71", "SELL": "#e74c3c", "HOLD": "#888"}.get(sig["signal"], "#888")
            etf_rows += f"""<tr>
                <td><b>{sig["ticker"]}</b></td>
                <td style="color:{sc_color}">{sc:+d}</td>
                <td style="color:{sig_color};font-weight:600">{sig["signal"]}</td>
                <td>${sig["price"]:.2f}</td>
                <td style="color:#888;font-size:12px">{sig.get("regime","")}</td>
            </tr>"""
        etf_html = f"""
        <table>
            <thead><tr><th>Ticker</th><th>Score</th><th>Signal</th><th>Price</th><th>Regime</th></tr></thead>
            <tbody>{etf_rows}</tbody>
        </table>"""
    else:
        etf_html = '<p style="color:#888">No ETF data yet — market may be closed or first run pending</p>'

    # Open entries (manual stops/trails)
    if status["open_entries"]:
        entry_rows = ""
        for sym, e in status["open_entries"].items():
            entry_price  = e["price"]
            peak_price   = e.get("peak_price", entry_price)
            initial_stop = e.get("stop", 0)
            trail_stop   = peak_price * (1 - TRAILING_STOP_PCT)
            eff_stop     = max(initial_stop, trail_stop)
            peak_pct     = (peak_price - entry_price) / entry_price * 100
            stop_color   = "#2ecc71" if eff_stop > initial_stop else "#e67e22"
            trail_label  = "✅ trailing" if eff_stop > initial_stop else "⚠️ initial"
            pyramided    = "✅" if e.get("pyramided") else "—"
            entry_rows += f"""<tr>
                <td><b>{sym}</b></td>
                <td>${entry_price:.4f}</td>
                <td style="color:{stop_color}">${eff_stop:.4f} <small>({trail_label})</small></td>
                <td>${peak_price:.4f} <small style="color:#2ecc71">({peak_pct:+.1f}%)</small></td>
                <td>{pyramided}</td>
            </tr>"""
        entries_html = f"""
        <table>
            <thead><tr>
                <th>Symbol</th><th>Entry</th><th>Effective Stop</th><th>Peak</th><th>Pyramided</th>
            </tr></thead>
            <tbody>{entry_rows}</tbody>
        </table>"""
    else:
        entries_html = '<p style="color:#888">No tracked crypto entries</p>'

    html = f"""<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta http-equiv="refresh" content="60">
    <meta name="viewport" content="width=device-width, initial-scale=1">
    <title>Trading Bot Dashboard</title>
    <style>
        * {{ box-sizing: border-box; margin: 0; padding: 0; }}
        body {{
            background: #0d1117; color: #e6edf3;
            font-family: 'Courier New', monospace; font-size: 14px;
            padding: 20px;
        }}
        h1 {{ color: #58a6ff; font-size: 22px; margin-bottom: 4px; }}
        h2 {{ color: #8b949e; font-size: 13px; font-weight: normal;
              text-transform: uppercase; letter-spacing: 1px;
              margin: 28px 0 10px; border-bottom: 1px solid #21262d; padding-bottom: 6px; }}
        .header {{ display: flex; justify-content: space-between; align-items: center;
                   margin-bottom: 24px; }}
        .refresh {{ color: #8b949e; font-size: 12px; }}
        .cards {{ display: flex; gap: 16px; flex-wrap: wrap; margin-bottom: 8px; }}
        .card {{
            background: #161b22; border: 1px solid #21262d;
            border-radius: 8px; padding: 16px 20px; min-width: 180px; flex: 1;
        }}
        .card-label {{ color: #8b949e; font-size: 12px; text-transform: uppercase;
                       letter-spacing: 1px; margin-bottom: 6px; }}
        .card-value {{ font-size: 22px; font-weight: 700; color: #e6edf3; }}
        .card-sub {{ color: #8b949e; font-size: 13px; margin-top: 4px; }}
        table {{
            width: 100%; border-collapse: collapse;
            background: #161b22; border-radius: 8px; overflow: hidden;
        }}
        th {{
            background: #21262d; color: #8b949e; font-size: 11px;
            text-transform: uppercase; letter-spacing: 1px;
            padding: 10px 12px; text-align: left;
        }}
        td {{ padding: 9px 12px; border-bottom: 1px solid #21262d; }}
        tr:last-child td {{ border-bottom: none; }}
        tr:hover td {{ background: #1c2128; }}
        .status-row {{ display: flex; gap: 24px; flex-wrap: wrap;
                       background: #161b22; border: 1px solid #21262d;
                       border-radius: 8px; padding: 14px 18px; }}
        .status-item {{ display: flex; flex-direction: column; gap: 4px; }}
        .status-label {{ color: #8b949e; font-size: 11px; text-transform: uppercase; }}
        .status-value {{ color: #e6edf3; }}
        .active-syms {{ color: #58a6ff; font-size: 13px; }}
    </style>
</head>
<body>
    <div class="header">
        <div>
            <h1>⚡ Trading Bot Dashboard</h1>
            <div style="color:#8b949e;font-size:12px">Paper Trading · $2,000 Starting Capital</div>
        </div>
        <div class="refresh">Auto-refresh: 60s · Last updated: {now}</div>
    </div>

    <h2>Portfolio Overview</h2>
    {account_html}

    <h2>Bot Status</h2>
    <div class="status-row">
        <div class="status-item">
            <div class="status-label">Bot</div>
            <div class="status-value">{halt_badge}</div>
        </div>
        <div class="status-item">
            <div class="status-label">Uptime</div>
            <div class="status-value">{status["uptime"]}</div>
        </div>
        <div class="status-item">
            <div class="status-label">ETF Market ({mkt["et_time"]})</div>
            <div class="status-value">{mkt_badge}</div>
            <div class="card-sub" style="color:#8b949e">{mkt["sub"]}</div>
        </div>
        <div class="status-item">
            <div class="status-label">Last Data Refresh</div>
            <div class="status-value">{status["last_refresh"]}</div>
        </div>
        <div class="status-item">
            <div class="status-label">Active Crypto</div>
            <div class="active-syms">{active_syms}</div>
        </div>
    </div>

    <h2>Open Positions ({len([p for p in positions if "error" not in p])})</h2>
    {pos_html}

    <h2>Crypto Entry Tracking (Trailing Stops)</h2>
    {entries_html}

    <h2>ETF Signals (last run)</h2>
    {etf_html}

    <h2>Performance Stats</h2>
    {perf_html}

    <h2>Recent Trades (last 30)</h2>
    {trades_html}
</body>
</html>"""

    return html


def start_dashboard(trader_instance, live_instance) -> None:
    """Start the Flask dashboard in a background daemon thread."""
    global _trader, _live
    _trader = trader_instance
    _live   = live_instance

    port = int(os.environ.get("PORT", 5000))

    def run():
        # Silence Flask's default request logs to keep Railway logs clean
        log = logging.getLogger("werkzeug")
        log.setLevel(logging.ERROR)
        app.run(host="0.0.0.0", port=port, debug=False, use_reloader=False)

    t = threading.Thread(target=run, daemon=True, name="Dashboard")
    t.start()
    logger.info(f"[DASHBOARD] Web dashboard running on port {port}")

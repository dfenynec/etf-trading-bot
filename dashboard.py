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
from collections import deque
from datetime import datetime

from flask import Flask

from performance import get_stats
from config import (
    TRAILING_STOP_PCT, RISK_PER_TRADE_PCT, STOP_LOSS_MAX_PCT, TAKE_PROFIT_MAX_PCT,
    DAILY_LOSS_LIMIT_PCT, MAX_POSITIONS, MAX_SHORT_POSITIONS, MAX_CRYPTO_POSITIONS,
    MIN_BUY_SCORE, MIN_SELL_SCORE, SCREEN_TOP_N_ETF, SCREEN_TOP_N_CRYPTO,
    RUN_INTERVAL_MINUTES, BTC_CORRELATION_FILTER,
    PYRAMID_TRIGGER_PCT, PYRAMID_ADD_PCT,
    KELLY_MIN_TRADES, KELLY_MAX_RISK, KELLY_FRACTION,
)
from screener import ETF_CANDIDATES, CRYPTO_CANDIDATES
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

# In-memory log ring buffer — last 200 lines captured from root logger
_log_buffer: deque = deque(maxlen=200)

LEVEL_COLOR = {
    "DEBUG":    "#555",
    "INFO":     "#8b949e",
    "WARNING":  "#e67e22",
    "ERROR":    "#e74c3c",
    "CRITICAL": "#ff0000",
}
LEVEL_HIGHLIGHT = {"WARNING", "ERROR", "CRITICAL"}


class _MemoryLogHandler(logging.Handler):
    """Appends every log record to the shared _log_buffer deque."""
    def emit(self, record):
        try:
            _log_buffer.append({
                "time":  self.formatTime(record, "%H:%M:%S"),
                "level": record.levelname,
                "msg":   record.getMessage(),
            })
        except Exception:
            pass


def update_etf_state(universe: list, signals: list) -> None:
    """Called by bot.py after each ETF run so the dashboard can display current ETF info."""
    global _etf_universe, _etf_signals
    _etf_universe = universe
    _etf_signals  = signals
    logger.info(f"[DASHBOARD] ETF state updated: {len(signals)} signals for {len(universe)} tickers")


# ---------------------------------------------------------------------------
# Data helpers
# ---------------------------------------------------------------------------

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
            entry   = float(pos.avg_entry_price)
            curr    = float(pos.current_price)
            qty     = float(pos.qty)
            pnl     = float(pos.unrealized_pl)
            pnl_pct = float(pos.unrealized_plpc) * 100
            rows.append({
                "symbol":  sym,
                "side":    str(pos.side).replace("PositionSide.", ""),
                "qty":     qty,
                "entry":   entry,
                "current": curr,
                "pnl":     pnl,
                "pnl_pct": pnl_pct,
                "value":   abs(qty * curr),
            })
        return sorted(rows, key=lambda x: x["pnl_pct"], reverse=True)
    except Exception as e:
        return [{"error": str(e)}]


def _all_trades(n: int = 200) -> list:
    rows = db.get_trades(n)
    if rows:
        return rows
    journal = "trade_journal.csv"
    if not os.path.exists(journal):
        return []
    try:
        with open(journal, newline="") as f:
            rows = list(csv.DictReader(f))
        return list(reversed(rows[-n:]))
    except Exception:
        return []


def _closed_trades(n: int = 10) -> list:
    """Return the last N closing events (CLOSE / SELL / COVER)."""
    all_t  = _all_trades(200)
    closed = [t for t in all_t if t.get("action", "") in ("CLOSE", "SELL", "COVER")]
    return closed[:n]


def _market_status() -> dict:
    """Return ETF market open/closed status and countdown (US Eastern time)."""
    from datetime import timezone, timedelta
    now_utc = datetime.now(timezone.utc)
    month   = now_utc.month
    et_offset = timedelta(hours=-4) if 3 <= month <= 10 else timedelta(hours=-5)
    now_et    = now_utc + et_offset

    is_weekday   = now_et.weekday() < 5
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
        "daily_halt": _live._daily_halt if _live else False,
    }


def _config_info() -> dict:
    from live_trader import TRADE_COOLDOWN, FULL_REFRESH_INTERVAL
    kelly_risk = None
    try:
        from performance import kelly_risk_pct
        kelly_risk = kelly_risk_pct()
    except Exception:
        pass
    return {
        "risk_per_trade":   f"{RISK_PER_TRADE_PCT*100:.1f}%",
        "kelly_risk":       f"{kelly_risk*100:.2f}% (active)" if kelly_risk and kelly_risk != RISK_PER_TRADE_PCT else f"{RISK_PER_TRADE_PCT*100:.1f}% (base — Kelly pending {KELLY_MIN_TRADES} trades)",
        "max_stop":         f"{STOP_LOSS_MAX_PCT*100:.0f}%",
        "max_tp":           f"{TAKE_PROFIT_MAX_PCT*100:.0f}%",
        "trailing_stop":    f"{TRAILING_STOP_PCT*100:.0f}%",
        "daily_loss_limit": f"{DAILY_LOSS_LIMIT_PCT*100:.0f}%",
        "max_long_etf":     MAX_POSITIONS,
        "max_short_etf":    MAX_SHORT_POSITIONS,
        "max_crypto":       MAX_CRYPTO_POSITIONS,
        "buy_threshold":    f"≥ +{MIN_BUY_SCORE}",
        "sell_threshold":   f"≤ {MIN_SELL_SCORE}",
        "etf_pool":         f"{len(ETF_CANDIDATES)} candidates → top {SCREEN_TOP_N_ETF} active",
        "crypto_pool":      f"{len(CRYPTO_CANDIDATES)} candidates → top {SCREEN_TOP_N_CRYPTO} active",
        "etf_interval":     f"every {RUN_INTERVAL_MINUTES} min (market + ext hours)",
        "crypto_cooldown":  f"{TRADE_COOLDOWN}s between trades per symbol",
        "data_refresh":     f"every {FULL_REFRESH_INTERVAL//60} min",
        "btc_filter":       "ON" if BTC_CORRELATION_FILTER else "OFF",
        "pyramid":          f"ON — adds {int(PYRAMID_ADD_PCT*100)}% at +{int(PYRAMID_TRIGGER_PCT*100)}%",
        "kelly_fraction":   f"{int(KELLY_FRACTION*100)}% Kelly (activates after {KELLY_MIN_TRADES} trades, max {KELLY_MAX_RISK*100:.0f}%)",
    }


def _pnl_badge(pct: float, show_sign=True) -> str:
    color = "#2ecc71" if pct >= 0 else "#e74c3c"
    sign  = "+" if pct >= 0 and show_sign else ""
    return f'<span style="color:{color};font-weight:600">{sign}{pct:.2f}%</span>'


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------

@app.route("/")
def index():
    try:
        return _render_dashboard()
    except Exception as e:
        logger.error(f"[DASHBOARD] Render error: {e}", exc_info=True)
        return f"<pre style='color:#e74c3c;background:#0d1117;padding:20px'>Dashboard render error:\n{e}</pre>", 500


def _render_dashboard():
    account   = _account_data()
    positions = _positions_data()
    closed    = _closed_trades(10)
    stats     = get_stats()
    status    = _bot_status()
    cfg       = _config_info()
    now       = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    mkt       = _market_status()

    # ── Header pills ──────────────────────────────────────────────────────────
    if "error" not in account:
        eq_pill     = f'<span class="pill">${account["equity"]:,.2f}</span>'
        dpnl_color  = "#2ecc71" if account["daily_pnl"] >= 0 else "#e74c3c"
        tpnl_color  = "#2ecc71" if account["total_pnl"] >= 0 else "#e74c3c"
        dpnl_sign   = "+" if account["daily_pnl"] >= 0 else ""
        tpnl_sign   = "+" if account["total_pnl"] >= 0 else ""
        daily_pill  = (f'<span class="pill" style="color:{dpnl_color}">'
                       f'Day {dpnl_sign}${account["daily_pnl"]:,.2f} ({dpnl_sign}{account["daily_pct"]:.2f}%)</span>')
        total_pill  = (f'<span class="pill" style="color:{tpnl_color}">'
                       f'Total {tpnl_sign}${account["total_pnl"]:,.2f} ({tpnl_sign}{account["total_pct"]:.2f}%)</span>')
    else:
        eq_pill = daily_pill = total_pill = '<span class="pill" style="color:#e74c3c">API error</span>'

    # W/L pill from stats
    if stats.get("completed", 0) > 0:
        wl_color = "#2ecc71" if stats["win_rate"] >= 45 else "#e74c3c"
        wl_pill  = (f'<span class="pill" style="color:{wl_color}">'
                    f'W/L {stats["wins"]}/{stats["losses"]} ({stats["win_rate"]}%)</span>')
    else:
        wl_pill = '<span class="pill" style="color:#888">W/L —</span>'

    halt_pill = ('<span class="pill" style="color:#e74c3c">⛔ HALTED</span>'
                 if status["daily_halt"] else
                 '<span class="pill" style="color:#2ecc71">✅ BOT ACTIVE</span>')
    mkt_color = "#2ecc71" if mkt["open"] else "#e74c3c"
    mkt_pill  = (f'<span class="pill" style="color:{mkt_color}">'
                 f'ETF {mkt["label"]} · {mkt["sub"]}</span>')

    # ── Open Positions ────────────────────────────────────────────────────────
    open_count = len([p for p in positions if "error" not in p])
    if not positions or "error" in positions[0]:
        pos_html = '<p class="empty">No open positions</p>'
    else:
        rows = ""
        for p in positions:
            c = "#2ecc71" if p["pnl_pct"] >= 0 else "#e74c3c"
            rows += f"""<tr>
                <td><b>{p["symbol"]}</b></td>
                <td style="color:#aaa">{p["side"]}</td>
                <td>{p["qty"]}</td>
                <td>${p["entry"]:.4f}</td>
                <td>${p["current"]:.4f}</td>
                <td style="color:{c}">${p["pnl"]:+.2f}</td>
                <td>{_pnl_badge(p["pnl_pct"])}</td>
                <td>${p["value"]:,.2f}</td>
            </tr>"""
        pos_html = f"""<table>
            <thead><tr>
                <th>Symbol</th><th>Side</th><th>Qty</th>
                <th>Entry</th><th>Current</th><th>P&L $</th><th>P&L %</th><th>Value</th>
            </tr></thead><tbody>{rows}</tbody></table>"""

    # Crypto entry tracking (trailing stops)
    if status["open_entries"]:
        entry_rows = ""
        for sym, e in status["open_entries"].items():
            side         = e.get("side", "long")
            entry_price  = e["price"]
            initial_stop = e.get("stop", 0)
            if side == "short":
                trough      = e.get("trough_price", entry_price)
                trail_stop  = trough * (1 + TRAILING_STOP_PCT)
                eff_stop    = min(initial_stop, trail_stop)
                peak_lbl    = f"${trough:.4f}"
                stop_active = trail_stop < initial_stop
            else:
                peak_price  = e.get("peak_price", entry_price)
                trail_stop  = peak_price * (1 - TRAILING_STOP_PCT)
                eff_stop    = max(initial_stop, trail_stop)
                gain_pct    = (peak_price - entry_price) / entry_price * 100
                peak_lbl    = f"${peak_price:.4f} <small style='color:#2ecc71'>({gain_pct:+.1f}%)</small>"
                stop_active = trail_stop > initial_stop
            stop_color  = "#2ecc71" if stop_active else "#e67e22"
            trail_label = "✅ trailing" if stop_active else "⚠️ initial"
            side_badge  = f'<span style="color:{"#e74c3c" if side=="short" else "#2ecc71"}">{side}</span>'
            entry_rows += f"""<tr>
                <td><b>{sym}</b></td>
                <td>{side_badge}</td>
                <td>${entry_price:.4f}</td>
                <td style="color:{stop_color}">${eff_stop:.4f} <small>({trail_label})</small></td>
                <td>{peak_lbl}</td>
                <td>{"✅" if e.get("pyramided") else "—"}</td>
            </tr>"""
        entries_html = f"""<table>
            <thead><tr>
                <th>Symbol</th><th>Side</th><th>Entry</th><th>Eff. Stop</th><th>Peak / Trough</th><th>Pyramided</th>
            </tr></thead><tbody>{entry_rows}</tbody></table>"""
    else:
        entries_html = '<p class="empty">No tracked crypto entries</p>'

    # ── Last 10 Closed Trades ─────────────────────────────────────────────────
    if not closed:
        closed_html = '<p class="empty">No closed trades yet</p>'
    else:
        rows = ""
        for t in closed:
            action = t.get("action", "")
            color  = {"SELL": "#e74c3c", "COVER": "#3498db", "CLOSE": "#9b59b6"}.get(action, "#aaa")
            try:
                price_fmt = f'${float(t["price"]):.4f}'
            except Exception:
                price_fmt = t.get("price", "")
            note = t.get("note", "")
            pnl_html = ""
            if "pnl=" in note:
                try:
                    pnl_str = note.split("pnl=")[1].split("%")[0] + "%"
                    pnl_val = float(pnl_str.replace("%", ""))
                    pnl_html = _pnl_badge(pnl_val)
                except Exception:
                    pnl_html = f'<span style="color:#888">{note[:40]}</span>'
            else:
                pnl_html = f'<span style="color:#888;font-size:12px">{note[:40]}</span>'
            rows += f"""<tr>
                <td style="color:#888;font-size:12px">{t.get("timestamp","")}</td>
                <td style="color:{color};font-weight:600">{action}</td>
                <td><b>{t.get("symbol","")}</b></td>
                <td>{price_fmt}</td>
                <td>{pnl_html}</td>
            </tr>"""
        closed_html = f"""<table>
            <thead><tr><th>Time</th><th>Action</th><th>Symbol</th><th>Price</th><th>P&L / Note</th></tr></thead>
            <tbody>{rows}</tbody></table>"""

    # ── Live Logs ─────────────────────────────────────────────────────────────
    log_lines = list(_log_buffer)[-100:]   # last 100 lines, newest at bottom
    if log_lines:
        log_items = ""
        for line in log_lines:
            lvl   = line["level"]
            color = LEVEL_COLOR.get(lvl, "#8b949e")
            bold  = "font-weight:600;" if lvl in LEVEL_HIGHLIGHT else ""
            bg    = "background:#1a0a0a;" if lvl == "ERROR" else (
                    "background:#1a1200;" if lvl == "WARNING" else "")
            log_items += (
                f'<div style="padding:2px 0;{bg}">'
                f'<span style="color:#555;user-select:none">{line["time"]} </span>'
                f'<span style="color:{color};{bold}min-width:60px;display:inline-block">[{lvl}]</span> '
                f'<span style="color:#cdd9e5">{line["msg"]}</span>'
                f'</div>'
            )
        logs_html = f"""
        <div style="background:#0d1117;border:1px solid #21262d;border-radius:8px;
                    padding:14px;font-family:\'Courier New\',monospace;font-size:12px;
                    max-height:500px;overflow-y:auto;line-height:1.5" id="logbox">
            {log_items}
        </div>
        <script>
            var lb = document.getElementById("logbox");
            if(lb) lb.scrollTop = lb.scrollHeight;
        </script>"""
    else:
        logs_html = '<p class="empty">No logs captured yet</p>'

    # ── ETF Signals ───────────────────────────────────────────────────────────
    if _etf_signals:
        etf_rows = ""
        for sig in sorted(_etf_signals, key=lambda x: x["score"], reverse=True):
            sc       = sig["score"]
            sc_color = "#2ecc71" if sc > 0 else ("#e74c3c" if sc < 0 else "#888")
            sg_color = {"BUY": "#2ecc71", "SELL": "#e74c3c", "HOLD": "#888"}.get(sig["signal"], "#888")
            etf_rows += f"""<tr>
                <td><b>{sig["ticker"]}</b></td>
                <td style="color:{sc_color}">{int(sc):+d}</td>
                <td style="color:{sg_color};font-weight:600">{sig["signal"]}</td>
                <td>${sig["price"]:.2f}</td>
                <td style="color:#888;font-size:12px">{sig.get("regime","")}</td>
            </tr>"""
        etf_html = f"""<table>
            <thead><tr><th>Ticker</th><th>Score</th><th>Signal</th><th>Price</th><th>Regime</th></tr></thead>
            <tbody>{etf_rows}</tbody></table>"""
    else:
        etf_html = '<p class="empty">No ETF data yet — market may be closed or first run pending</p>'

    # ── Performance Stats ─────────────────────────────────────────────────────
    if "error" in stats:
        perf_html = f'<p class="empty">{stats["error"]}</p>'
    elif stats.get("completed", 0) == 0:
        perf_html = f'<p class="empty">{stats.get("message","No closed trades yet")} ({stats.get("total_entries",0)} entries opened)</p>'
    else:
        def stat_row(label, value, target="", good=True):
            icon = "✅" if good else "⚠️"
            return f"<tr><td>{label}</td><td><b>{value}</b></td><td>{icon+' '+target if target else ''}</td></tr>"
        perf_html = f"""<table>
            <thead><tr><th>Metric</th><th>Value</th><th>Target</th></tr></thead><tbody>
                {stat_row("Entries opened",  stats["total_entries"])}
                {stat_row("Closed trades",   f"{stats['completed']} ({stats['wins']}W / {stats['losses']}L)")}
                {stat_row("Win rate",        f"{stats['win_rate']}%", "≥ 45%", stats["win_rate"] >= 45)}
                {stat_row("Avg win",         f"+{stats['avg_win_pct']}%")}
                {stat_row("Avg loss",        f"{stats['avg_loss_pct']}%")}
                {stat_row("Profit factor",   stats["profit_factor"], "≥ 1.5", stats["profit_factor"] >= 1.5)}
                {stat_row("Total closed P&L",f"{stats['total_pnl_pct']:+.2f}%")}
            </tbody></table>"""

    # ── Bot Configuration ─────────────────────────────────────────────────────
    def cfg_tbl(*rows_data):
        rows = "".join(
            f'<tr><td style="color:#8b949e;padding:3px 12px 3px 0">{k}</td>'
            f'<td style="color:{c}">{v}</td></tr>'
            for k, v, c in rows_data
        )
        return f'<table style="background:transparent;font-size:13px">{rows}</table>'

    cfg_html = f"""
    <div style="display:grid;grid-template-columns:repeat(auto-fill,minmax(300px,1fr));gap:10px">
        <div class="cfg-card">
            <div class="cfg-title">⚖️ Risk Management</div>
            {cfg_tbl(
                ("Risk/trade",    cfg["risk_per_trade"],   "#e6edf3"),
                ("Kelly sizing",  cfg["kelly_risk"],       "#58a6ff"),
                ("Max stop-loss", cfg["max_stop"],         "#e74c3c"),
                ("Max take-profit",cfg["max_tp"],          "#2ecc71"),
                ("Trailing stop", cfg["trailing_stop"],    "#e6edf3"),
                ("Daily halt",    cfg["daily_loss_limit"], "#e74c3c"),
            )}
        </div>
        <div class="cfg-card">
            <div class="cfg-title">📊 Positions &amp; Signals</div>
            {cfg_tbl(
                ("Max ETF longs",  str(cfg["max_long_etf"]),  "#e6edf3"),
                ("Max ETF shorts", str(cfg["max_short_etf"]), "#e6edf3"),
                ("Max crypto",     str(cfg["max_crypto"]),    "#e6edf3"),
                ("Buy threshold",  cfg["buy_threshold"],      "#2ecc71"),
                ("Sell threshold", cfg["sell_threshold"],     "#e74c3c"),
            )}
        </div>
        <div class="cfg-card">
            <div class="cfg-title">🌐 Universe &amp; Timing</div>
            {cfg_tbl(
                ("ETF pool",       cfg["etf_pool"],       "#e6edf3"),
                ("Crypto pool",    cfg["crypto_pool"],    "#e6edf3"),
                ("ETF runs",       cfg["etf_interval"],   "#e6edf3"),
                ("Crypto cooldown",cfg["crypto_cooldown"],"#e6edf3"),
                ("Data refresh",   cfg["data_refresh"],   "#e6edf3"),
            )}
        </div>
        <div class="cfg-card">
            <div class="cfg-title">🤖 Features</div>
            {cfg_tbl(
                ("BTC filter",    cfg["btc_filter"],       "#58a6ff"),
                ("Pyramiding",    cfg["pyramid"],          "#2ecc71"),
                ("Kelly",         cfg["kelly_fraction"],   "#e6edf3"),
                ("ETF shorts",    "ON (US equities only)", "#2ecc71"),
                ("Ext hours",     "ON 4AM–9:30AM/4PM–8PM","#2ecc71"),
            )}
        </div>
    </div>"""

    # ── Active crypto (for header) ────────────────────────────────────────────
    active_syms = ", ".join(s.replace("/USD", "") for s in status["active_symbols"]) or (
        "⏳ Loading..." if status["refreshing"] else "Pending first refresh"
    )

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
        h2 {{
            color: #8b949e; font-size: 12px; font-weight: normal;
            text-transform: uppercase; letter-spacing: 1px;
            margin: 28px 0 10px;
            border-bottom: 1px solid #21262d; padding-bottom: 6px;
        }}
        /* Header */
        .topbar {{
            display: flex; align-items: center; flex-wrap: wrap;
            gap: 10px; margin-bottom: 24px;
            background: #161b22; border: 1px solid #21262d;
            border-radius: 10px; padding: 14px 18px;
        }}
        .topbar-title {{
            color: #58a6ff; font-size: 18px; font-weight: 700;
            margin-right: 8px; white-space: nowrap;
        }}
        .pill {{
            background: #21262d; border: 1px solid #30363d;
            border-radius: 20px; padding: 5px 14px;
            font-size: 13px; white-space: nowrap;
        }}
        .topbar-right {{
            margin-left: auto; color: #555; font-size: 11px; white-space: nowrap;
        }}
        /* Tables */
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
        /* Config cards */
        .cfg-card {{
            background: #161b22; border: 1px solid #21262d;
            border-radius: 8px; padding: 14px;
        }}
        .cfg-title {{
            color: #8b949e; font-size: 11px; text-transform: uppercase;
            letter-spacing: 1px; margin-bottom: 10px;
        }}
        .empty {{ color: #555; padding: 10px 0; }}
        /* Active crypto bar */
        .cryptobar {{
            background: #161b22; border: 1px solid #21262d;
            border-radius: 8px; padding: 10px 14px;
            color: #58a6ff; font-size: 13px; margin-bottom: 4px;
        }}
    </style>
</head>
<body>

    <!-- ── TOP BAR ── -->
    <div class="topbar">
        <div class="topbar-title">⚡ Trading Bot</div>
        {eq_pill}
        {wl_pill}
        {daily_pill}
        {total_pill}
        {halt_pill}
        {mkt_pill}
        <div class="topbar-right">
            uptime {status["uptime"]} &nbsp;·&nbsp; refresh {status["last_refresh"]}<br>
            auto-refresh 60s &nbsp;·&nbsp; {now}
        </div>
    </div>

    <!-- ── ACTIVE CRYPTO ── -->
    <div class="cryptobar">
        🔍 Active crypto: <b>{active_syms}</b>
    </div>

    <!-- ── OPEN POSITIONS ── -->
    <h2>Open Positions ({open_count})</h2>
    {pos_html}

    <!-- ── CRYPTO ENTRY TRACKING ── -->
    <h2>Crypto Entry Tracking (Trailing Stops)</h2>
    {entries_html}

    <!-- ── LAST 10 CLOSED TRADES ── -->
    <h2>Last 10 Closed Trades</h2>
    {closed_html}

    <!-- ── LIVE LOGS ── -->
    <h2>Live Bot Logs (last 100 lines)</h2>
    {logs_html}

    <!-- ── ETF SIGNALS ── -->
    <h2>ETF Signals (last run)</h2>
    {etf_html}

    <!-- ── PERFORMANCE STATS ── -->
    <h2>Performance Stats</h2>
    {perf_html}

    <!-- ── BOT CONFIGURATION ── -->
    <h2>Bot Configuration</h2>
    {cfg_html}

</body>
</html>"""

    return html


# ---------------------------------------------------------------------------
# Start
# ---------------------------------------------------------------------------

def start_dashboard(trader_instance, live_instance) -> None:
    """Start the Flask dashboard in a background daemon thread."""
    global _trader, _live
    _trader = trader_instance
    _live   = live_instance

    # Attach memory log handler to root logger so all bot logs appear in dashboard
    mem_handler = _MemoryLogHandler()
    mem_handler.setLevel(logging.INFO)
    logging.getLogger().addHandler(mem_handler)

    port = int(os.environ.get("PORT", 5000))

    def run():
        log = logging.getLogger("werkzeug")
        log.setLevel(logging.ERROR)
        app.run(host="0.0.0.0", port=port, debug=False, use_reloader=False)

    t = threading.Thread(target=run, daemon=True, name="Dashboard")
    t.start()
    logger.info(f"[DASHBOARD] Web dashboard running on port {port}")

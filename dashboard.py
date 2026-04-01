"""
Web dashboard for the trading bot.
Runs as a Flask server on PORT (set by Railway) in a background thread.
Professional broker-style layout with Chart.js equity curve.
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

# In-memory log ring buffer
_log_buffer: deque = deque(maxlen=300)

LEVEL_COLOR = {
    "DEBUG": "#555", "INFO": "#8b949e", "WARNING": "#e67e22",
    "ERROR": "#e74c3c", "CRITICAL": "#ff0000",
}
LEVEL_HIGHLIGHT = {"WARNING", "ERROR", "CRITICAL"}


class _MemoryLogHandler(logging.Handler):
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
            "equity": equity, "cash": cash,
            "daily_pnl": daily_pnl, "daily_pct": daily_pct,
            "start": 2000.0,
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
                "symbol": sym, "side": str(pos.side).replace("PositionSide.", ""),
                "qty": qty, "entry": entry, "current": curr,
                "pnl": pnl, "pnl_pct": pnl_pct, "value": abs(qty * curr),
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
    all_t  = _all_trades(200)
    closed = [t for t in all_t if t.get("action", "") in ("CLOSE", "SELL", "COVER")]
    return closed[:n]


def _equity_curve_data() -> dict:
    """Build equity curve from DB snapshots + trade history fallback."""
    snapshots = db.get_equity_history(500)
    if snapshots:
        labels = [s["ts"] for s in snapshots]
        values = [s["equity"] for s in snapshots]
    else:
        # Fallback: reconstruct from closed trades
        labels = []
        values = []
        equity = 2000.0
        all_trades = _all_trades(200)
        for t in reversed(all_trades):  # oldest first
            if t.get("action") not in ("CLOSE", "SELL", "COVER"):
                continue
            pnl_pct = t.get("pnl_pct")
            if pnl_pct is None:
                note = t.get("note", "")
                if "pnl=" in note:
                    try:
                        pnl_pct = float(note.split("pnl=")[1].split("%")[0])
                    except Exception:
                        continue
                else:
                    continue
            # Approximate: 1% risk means pnl impact on portfolio
            dollar_change = equity * (float(pnl_pct) / 100) * RISK_PER_TRADE_PCT * 10
            equity += dollar_change
            ts = t.get("timestamp", "")
            labels.append(str(ts))
            values.append(round(equity, 2))

    # Append current live equity as final point
    try:
        account = _trader.get_account()
        labels.append(datetime.now().isoformat())
        values.append(float(account.equity))
    except Exception:
        pass

    # Ensure we always have at least the starting point
    if not values:
        labels = [datetime.now().isoformat()]
        values = [2000.0]

    return {"labels": labels, "values": values}


def _market_status() -> dict:
    from datetime import timezone, timedelta
    now_utc = datetime.now(timezone.utc)
    month   = now_utc.month
    et_offset = timedelta(hours=-4) if 3 <= month <= 10 else timedelta(hours=-5)
    now_et    = now_utc + et_offset
    is_weekday   = now_et.weekday() < 5
    market_open  = now_et.replace(hour=9, minute=30, second=0, microsecond=0)
    market_close = now_et.replace(hour=16, minute=0, second=0, microsecond=0)
    is_open      = is_weekday and market_open <= now_et <= market_close

    def _fmt(td):
        total = int(td.total_seconds())
        h, r  = divmod(total, 3600)
        m, s  = divmod(r, 60)
        return f"{h}h {m}m" if h else f"{m}m {s}s"

    if is_open:
        return {"open": True, "label": "OPEN", "sub": f"closes in {_fmt(market_close - now_et)}",
                "et_time": now_et.strftime("%H:%M ET")}
    elif is_weekday and now_et < market_open:
        return {"open": False, "label": "CLOSED", "sub": f"opens in {_fmt(market_open - now_et)}",
                "et_time": now_et.strftime("%H:%M ET")}
    else:
        days_ahead = (7 - now_et.weekday()) % 7 or 7
        next_open  = (now_et + timedelta(days=days_ahead)).replace(hour=9, minute=30, second=0, microsecond=0)
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
        "uptime": f"{h}h {m}m {s}s",
        "active_symbols": active,
        "open_entries": entries,
        "refreshing": refreshing,
        "last_refresh": (
            datetime.fromtimestamp(last_refresh).strftime("%H:%M:%S")
            if last_refresh > 0 else ("Loading..." if refreshing else "Pending")
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
        "kelly_risk":       f"{kelly_risk*100:.2f}% (active)" if kelly_risk and kelly_risk != RISK_PER_TRADE_PCT else f"{RISK_PER_TRADE_PCT*100:.1f}% (base)",
        "max_stop":         f"{STOP_LOSS_MAX_PCT*100:.1f}%",
        "max_tp":           f"{TAKE_PROFIT_MAX_PCT*100:.1f}%",
        "trailing_stop":    f"{TRAILING_STOP_PCT*100:.0f}%",
        "daily_loss_limit": f"{DAILY_LOSS_LIMIT_PCT*100:.0f}%",
        "max_long_etf":     MAX_POSITIONS,
        "max_short_etf":    MAX_SHORT_POSITIONS,
        "max_crypto":       MAX_CRYPTO_POSITIONS,
        "buy_threshold":    f"+{MIN_BUY_SCORE}",
        "sell_threshold":   f"{MIN_SELL_SCORE}",
        "etf_pool":         f"{len(ETF_CANDIDATES)} -> top {SCREEN_TOP_N_ETF}",
        "crypto_pool":      f"{len(CRYPTO_CANDIDATES)} -> top {SCREEN_TOP_N_CRYPTO}",
        "etf_interval":     f"{RUN_INTERVAL_MINUTES}min",
        "crypto_cooldown":  f"{TRADE_COOLDOWN}s",
        "data_refresh":     f"{FULL_REFRESH_INTERVAL//60}min",
        "btc_filter":       "ON" if BTC_CORRELATION_FILTER else "OFF",
        "pyramid":          f"+{int(PYRAMID_ADD_PCT*100)}% at +{int(PYRAMID_TRIGGER_PCT*100)}%",
        "kelly":            f"{int(KELLY_FRACTION*100)}% Kelly after {KELLY_MIN_TRADES} trades",
    }


def _pnl_color(val: float) -> str:
    return "#00c087" if val >= 0 else "#ff5252"


def _pnl_badge(pct: float, show_sign=True) -> str:
    color = _pnl_color(pct)
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
        return f"<pre style='color:#ff5252;background:#0d1117;padding:20px'>Dashboard error:\n{e}</pre>", 500


def _render_dashboard():
    account   = _account_data()
    positions = _positions_data()
    closed    = _closed_trades(10)
    stats     = get_stats()
    status    = _bot_status()
    cfg       = _config_info()
    now       = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    mkt       = _market_status()
    curve     = _equity_curve_data()

    # ── Hero section (equity + P&L) ──────────────────────────────────────────
    if "error" not in account:
        eq          = account["equity"]
        daily_pnl   = account["daily_pnl"]
        daily_pct   = account["daily_pct"]
        total_pnl   = account["total_pnl"]
        total_pct   = account["total_pct"]
        d_color     = _pnl_color(daily_pnl)
        t_color     = _pnl_color(total_pnl)
        d_sign      = "+" if daily_pnl >= 0 else ""
        t_sign      = "+" if total_pnl >= 0 else ""
        hero_html   = f"""
        <div class="hero-value">${eq:,.2f}</div>
        <div class="hero-pnl">
            <span style="color:{t_color}">{t_sign}${total_pnl:,.2f} ({t_sign}{total_pct:.2f}%) all time</span>
            <span class="hero-sep">|</span>
            <span style="color:{d_color}">{d_sign}${daily_pnl:,.2f} ({d_sign}{daily_pct:.2f}%) today</span>
        </div>"""
    else:
        hero_html = '<div class="hero-value" style="color:#ff5252">API Error</div>'

    # Chart gradient color
    chart_color = "#00c087" if ("error" not in account and account["total_pnl"] >= 0) else "#ff5252"
    curve_json  = json.dumps(curve)

    # ── Status pills ─────────────────────────────────────────────────────────
    halt_class = "pill-red" if status["daily_halt"] else "pill-green"
    halt_text  = "HALTED" if status["daily_halt"] else "ACTIVE"
    mkt_class  = "pill-green" if mkt["open"] else "pill-neutral"

    # W/L
    if stats.get("completed", 0) > 0:
        wl_text  = f'{stats["wins"]}W / {stats["losses"]}L ({stats["win_rate"]}%)'
        wl_class = "pill-green" if stats["win_rate"] >= 45 else "pill-red"
    else:
        wl_text  = "No trades"
        wl_class = "pill-neutral"

    active_syms = ", ".join(s.replace("/USD", "") for s in status["active_symbols"]) or (
        "Loading..." if status["refreshing"] else "Pending")

    # ── Stat cards ───────────────────────────────────────────────────────────
    def stat_card(label, value, color="#e6edf3"):
        return f'<div class="stat-card"><div class="stat-label">{label}</div><div class="stat-value" style="color:{color}">{value}</div></div>'

    if stats.get("completed", 0) > 0:
        wr_color = "#00c087" if stats["win_rate"] >= 45 else "#ff5252"
        pf_color = "#00c087" if stats["profit_factor"] >= 1.5 else "#ff5252"
        stat_cards = (
            stat_card("Win Rate", f'{stats["win_rate"]}%', wr_color) +
            stat_card("Profit Factor", f'{stats["profit_factor"]}', pf_color) +
            stat_card("Avg Win", f'+{stats["avg_win_pct"]}%', "#00c087") +
            stat_card("Avg Loss", f'{stats["avg_loss_pct"]}%', "#ff5252") +
            stat_card("Trades", f'{stats["completed"]}') +
            stat_card("Total P&L", f'{stats["total_pnl_pct"]:+.2f}%',
                      _pnl_color(stats["total_pnl_pct"]))
        )
    else:
        stat_cards = (
            stat_card("Win Rate", "--") +
            stat_card("Profit Factor", "--") +
            stat_card("Avg Win", "--") +
            stat_card("Avg Loss", "--") +
            stat_card("Trades", f'{stats.get("total_entries", 0)} opened') +
            stat_card("Total P&L", "--")
        )

    # ── Open Positions ───────────────────────────────────────────────────────
    open_count = len([p for p in positions if "error" not in p])
    if not positions or "error" in positions[0]:
        pos_rows = '<tr><td colspan="7" class="empty-cell">No open positions</td></tr>'
    else:
        pos_rows = ""
        for p in positions:
            c = _pnl_color(p["pnl_pct"])
            pos_rows += f"""<tr>
                <td><span class="symbol">{p["symbol"]}</span>
                    <span class="side-badge {'side-long' if p['side']=='long' else 'side-short'}">{p["side"]}</span></td>
                <td>${p["entry"]:.4f}</td>
                <td>${p["current"]:.4f}</td>
                <td style="color:{c}">${p["pnl"]:+.2f}</td>
                <td>{_pnl_badge(p["pnl_pct"])}</td>
                <td>${p["value"]:,.2f}</td>
            </tr>"""

    # Crypto entries (trailing stops)
    if status["open_entries"]:
        entry_rows = ""
        for sym, e in status["open_entries"].items():
            side = e.get("side", "long")
            ep   = e["price"]
            init_stop = e.get("stop", 0)
            if side == "short":
                trough = e.get("trough_price", ep)
                trail  = trough * (1 + TRAILING_STOP_PCT)
                eff    = min(init_stop, trail)
                active = trail < init_stop
            else:
                peak  = e.get("peak_price", ep)
                trail = peak * (1 - TRAILING_STOP_PCT)
                eff   = max(init_stop, trail)
                active = trail > init_stop
            sc = "#00c087" if active else "#e67e22"
            lbl = "trailing" if active else "initial"
            entry_rows += f"""<tr>
                <td><span class="symbol">{sym}</span>
                    <span class="side-badge {'side-short' if side=='short' else 'side-long'}">{side}</span></td>
                <td>${ep:.4f}</td>
                <td style="color:{sc}">${eff:.4f} <small>({lbl})</small></td>
                <td>{"Yes" if e.get("pyramided") else "--"}</td>
            </tr>"""
        entries_html = f"""<table>
            <thead><tr><th>Symbol</th><th>Entry</th><th>Stop</th><th>Pyramided</th></tr></thead>
            <tbody>{entry_rows}</tbody></table>"""
    else:
        entries_html = '<p class="empty">No tracked entries</p>'

    # ── Closed Trades ────────────────────────────────────────────────────────
    if not closed:
        closed_rows = '<tr><td colspan="5" class="empty-cell">No closed trades yet</td></tr>'
    else:
        closed_rows = ""
        for t in closed:
            action = t.get("action", "")
            ac = {"SELL": "#ff5252", "COVER": "#448aff", "CLOSE": "#ab47bc"}.get(action, "#888")
            try:
                pf = f'${float(t["price"]):.4f}'
            except Exception:
                pf = str(t.get("price", ""))
            note = t.get("note", "")
            pnl_html = ""
            if "pnl=" in note:
                try:
                    pv = float(note.split("pnl=")[1].split("%")[0])
                    pnl_html = _pnl_badge(pv)
                except Exception:
                    pnl_html = f'<span class="muted">{note[:30]}</span>'
            else:
                pnl_html = f'<span class="muted">{note[:30]}</span>'
            ts_str = str(t.get("timestamp", ""))
            # Format timestamp nicely
            if len(ts_str) > 19:
                ts_str = ts_str[:19]
            closed_rows += f"""<tr>
                <td class="muted" style="font-size:12px">{ts_str}</td>
                <td style="color:{ac};font-weight:600">{action}</td>
                <td><span class="symbol">{t.get("symbol","")}</span></td>
                <td>{pf}</td>
                <td>{pnl_html}</td>
            </tr>"""

    # ── ETF Signals ──────────────────────────────────────────────────────────
    if _etf_signals:
        etf_rows = ""
        for sig in sorted(_etf_signals, key=lambda x: x["score"], reverse=True):
            sc = sig["score"]
            sc_c = "#00c087" if sc > 0 else ("#ff5252" if sc < 0 else "#888")
            sg_c = {"BUY": "#00c087", "SELL": "#ff5252", "HOLD": "#888"}.get(sig["signal"], "#888")
            etf_rows += f"""<tr>
                <td><span class="symbol">{sig["ticker"]}</span></td>
                <td style="color:{sc_c}">{int(sc):+d}</td>
                <td style="color:{sg_c};font-weight:600">{sig["signal"]}</td>
                <td>${sig["price"]:.2f}</td>
                <td class="muted">{sig.get("regime","")}</td>
            </tr>"""
        etf_html = f"""<table>
            <thead><tr><th>Ticker</th><th>Score</th><th>Signal</th><th>Price</th><th>Regime</th></tr></thead>
            <tbody>{etf_rows}</tbody></table>"""
    else:
        etf_html = '<p class="empty">No ETF signals -- market may be closed</p>'

    # ── Live Logs ────────────────────────────────────────────────────────────
    log_lines = list(_log_buffer)[-100:]
    if log_lines:
        log_items = ""
        for line in log_lines:
            lvl = line["level"]
            c   = LEVEL_COLOR.get(lvl, "#8b949e")
            bld = "font-weight:600;" if lvl in LEVEL_HIGHLIGHT else ""
            bg  = "background:#2a0a0a;" if lvl == "ERROR" else (
                  "background:#2a1f00;" if lvl == "WARNING" else "")
            # Escape HTML in log messages
            msg = line["msg"].replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
            log_items += (
                f'<div style="padding:1px 0;{bg}">'
                f'<span style="color:#444">{line["time"]} </span>'
                f'<span style="color:{c};{bld}">[{lvl[:4]}]</span> '
                f'<span style="color:#b0b8c1">{msg}</span></div>'
            )
        logs_html = f'<div class="logbox" id="logbox">{log_items}</div>'
    else:
        logs_html = '<p class="empty">No logs yet</p>'

    # ── Config grid ──────────────────────────────────────────────────────────
    def cfg_row(k, v):
        return f'<div class="cfg-row"><span class="cfg-key">{k}</span><span class="cfg-val">{v}</span></div>'

    cfg_html = f"""
    <div class="cfg-grid">
        <div class="cfg-section">
            <div class="cfg-section-title">Risk</div>
            {cfg_row("Per trade", cfg["risk_per_trade"])}
            {cfg_row("Kelly", cfg["kelly_risk"])}
            {cfg_row("Stop loss", cfg["max_stop"])}
            {cfg_row("Take profit", cfg["max_tp"])}
            {cfg_row("Trail stop", cfg["trailing_stop"])}
            {cfg_row("Daily halt", cfg["daily_loss_limit"])}
        </div>
        <div class="cfg-section">
            <div class="cfg-section-title">Positions</div>
            {cfg_row("ETF longs", cfg["max_long_etf"])}
            {cfg_row("ETF shorts", cfg["max_short_etf"])}
            {cfg_row("Crypto", cfg["max_crypto"])}
            {cfg_row("Buy score", cfg["buy_threshold"])}
            {cfg_row("Sell score", cfg["sell_threshold"])}
        </div>
        <div class="cfg-section">
            <div class="cfg-section-title">Timing</div>
            {cfg_row("ETF interval", cfg["etf_interval"])}
            {cfg_row("Crypto cooldown", cfg["crypto_cooldown"])}
            {cfg_row("Data refresh", cfg["data_refresh"])}
            {cfg_row("Pyramid", cfg["pyramid"])}
            {cfg_row("BTC filter", cfg["btc_filter"])}
        </div>
    </div>"""

    # ── FULL HTML ────────────────────────────────────────────────────────────
    html = f"""<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta http-equiv="refresh" content="60">
    <meta name="viewport" content="width=device-width, initial-scale=1">
    <title>Trading Bot</title>
    <script src="https://cdn.jsdelivr.net/npm/chart.js@4/dist/chart.umd.min.js"></script>
    <script src="https://cdn.jsdelivr.net/npm/chartjs-adapter-date-fns@3/dist/chartjs-adapter-date-fns.bundle.min.js"></script>
    <style>
        * {{ box-sizing: border-box; margin: 0; padding: 0; }}
        body {{
            background: #0d1117; color: #c9d1d9;
            font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif;
            font-size: 14px; padding: 0;
        }}
        .container {{ max-width: 1200px; margin: 0 auto; padding: 20px 24px; }}

        /* ── Hero ── */
        .hero {{
            padding: 24px 0 8px;
        }}
        .hero-value {{
            font-size: 42px; font-weight: 700; color: #e6edf3;
            letter-spacing: -1px;
        }}
        .hero-pnl {{
            font-size: 15px; margin-top: 4px;
        }}
        .hero-sep {{ color: #30363d; margin: 0 10px; }}

        /* ── Status bar ── */
        .status-bar {{
            display: flex; align-items: center; flex-wrap: wrap;
            gap: 8px; margin: 16px 0;
        }}
        .pill {{
            display: inline-flex; align-items: center; gap: 6px;
            background: #161b22; border: 1px solid #21262d;
            border-radius: 20px; padding: 5px 14px;
            font-size: 12px; font-weight: 500; white-space: nowrap;
        }}
        .pill-green {{ color: #00c087; }}
        .pill-red   {{ color: #ff5252; }}
        .pill-neutral {{ color: #8b949e; }}
        .pill-dot {{
            width: 7px; height: 7px; border-radius: 50%;
            display: inline-block;
        }}
        .pill-dot-green {{ background: #00c087; }}
        .pill-dot-red   {{ background: #ff5252; }}
        .pill-dot-neutral {{ background: #8b949e; }}

        /* ── Chart ── */
        .chart-container {{
            background: #161b22; border: 1px solid #21262d;
            border-radius: 12px; padding: 20px;
            margin: 16px 0; height: 280px; position: relative;
        }}

        /* ── Stat cards ── */
        .stats-row {{
            display: grid;
            grid-template-columns: repeat(auto-fill, minmax(150px, 1fr));
            gap: 10px; margin: 16px 0;
        }}
        .stat-card {{
            background: #161b22; border: 1px solid #21262d;
            border-radius: 10px; padding: 14px 16px;
        }}
        .stat-label {{
            font-size: 11px; color: #8b949e; text-transform: uppercase;
            letter-spacing: 0.5px; margin-bottom: 4px;
        }}
        .stat-value {{
            font-size: 20px; font-weight: 700;
        }}

        /* ── Section headers ── */
        .section-header {{
            font-size: 13px; font-weight: 600; color: #8b949e;
            text-transform: uppercase; letter-spacing: 0.5px;
            margin: 28px 0 10px; padding-bottom: 8px;
            border-bottom: 1px solid #21262d;
        }}

        /* ── Tables ── */
        .card {{
            background: #161b22; border: 1px solid #21262d;
            border-radius: 12px; overflow: hidden;
        }}
        table {{ width: 100%; border-collapse: collapse; background: transparent; }}
        th {{
            background: #0d1117; color: #8b949e; font-size: 11px;
            text-transform: uppercase; letter-spacing: 0.5px;
            padding: 10px 14px; text-align: left; font-weight: 600;
        }}
        td {{ padding: 10px 14px; border-top: 1px solid #21262d; }}
        tr:first-child td {{ border-top: none; }}
        tr:hover td {{ background: rgba(255,255,255,0.02); }}
        .empty-cell {{ color: #484f58; text-align: center; padding: 24px; }}
        .empty {{ color: #484f58; padding: 16px 0; }}
        .muted {{ color: #484f58; font-size: 12px; }}
        .symbol {{ font-weight: 600; color: #e6edf3; }}
        .side-badge {{
            font-size: 10px; font-weight: 600; text-transform: uppercase;
            padding: 2px 6px; border-radius: 4px; margin-left: 6px;
        }}
        .side-long  {{ background: rgba(0,192,135,0.15); color: #00c087; }}
        .side-short {{ background: rgba(255,82,82,0.15); color: #ff5252; }}

        /* ── Two-column layout ── */
        .grid-2 {{
            display: grid;
            grid-template-columns: 1fr 1fr;
            gap: 16px;
        }}
        @media (max-width: 800px) {{
            .grid-2 {{ grid-template-columns: 1fr; }}
        }}

        /* ── Log box ── */
        .logbox {{
            background: #010409; border: 1px solid #21262d;
            border-radius: 12px; padding: 14px;
            font-family: 'SF Mono', 'Fira Code', monospace; font-size: 11px;
            max-height: 400px; overflow-y: auto; line-height: 1.6;
        }}

        /* ── Config ── */
        .cfg-grid {{
            display: grid; grid-template-columns: repeat(auto-fill, minmax(260px, 1fr));
            gap: 12px;
        }}
        .cfg-section {{
            background: #161b22; border: 1px solid #21262d;
            border-radius: 10px; padding: 14px;
        }}
        .cfg-section-title {{
            font-size: 11px; color: #8b949e; text-transform: uppercase;
            letter-spacing: 0.5px; margin-bottom: 10px; font-weight: 600;
        }}
        .cfg-row {{
            display: flex; justify-content: space-between;
            padding: 3px 0; font-size: 13px;
        }}
        .cfg-key {{ color: #8b949e; }}
        .cfg-val {{ color: #e6edf3; font-weight: 500; }}

        /* ── Footer ── */
        .footer {{
            text-align: center; color: #30363d; font-size: 11px;
            padding: 24px 0 8px;
        }}
    </style>
</head>
<body>
<div class="container">

    <!-- ── HERO ── -->
    <div class="hero">
        {hero_html}
    </div>

    <!-- ── STATUS BAR ── -->
    <div class="status-bar">
        <span class="pill {halt_class}">
            <span class="pill-dot {'pill-dot-green' if not status['daily_halt'] else 'pill-dot-red'}"></span>
            {halt_text}
        </span>
        <span class="pill {mkt_class}">
            <span class="pill-dot {'pill-dot-green' if mkt['open'] else 'pill-dot-neutral'}"></span>
            ETF {mkt["label"]} &middot; {mkt["sub"]}
        </span>
        <span class="pill {wl_class}">{wl_text}</span>
        <span class="pill pill-neutral">Crypto: {active_syms}</span>
        <span class="pill pill-neutral">Uptime {status["uptime"]}</span>
    </div>

    <!-- ── EQUITY CHART ── -->
    <div class="chart-container">
        <canvas id="equityChart"></canvas>
    </div>

    <!-- ── STAT CARDS ── -->
    <div class="stats-row">
        {stat_cards}
    </div>

    <!-- ── POSITIONS + TRADES (2 col) ── -->
    <div class="grid-2">
        <div>
            <div class="section-header">Open Positions ({open_count})</div>
            <div class="card">
                <table>
                    <thead><tr><th>Symbol</th><th>Entry</th><th>Current</th><th>P&L $</th><th>P&L %</th><th>Value</th></tr></thead>
                    <tbody>{pos_rows}</tbody>
                </table>
            </div>
            {f'<div class="section-header">Trailing Stops</div><div class="card">{entries_html}</div>' if status["open_entries"] else ''}
        </div>
        <div>
            <div class="section-header">Recent Closed Trades</div>
            <div class="card">
                <table>
                    <thead><tr><th>Time</th><th>Action</th><th>Symbol</th><th>Price</th><th>P&L</th></tr></thead>
                    <tbody>{closed_rows}</tbody>
                </table>
            </div>
        </div>
    </div>

    <!-- ── ETF SIGNALS ── -->
    <div class="section-header">ETF Signals</div>
    <div class="card">
        {etf_html}
    </div>

    <!-- ── LIVE LOGS ── -->
    <div class="section-header">Live Logs</div>
    {logs_html}

    <!-- ── CONFIG ── -->
    <div class="section-header">Bot Configuration</div>
    {cfg_html}

    <div class="footer">
        Auto-refresh 60s &middot; {now} &middot; Last data refresh: {status["last_refresh"]}
    </div>
</div>

<!-- ── Chart.js ── -->
<script>
(function() {{
    const data = {curve_json};
    const ctx = document.getElementById('equityChart').getContext('2d');
    const color = '{chart_color}';

    const gradient = ctx.createLinearGradient(0, 0, 0, 260);
    gradient.addColorStop(0, color + '40');
    gradient.addColorStop(1, color + '00');

    new Chart(ctx, {{
        type: 'line',
        data: {{
            labels: data.labels,
            datasets: [{{
                data: data.values,
                borderColor: color,
                backgroundColor: gradient,
                borderWidth: 2.5,
                fill: true,
                tension: 0.3,
                pointRadius: data.values.length > 50 ? 0 : 3,
                pointHoverRadius: 5,
                pointBackgroundColor: color,
            }}]
        }},
        options: {{
            responsive: true,
            maintainAspectRatio: false,
            plugins: {{
                legend: {{ display: false }},
                tooltip: {{
                    backgroundColor: '#161b22',
                    borderColor: '#30363d',
                    borderWidth: 1,
                    titleColor: '#8b949e',
                    bodyColor: '#e6edf3',
                    bodyFont: {{ weight: '600', size: 14 }},
                    padding: 12,
                    displayColors: false,
                    callbacks: {{
                        label: function(ctx) {{
                            return '$' + ctx.parsed.y.toLocaleString(undefined, {{
                                minimumFractionDigits: 2, maximumFractionDigits: 2
                            }});
                        }}
                    }}
                }}
            }},
            scales: {{
                x: {{
                    display: true,
                    grid: {{ display: false }},
                    ticks: {{ color: '#484f58', font: {{ size: 10 }}, maxTicksLimit: 8 }}
                }},
                y: {{
                    display: true,
                    grid: {{ color: '#21262d', drawBorder: false }},
                    ticks: {{
                        color: '#484f58',
                        font: {{ size: 11 }},
                        callback: function(v) {{ return '$' + v.toLocaleString(); }}
                    }}
                }}
            }},
            interaction: {{ intersect: false, mode: 'index' }},
        }}
    }});

    // Auto-scroll logs
    var lb = document.getElementById("logbox");
    if(lb) lb.scrollTop = lb.scrollHeight;
}})();
</script>

</body>
</html>"""

    return html


# ---------------------------------------------------------------------------
# Start
# ---------------------------------------------------------------------------

def start_dashboard(trader_instance, live_instance) -> None:
    global _trader, _live
    _trader = trader_instance
    _live   = live_instance

    mem_handler = _MemoryLogHandler()
    mem_handler.setLevel(logging.INFO)
    logging.getLogger().addHandler(mem_handler)

    port = int(os.environ.get("PORT", 5000))

    def run():
        logging.getLogger("werkzeug").setLevel(logging.ERROR)
        app.run(host="0.0.0.0", port=port, debug=False, use_reloader=False)

    t = threading.Thread(target=run, daemon=True, name="Dashboard")
    t.start()
    logger.info(f"[DASHBOARD] Web dashboard running on port {port}")

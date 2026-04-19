"""
KK Trader — Flask Server
Port 5559 | 127.0.0.1 only
"""

import os
import sys
import secrets
from functools import wraps
from flask import Flask, jsonify, render_template, request
from dotenv import load_dotenv

# Use KK's edge module if trader is run from same venv
sys.path.insert(0, os.path.expanduser("~/kalshi-edge"))

load_dotenv()
load_dotenv(os.path.expanduser("~/kalshi-edge/.env"), override=False)

from trader import TradingEngine

app = Flask(__name__)
engine = TradingEngine()

# ── Local API token — generated fresh each startup ────────────────────────────
# Prevents any other local process from calling control endpoints.
# The dashboard receives this token via /api/token (GET, no auth required)
# and includes it in all subsequent POST requests as X-KK-Token.
_LOCAL_TOKEN = secrets.token_hex(16)


def _require_token(f):
    """Decorator: require X-KK-Token header on mutating endpoints."""
    @wraps(f)
    def decorated(*args, **kwargs):
        token = request.headers.get("X-KK-Token", "")
        if token != _LOCAL_TOKEN:
            return jsonify({"ok": False, "error": "unauthorized"}), 403
        return f(*args, **kwargs)
    return decorated


# ── Pages ─────────────────────────────────────────────────────────────────────

@app.route("/")
def index():
    return render_template("index.html")

@app.route("/guide")
def guide():
    return render_template("guide.html")


# ── Auth handshake — dashboard calls this on load ─────────────────────────────

@app.route("/api/token")
def get_token():
    """Returns the session token so the dashboard can auth its POST requests."""
    return jsonify({"token": _LOCAL_TOKEN})


# ── Bot Control ───────────────────────────────────────────────────────────────

@app.route("/api/bot/start", methods=["POST"])
@_require_token
def bot_start():
    engine.start()
    return jsonify({"ok": True, "status": "started"})


@app.route("/api/bot/stop", methods=["POST"])
@_require_token
def bot_stop():
    engine.stop()
    return jsonify({"ok": True, "status": "stopped"})


@app.route("/api/reset-paper", methods=["POST"])
@_require_token
def reset_paper():
    """Clear all paper positions and reset daily P&L. Paper mode only."""
    from trader import PAPER_TRADING
    if not PAPER_TRADING:
        return jsonify({"ok": False, "error": "Only available in paper mode"}), 400
    with engine._lock:
        count = len(engine.positions)
        engine.positions.clear()
        engine._daily_pnl = 0.0
        engine._wins = 0
        engine._losses = 0
        engine._status_msg = "Paper reset — positions cleared"
    print(f"  🗑 Paper reset: cleared {count} positions, zeroed daily P&L")
    return jsonify({"ok": True, "cleared": count})


# ── Data ──────────────────────────────────────────────────────────────────────

@app.route("/api/status")
def status():
    return jsonify(engine.get_status())


@app.route("/api/trades")
def trades():
    return jsonify(engine.get_trades())


@app.route("/api/positions")
def positions():
    return jsonify(engine.get_positions())


# ── Settings ──────────────────────────────────────────────────────────────────

@app.route("/api/chat", methods=["POST"])
def chat():
    import json
    data    = request.get_json(force=True)
    message = data.get("message", "")
    context = data.get("context", {})
    api_key = os.getenv("ANTHROPIC_API_KEY", "")

    if not api_key:
        return jsonify({"reply": "Chat requires ANTHROPIC_API_KEY in .env. Add it and restart."})

    try:
        import anthropic
        # Re-use module-level client if already created — avoid rebuilding per request
        if not hasattr(chat, "_client"):
            chat._client = anthropic.Anthropic(api_key=api_key)
        client = chat._client
        system = (
            "You are KK, an AI assistant embedded in KK Trader — an autonomous Kalshi prediction market trading bot. "
            "You have access to the user's live session data. Be concise, sharp, and direct. "
            f"Current session: P&L={context.get('pnl','?')}, "
            f"Open positions={context.get('positions','?')}, "
            f"Wins={context.get('wins','?')}, Losses={context.get('losses','?')}, "
            f"Markets watched={context.get('markets','?')}. "
            f"Recent trades: {json.dumps(context.get('trades',[])[:5])}"
        )
        resp = client.messages.create(
            model="claude-sonnet-4-6",
            max_tokens=400,
            system=system,
            messages=[{"role": "user", "content": message}]
        )
        return jsonify({"reply": resp.content[0].text})
    except Exception as e:
        return jsonify({"reply": f"Chat error: {str(e)[:100]}"})


@app.route("/api/balance")
def balance():
    import kalshi_api as kapi
    return jsonify({"balance": kapi.get_balance()})


@app.route("/api/settings", methods=["GET"])
def settings_get():
    return jsonify(engine.settings)


@app.route("/api/settings", methods=["POST"])
@_require_token
def settings_post():
    data = request.get_json(force=True)
    engine.update_settings(data)
    return jsonify({"ok": True})


@app.route("/api/exit/<ticker>", methods=["POST"])
@_require_token
def exit_position(ticker):
    ok, msg = engine.force_exit(ticker.upper())
    return jsonify({"ok": ok, "msg": msg})


@app.route("/api/exit/all", methods=["POST"])
@_require_token
def exit_all():
    count = engine.force_exit_all()
    return jsonify({"ok": True, "msg": f"closed {count} position(s)"})


@app.route("/api/mode", methods=["POST"])
@_require_token
def set_mode():
    """Toggle paper / live trading mode."""
    import trader as t_mod
    data = request.get_json(force=True)
    paper = bool(data.get("paper", True))
    t_mod.PAPER_TRADING = paper
    mode = "paper" if paper else "live"
    print(f"  🔄 Trading mode → {mode.upper()}")
    return jsonify({"ok": True, "paper_trading": paper, "mode": mode})


# ── Run ───────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5559))
    host = "0.0.0.0" if os.environ.get("RAILWAY_ENVIRONMENT") else "127.0.0.1"
    print("  🤖 KK Trader — Factory Without Lights")
    print(f"  🌐 http://{host}:{port}")
    app.run(host=host, port=port, debug=False)

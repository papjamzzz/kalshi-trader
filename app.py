"""
KK Trader — Flask Server
Port 5559 | 127.0.0.1 only
"""

import os
import sys
from flask import Flask, jsonify, render_template, request
from dotenv import load_dotenv

# Use KK's edge module if trader is run from same venv
sys.path.insert(0, os.path.expanduser("~/kalshi-edge"))

load_dotenv()
load_dotenv(os.path.expanduser("~/kalshi-edge/.env"), override=False)

from trader import TradingEngine

app = Flask(__name__)
engine = TradingEngine()


# ── Pages ─────────────────────────────────────────────────────────────────────

@app.route("/")
def index():
    return render_template("index.html")

@app.route("/guide")
def guide():
    return render_template("guide.html")


# ── Bot Control ───────────────────────────────────────────────────────────────

@app.route("/api/bot/start", methods=["POST"])
def bot_start():
    engine.start()
    return jsonify({"ok": True, "status": "started"})


@app.route("/api/bot/stop", methods=["POST"])
def bot_stop():
    engine.stop()
    return jsonify({"ok": True, "status": "stopped"})


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
        client = anthropic.Anthropic(api_key=api_key)
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
def settings_post():
    data = request.get_json(force=True)
    engine.update_settings(data)
    return jsonify({"ok": True})


@app.route("/api/exit/<ticker>", methods=["POST"])
def exit_position(ticker):
    ok, msg = engine.force_exit(ticker.upper())
    return jsonify({"ok": ok, "msg": msg})

@app.route("/api/exit/all", methods=["POST"])
def exit_all():
    count = engine.force_exit_all()
    return jsonify({"ok": True, "msg": f"closed {count} position(s)"})

# ── Run ───────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    print("  🤖 KK Trader — Factory Without Lights")
    print("  🌐 http://localhost:5559")
    app.run(host="127.0.0.1", port=5559, debug=False)

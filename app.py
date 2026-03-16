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


# ── Run ───────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    print("  🤖 KK Trader — Factory Without Lights")
    print("  🌐 http://localhost:5559")
    app.run(host="127.0.0.1", port=5559, debug=False)

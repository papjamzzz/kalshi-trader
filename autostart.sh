#!/bin/bash
# KK Trader + KK Edge — Auto-start script
# Runs on login via LaunchAgent

sleep 10  # wait for network to settle

# ── Kill any stale processes ──────────────────────────────────────────────────
lsof -ti:5555 | xargs kill -9 2>/dev/null
lsof -ti:5559 | xargs kill -9 2>/dev/null
sleep 1

# ── Start KK Edge (port 5555) ─────────────────────────────────────────────────
cd /Users/miahsm1/kalshi-edge
source venv/bin/activate
python3 app.py >> /tmp/kk-edge.log 2>&1 &
KK_PID=$!
echo "[$(date)] KK Edge started (PID $KK_PID)" >> /tmp/kk-autostart.log

# ── Wait for KK Edge to be ready ─────────────────────────────────────────────
for i in {1..15}; do
    sleep 2
    curl -s http://localhost:5555/api/markets > /dev/null 2>&1 && break
done

# ── Start KK Trader (port 5559) ───────────────────────────────────────────────
cd /Users/miahsm1/kalshi-trader
./venv/bin/python app.py >> /tmp/kk-trader.log 2>&1 &
TRADER_PID=$!
echo "[$(date)] KK Trader started (PID $TRADER_PID)" >> /tmp/kk-autostart.log

# ── Wait for trader to be ready, then start the engine ───────────────────────
for i in {1..15}; do
    sleep 2
    STATUS=$(curl -s http://localhost:5559/api/status 2>/dev/null)
    [ -n "$STATUS" ] && break
done

sleep 3
curl -s -X POST http://localhost:5559/api/bot/start > /dev/null 2>&1
echo "[$(date)] Trading engine started" >> /tmp/kk-autostart.log

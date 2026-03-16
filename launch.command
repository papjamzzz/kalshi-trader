#!/bin/bash
DIR="$(cd "$(dirname "$0")" && pwd)"
cd "$DIR"

echo ""
echo "  ⚡ KK Trader — Factory Without Lights"
echo "  ──────────────────────────────────────"

# Kill anything on 5559
lsof -ti:5559 | xargs kill -9 2>/dev/null && echo "  🔄 Cleared port 5559" || true

# Setup venv if missing
if [ ! -d venv ]; then
  echo "  📦 First run — installing dependencies..."
  python3 -m venv venv
  ./venv/bin/pip install -q --upgrade pip
  ./venv/bin/pip install -r requirements.txt
fi

# Check for .env
if [ ! -f .env ]; then
  cp .env.example .env
  echo ""
  echo "  ⚠️  .env created from template."
  echo "  Add your KALSHI_API_KEY (and optionally Twilio + Gmail creds)."
  echo "  Then double-click launch.command again."
  echo ""
  open .env
  read -p "  Press Enter when done..."
fi

echo "  🚀 Starting KK Trader on http://localhost:5559"
echo ""

# Open browser after short delay
(sleep 2 && open http://localhost:5559) &

./venv/bin/python app.py

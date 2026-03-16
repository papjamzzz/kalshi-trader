# KK Trader — CLAUDE.md
*Re-entry: KK Trader*

## What This Is
Autonomous trading engine for Kalshi prediction markets.
Companion to KK (Kalshi Konnektor) on port 5555.
This app runs on **port 5559**.

## Status
🟡 Built — needs Kalshi trading API key permissions enabled + Twilio account

## Architecture
- `app.py` — Flask server (port 5559)
- `trader.py` — TradingEngine, background thread, entry/exit logic
- `kalshi_api.py` — All Kalshi REST API calls (read + trading)
- `notifier.py` — Twilio SMS + Gmail SMTP alerts (funny messages)
- `templates/index.html` — Ableton-style dashboard with fader board

## Integration with KK
- Reads scored markets from `http://localhost:5555/api/markets`
- Falls back to direct Kalshi API + edge.py import if KK is offline
- Shares the same KALSHI_API_KEY from .env

## Algorithm Logic
1. Volume guard: pool > min_volume (blocks thin/insider markets)
2. Edge score gate: score from KK ≥ min_edge_score
3. Spread check: ask-bid < 25¢ (tight = efficient price discovery)
4. Expiry guard: >4h to close
5. Direction: YES ask <45¢ → buy YES | YES bid >55¢ → buy NO | 45-55¢ → skip
6. Monitor open positions every scan cycle for TP/SL/expiry exits

## Faders (all runtime-adjustable)
- Max spend/trade ($0.01–$10)
- Max open positions (2–∞)
- Daily loss cap ($5–$200)
- Min pool volume
- Take profit %
- Stop loss %
- Min edge score (40–95)
- Scan interval (30s–10m)

## Notifications
- SMS: 413-834-5062 (Twilio, pending account setup)
- Email: underwaterfile@proton.me (from jeremiahstepehensmith@gmail.com)
- Gmail App Password needed in .env

## Next Steps
- [ ] User enables trading permissions on Kalshi API key
- [ ] Set up Twilio account → fill TWILIO_* in .env
- [ ] Generate Gmail App Password → fill GMAIL_APP_PASSWORD in .env
- [ ] Run and test with very small positions (1–2 contracts)
- [ ] Watch trade log, tune faders, confirm TP/SL working
- [ ] Consider: track P&L history to chart over time
- [ ] Consider: add market whitelist/blacklist by category

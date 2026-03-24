# kk trader

> **The factory without lights.**

An autonomous trading engine for Kalshi prediction markets. Scans every market, filters for edge, enters and exits positions 24/7 — without you touching a thing. Built as a companion to [Kalshi Konnektor](https://github.com/papjamzzz/kalshi-konnektor), which supplies the scored market feed.

---

## The Problem

Finding mispriced markets on Kalshi is one thing. Acting on them — consistently, at the right size, with proper risk management, at 3am — is another. Manual trading is slow, emotional, and impossible to scale.

KK Trader removes the human from the loop entirely. You set the parameters. The machine runs the trades.

---

## Features

- 🤖 **Fully autonomous** — scans, enters, monitors, and exits positions on a configurable interval
- 📡 **Edge-fed entries** — reads scored markets from Kalshi Konnektor, falls back to direct API
- 🎛 **KT-1 Signal Processor** — Ableton-style fader board to tune all risk parameters live, no restart
- 📊 **Session view trade log** — Ableton clip-row style log, blue/green/red by trade outcome
- 💬 **KK chat panel** — ask your bot what it's doing, why, and what it thinks of your session
- 📱 **SMS + email alerts** — Twilio + Gmail, fires on every buy, exit, stop, and daily limit hit
- 🛑 **Daily loss cap** — hard stop when drawdown limit is hit, bot parks itself
- ⚖️ **Multi-signal entry filter** — volume gate, edge score, spread check, expiry guard, direction filter — ALL must pass
- 🌑 **Dark theme** — Ableton aesthetic, built for long sessions

---

## How It Works

```
Kalshi Konnektor (port 5555)
  └── /api/markets — scored market feed
        ↓
  trader.py — TradingEngine (background thread)
        volume gate       ← pool > min_volume (blocks thin/insider markets)
        edge score gate   ← KK score ≥ min_edge_score
        spread check      ← ask−bid < 25¢
        expiry guard      ← > 4h to close
        direction filter  ← YES ask <45¢ → buy YES | YES bid >55¢ → buy NO | 45–55¢ → skip
              ↓
        place_order() → Kalshi REST API v2
              ↓
        monitor loop → take profit | stop loss | expiry exit
              ↓
  notifier.py → Twilio SMS + Gmail on every event
              ↓
  app.py (port 5559) → dashboard + KK chat panel
```

---

## The KT-1 Signal Processor

Eight live faders. Adjust any parameter mid-session without restarting.

| Fader | What It Does | Default |
|-------|-------------|---------|
| Max Spend / Trade | Dollar cap per position | $10 |
| Max Open Positions | Concurrent position limit | 50 |
| Daily Loss Cap | Bot stops for the day at this drawdown | $50 |
| Min Pool Volume | Liquidity guard — filters thin markets | 5,000 contracts |
| Take Profit | Exit when position gains this % | 25% |
| Stop Loss | Exit when position loses this % | 20% |
| Min Edge Score | KK score threshold to enter | 65 |
| Scan Interval | Seconds between market scans | 90s |

Click any fader value to type an exact number — audio production precision, no dragging.

---

## Requirements

- macOS
- Python 3.9+
- [Kalshi Konnektor](https://github.com/papjamzzz/kalshi-konnektor) running on port 5555
- Kalshi API key with trading permissions enabled
- Twilio account (SMS alerts)
- Gmail app password (email alerts)
- Anthropic API key (KK chat panel)

---

## Quick Start

```bash
git clone https://github.com/papjamzzz/kalshi-trader.git
cd kalshi-trader
```

**Option 1 — Double-click launcher (Mac)**
Double-click `launch.command` in Finder. Sets up venv, installs dependencies, starts the server.

**Option 2 — Terminal**
```bash
make setup      # creates venv, installs dependencies
make run        # starts the server at http://localhost:5559
```

Copy `.env.example` to `.env` and fill in your keys before first run.

---

## Environment Variables

```
KALSHI_KEY_ID               # Your Kalshi API key ID
KALSHI_PRIVATE_KEY_PATH     # Path to your RSA private key file

TWILIO_ACCOUNT_SID          # Twilio account SID
TWILIO_AUTH_TOKEN           # Twilio auth token
TWILIO_FROM_NUMBER          # Your Twilio phone number (+1...)

GMAIL_APP_PASSWORD          # Gmail app password (not your login password)

ANTHROPIC_API_KEY           # Anthropic API key for the chat panel
```

---

## Dashboard

| Section | What It Shows |
|---------|--------------|
| Transport bar | Bot status LED, START/STOP controls, links to Kalshi and guide |
| Metric strip | Daily P&L, open positions, wins, losses, markets watched, balance |
| Drawdown bar | Visual daily loss cap progress |
| KT-1 fader board | All 8 risk parameters, live-adjustable |
| Session view | Trade log — blue=buy, green=profit exit, red=stop loss |
| Chat panel | Ask KK anything about your session |

---

## Philosophy

- **Volume-first** — high volume = stability = fair price discovery. Thin markets are insider playgrounds.
- **Edge-filtered** — only trade what KK says is mispriced. No score, no trade.
- **Direction from price** — the market tells you which side to play. Don't guess.
- **No emotion** — the bot doesn't panic, revenge trade, or get attached. You set the rules before the session starts.
- **Factory without lights** — runs whether you're watching or not.

---

## Companion App

KK Trader is built on top of [Kalshi Konnektor](https://github.com/papjamzzz/kalshi-konnektor) — the edge detection dashboard that scores markets and supplies the signal feed. Run both together for the full system.

---

*Built with Claude Code.*

---

## Part of Creative Konsoles

Built by [Creative Konsoles](https://creativekonsoles.com) — tools built using thought.

**[creativekonsoles.com](https://creativekonsoles.com)** &nbsp;·&nbsp; support@creativekonsoles.com

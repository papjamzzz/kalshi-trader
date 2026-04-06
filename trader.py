"""
KK Trader — Autonomous Trading Engine
Background thread that scans, enters, monitors, and exits positions 24/7.

Philosophy:
  - Volume-first: high volume = stability = fair markets
  - Edge-filtered: only trade what KK says is mispriced
  - Direction from price: <45¢ → buy YES, >55¢ → buy NO
  - Moderate risk: configurable faders, daily loss cap, stop loss, take profit
  - No guessing: if signals conflict, skip
"""

import json
import os
import time
import threading
import uuid
import requests
from datetime import datetime, date
from collections import defaultdict

import kalshi_api as kapi
import notifier
try:
    import cross_market
    cross_market.start_background_fetcher()
    CROSS_MARKET_ENABLED = True
except Exception as e:
    print(f"  ⚠ Cross-market disabled: {e}")
    CROSS_MARKET_ENABLED = False

# ── Default Settings (all fader-controlled from UI) ───────────────────────────
DEFAULT_SETTINGS = {
    "max_spend":       10.00,   # $ per trade
    "max_positions":   50,      # 0 = unlimited (stored as 999)
    "daily_loss_cap":  50.00,   # $ before bot stops for the day
    "min_volume":      5000,    # contracts in pool (proxy for liquidity)
    "take_profit_pct": 25.0,    # % gain to exit
    "stop_loss_pct":   20.0,    # % loss to exit
    "min_edge_score":  65.0,    # KK edge score threshold (0–100)
    "scan_interval":   90,      # seconds between scans
    "notify_sms":      True,    # send SMS on trades
    "notify_email":    True,    # send email on trades
}

SETTINGS_FILE = "settings.json"
TRADES_FILE   = "trades.json"
KK_API        = "http://localhost:5555/api/markets"


class TradingEngine:
    def __init__(self):
        self.settings    = self._load_settings()
        self.trades      = self._load_trades()
        self.positions   = {}   # ticker → position dict
        self.running     = False
        self._thread     = None
        self._lock       = threading.Lock()
        self._daily_reset_date = str(date.today())
        self._daily_pnl  = 0.0
        self._wins       = 0
        self._losses     = 0
        self._last_scan  = None
        self._last_scan_count = 0
        self._status_msg = "Idle"

    # ── Persistence ───────────────────────────────────────────────────────────

    def _load_settings(self):
        if os.path.exists(SETTINGS_FILE):
            try:
                with open(SETTINGS_FILE) as f:
                    saved = json.load(f)
                cfg = DEFAULT_SETTINGS.copy()
                cfg.update(saved)
                return cfg
            except Exception:
                pass
        return DEFAULT_SETTINGS.copy()

    def _save_settings(self):
        with open(SETTINGS_FILE, "w") as f:
            json.dump(self.settings, f, indent=2)

    def _load_trades(self):
        if os.path.exists(TRADES_FILE):
            try:
                with open(TRADES_FILE) as f:
                    return json.load(f)
            except Exception:
                pass
        return []

    def _save_trades(self):
        with open(TRADES_FILE, "w") as f:
            json.dump(self.trades[-500:], f, indent=2)  # keep last 500

    def _record_trade(self, trade):
        with self._lock:
            self.trades.append(trade)
            self._save_trades()

    # ── Control ───────────────────────────────────────────────────────────────

    def start(self):
        if self.running:
            return
        self.running = True
        self._status_msg = "Running"
        self._thread = threading.Thread(target=self._engine_loop, daemon=True)
        self._thread.start()
        print("  ✅ KK Trader engine started")
        notifier.notify_startup(self._last_scan_count)

    def stop(self):
        self.running = False
        self._status_msg = "Stopped"
        open_count = len(self.positions)
        print("  ⛔ KK Trader engine stopped")
        notifier.notify_shutdown(self._daily_pnl, len(self.trades), open_count)

    def update_settings(self, new_settings):
        with self._lock:
            self.settings.update(new_settings)
            self._save_settings()

    # ── Main Loop ─────────────────────────────────────────────────────────────

    def _engine_loop(self):
        while self.running:
            try:
                self._daily_check()
                if not self._daily_limit_hit():
                    markets = self._fetch_markets()
                    self._last_scan = datetime.now().isoformat()
                    self._last_scan_count = len(markets)
                    self._scan_entries(markets)
                    self._monitor_positions(markets)
                else:
                    self._status_msg = "Paused — daily limit hit"
            except Exception as e:
                print(f"  ⚠ Engine loop error: {e}")
                self._status_msg = f"Error: {str(e)[:60]}"

            interval = self.settings.get("scan_interval", 90)
            time.sleep(interval)

    def _daily_check(self):
        today = str(date.today())
        if today != self._daily_reset_date:
            self._daily_reset_date = today
            self._daily_pnl = 0.0
            self._wins = 0
            self._losses = 0
            print("  🔄 Daily P&L reset")

    def _daily_limit_hit(self):
        cap = self.settings.get("daily_loss_cap", 50.0)
        return self._daily_pnl <= -cap

    # ── Market Fetching ───────────────────────────────────────────────────────

    # Game-level series to fetch — moneylines, spreads, totals
    # Excludes season-long futures (KXNBA, KXMLB, KXNHL without GAME/SPREAD/TOTAL)
    SPORTS_SERIES = [
        "KXNBAGAME", "KXMLBGAME", "KXNHLGAME",
        "KXNBASPREAD", "KXMLBSPREAD", "KXNHLSPREAD",
        "KXNBATOTAL", "KXMLBTOTAL", "KXNHLTOTAL",
    ]

    def _fetch_sports_direct(self):
        """
        Fetch MLB, NBA, NHL markets directly from Kalshi by series_ticker.
        Normalises price fields to the same format the rest of the bot expects.
        """
        all_markets = []
        for series in self.SPORTS_SERIES:
            try:
                data = kapi._get("/markets", params={
                    "limit": 100,
                    "status": "open",
                    "series_ticker": series,
                })
                raw = data.get("markets", [])
                for m in raw:
                    # Normalise dollar-string prices → integer cents
                    ya = round(float(m.get("yes_ask_dollars") or 0) * 100)
                    yb = round(float(m.get("yes_bid_dollars") or 0) * 100)
                    vol = round(float(m.get("volume_fp") or 0))
                    if ya == 0 and yb == 0:
                        continue   # no orderbook yet — skip
                    all_markets.append({
                        "ticker":        m.get("ticker", ""),
                        "event_ticker":  m.get("event_ticker", ""),
                        "title":         m.get("title", ""),
                        "yes_ask":       ya,
                        "yes_bid":       yb,
                        "volume":        vol,
                        "score":         70,   # base score — cross-market enrichment can raise this
                        "close_time":    m.get("close_time") or m.get("expected_expiration_time"),
                        "category":      "Sports",
                    })
            except Exception as e:
                print(f"  ⚠ Direct sports fetch error ({series}): {e}")
        print(f"  ⚽ {len(all_markets)} sports markets fetched directly")
        return all_markets

    def _fetch_markets(self):
        """
        Try KK first (it may score sports markets with higher signal).
        Always supplement with a direct sports fetch so we never miss game markets.
        """
        kk_markets = []
        try:
            r = requests.get(KK_API, timeout=8)
            if r.status_code == 200:
                data = r.json()
                raw = data if isinstance(data, list) else data.get("markets", [])
                # Only keep sports from KK
                kk_markets = [m for m in raw if any(
                    m.get("ticker", "").startswith(p) for p in self.SPORTS_PREFIXES
                )]
                if kk_markets:
                    print(f"  📡 {len(kk_markets)} sports markets from KK")
        except Exception:
            pass

        # Always do a direct sports fetch — merges with KK, deduplicates by ticker
        direct = self._fetch_sports_direct()
        seen = {m["ticker"] for m in kk_markets}
        for m in direct:
            if m["ticker"] not in seen:
                kk_markets.append(m)
                seen.add(m["ticker"])

        markets = kk_markets
        print(f"  📊 {len(markets)} total sports markets to scan")

        if CROSS_MARKET_ENABLED:
            try:
                markets = cross_market.enrich_markets(markets)
            except Exception as e:
                print(f"  ⚠ Cross-market enrichment error: {e}")

        return markets

    # ── Entry Logic ───────────────────────────────────────────────────────────

    def _scan_entries(self, markets):
        s = self.settings
        max_pos = s.get("max_positions", 50)
        if max_pos == 0:
            max_pos = 999

        current_pos_count = len(self.positions)
        if current_pos_count >= max_pos:
            self._status_msg = f"Position limit reached ({current_pos_count})"
            return

        # Check live balance before entering any positions
        available = kapi.get_balance() / 100.0   # convert cents → dollars
        max_spend = s.get("max_spend", 2.50)
        if available < max_spend:
            self._status_msg = f"Low balance (${available:.2f}) — need ${max_spend:.2f} to trade"
            print(f"  ⚠ Skipping entries — balance ${available:.2f} < max_spend ${max_spend:.2f}")
            return

        # Track entries per event ticker to prevent over-concentration
        MAX_PER_EVENT = 2
        event_entries = {}

        entries_this_scan = 0
        for m in markets:
            if len(self.positions) >= max_pos:
                break
            # Re-check balance before each order (we're spending as we go)
            if available < max_spend:
                print(f"  ⚠ Balance depleted (${available:.2f}), stopping entries")
                break
            # Event diversification guard — max 2 positions per event
            event = m.get("event_ticker", m.get("ticker", "")[:20])
            if event_entries.get(event, 0) >= MAX_PER_EVENT:
                continue
            if self._should_enter(m, s):
                self._enter_position(m, s)
                available -= max_spend   # pessimistic reserve so we don't over-commit
                event_entries[event] = event_entries.get(event, 0) + 1
                entries_this_scan += 1
                time.sleep(0.5)  # rate limit breathing room

    # Game-level markets only — NOT season-long futures (KXNBA-26-PHI etc.)
    # KXNBAGAME, KXMLBGAME, KXNHLGAME = individual game markets
    # KXNBASPREAD, KXMLBSPREAD, KXNHLSPREAD = spreads
    # KXNBATOTAL, KXMLBTOTAL, KXNHLTOTAL = over/unders
    SPORTS_PREFIXES = (
        "KXNBAGAME", "KXMLBGAME", "KXNHLGAME",
        "KXNBASPREAD", "KXMLBSPREAD", "KXNHLSPREAD",
        "KXNBATOTAL", "KXMLBTOTAL", "KXNHLTOTAL",
        "KXNBASTG",   "KXMLBSTG",   "KXNHLSTG",
    )

    def _should_enter(self, m, s):
        """Multi-signal entry filter. ALL conditions must pass."""

        ticker = m.get("ticker", "")
        if not ticker:
            return False

        # Game-level only — block season-long futures like KXNBA-26-PHI
        if not any(ticker.startswith(p) for p in self.SPORTS_PREFIXES):
            return False

        # Already in this market (check both local tracker and live positions)
        if ticker in self.positions:
            return False

        # Edge score check
        score = float(m.get("score", 0))
        if score < s.get("min_edge_score", 65):
            return False

        # Volume check (liquidity guard — insider protection)
        volume = int(m.get("volume", 0))
        if volume < s.get("min_volume", 5000):
            return False

        # Price data
        yes_bid = m.get("yes_bid", 0)
        yes_ask = m.get("yes_ask", 0)
        if not yes_bid or not yes_ask:
            return False

        # Minimum price floor — below 10¢ means stop-loss math breaks down
        # and there's no real liquidity to exit at a meaningful price
        if yes_ask < 10 and (100 - yes_bid) < 10:
            return False

        # Spread check — tighter spread = more liquid = faster repricing
        # Cross-market plays: allow up to 18¢ (still efficient enough)
        spread = yes_ask - yes_bid
        cross_signal = (m.get("cross_edge") or {}).get("signal")
        max_spread = 18 if cross_signal else 25
        if spread > max_spread:
            return False

        # Time to expiry — prefer longer-term positions (min 24h)
        close_time = m.get("close_time") or m.get("expected_expiration_time")
        if close_time:
            try:
                exp = datetime.fromisoformat(close_time.replace("Z", "+00:00"))
                from datetime import timezone
                hours_left = (exp - datetime.now(timezone.utc)).total_seconds() / 3600
                if hours_left < 24:
                    return False
            except Exception:
                pass

        # Direction filter — cross-market signal can expand the entry zone
        cross_signal = (m.get("cross_edge") or {}).get("signal")
        side = self._determine_side(yes_bid, yes_ask, cross_signal)
        if side is None:
            return False

        # If cross-market signal contradicts our direction, skip
        if cross_signal == "YES" and side == "no":
            return False
        if cross_signal == "NO" and side == "yes":
            return False

        return True

    def _determine_side(self, yes_bid, yes_ask, cross_signal=None):
        """
        Determine which side to buy.
        Base rule: YES < 45¢ → buy YES | YES > 55¢ → buy NO | 45–55¢ skip
        With confirmed cross-market signal: expand zone to 48¢ / 52¢.
        This catches markets where external consensus confirms a real edge
        in the 45–50¢ range that Kalshi hasn't priced yet.
        """
        yes_ceil = 48 if cross_signal == "YES" else 45
        no_floor = 52 if cross_signal == "NO"  else 55
        if yes_ask < yes_ceil:
            return "yes"
        if yes_bid > no_floor:
            return "no"
        return None

    def _enter_position(self, m, s):
        ticker  = m.get("ticker", "")
        yes_bid = m.get("yes_bid", 0)
        yes_ask = m.get("yes_ask", 0)
        side    = self._determine_side(yes_bid, yes_ask)
        score   = float(m.get("score", 0))

        if side == "yes":
            entry_price = int(round(yes_ask))   # must be whole cents for Kalshi API
        else:
            entry_price = int(round(100 - yes_bid))

        if entry_price <= 0 or entry_price >= 100:
            return

        max_spend = s.get("max_spend", 10.0)
        count = kapi.contracts_for_spend(max_spend, entry_price)
        if count <= 0:
            return

        actual_cost = (count * entry_price) / 100.0

        print(f"  🟢 ENTERING: {ticker} | {side.upper()} | {count}x @ {entry_price}¢ | ${actual_cost:.2f} | score={score:.1f}")

        try:
            order = kapi.place_order(ticker, side, count, entry_price, action="buy")
            order_id = order.get("order_id") or order.get("id", f"sim-{uuid.uuid4().hex[:8]}")

            position = {
                "ticker":        ticker,
                "side":          side,
                "count":         count,
                "entry_price":   entry_price,
                "entry_cost":    actual_cost,
                "entry_time":    datetime.now().isoformat(),
                "order_id":      order_id,
                "edge_score":    score,
                "title":         m.get("title", ticker),
            }
            with self._lock:
                self.positions[ticker] = position

            trade_entry = {**position, "event": "buy", "pnl": 0}
            self._record_trade(trade_entry)

            if self.settings.get("notify_sms") or self.settings.get("notify_email"):
                notifier.notify_buy(ticker, side, count, entry_price, actual_cost)
            self._status_msg = f"Entered {ticker} {side.upper()} @ {entry_price}¢"

        except Exception as e:
            import traceback
            traceback.print_exc()
            self._status_msg = f"Order error: {str(e)[:60]}"

    # ── Exit Logic ────────────────────────────────────────────────────────────

    def _monitor_positions(self, markets):
        if not self.positions:
            return

        s = self.settings
        take_profit = s.get("take_profit_pct", 25.0)
        stop_loss   = s.get("stop_loss_pct", 20.0)

        # Build market price lookup from current scan
        price_map = {m.get("ticker"): m for m in markets if m.get("ticker")}

        for ticker, pos in list(self.positions.items()):
            current_market = price_map.get(ticker)

            # Fetch fresh price if not in current scan
            if not current_market:
                raw = kapi.get_market(ticker)
                if raw:
                    current_market = raw

            if not current_market:
                continue

            yes_bid = current_market.get("yes_bid", 0)
            yes_ask = current_market.get("yes_ask", 0)
            side    = pos["side"]

            # Current price for our side
            if side == "yes":
                current_price = yes_bid  # can sell YES at the bid
            else:
                current_price = 100 - yes_ask  # NO price = 100 - yes_ask

            pnl_pct = kapi.pnl_pct(pos["entry_price"], current_price)

            # Take profit
            if pnl_pct >= take_profit:
                self._exit_position(ticker, pos, current_price, "take_profit")
                continue

            # Stop loss
            if pnl_pct <= -stop_loss:
                self._exit_position(ticker, pos, current_price, "stop_loss")
                continue

            # Expiry check — close anything expiring in < 2h
            close_time = current_market.get("close_time") or current_market.get("expected_expiration_time")
            if close_time:
                try:
                    exp = datetime.fromisoformat(close_time.replace("Z", "+00:00"))
                    from datetime import timezone
                    hours_left = (exp - datetime.now(timezone.utc)).total_seconds() / 3600
                    if hours_left < 2:
                        reason = "take_profit" if pnl_pct > 0 else "expiry_exit"
                        self._exit_position(ticker, pos, current_price, reason)
                except Exception:
                    pass

    def _exit_position(self, ticker, pos, exit_price, reason):
        side  = pos["side"]
        count = pos["count"]
        entry = pos["entry_price"]

        pnl_pct  = kapi.pnl_pct(entry, exit_price)
        pnl_dollars = ((exit_price - entry) * count) / 100.0

        print(f"  {'🟢' if pnl_dollars >= 0 else '🔴'} EXIT: {ticker} | {reason} | "
              f"entry={entry}¢ exit={exit_price}¢ | pnl=${pnl_dollars:.2f} ({pnl_pct:.1f}%)")

        try:
            kapi.place_order(ticker, side, count, exit_price, action="sell")
        except Exception as e:
            print(f"  ⚠ Exit order error: {ticker} — {e}")

        with self._lock:
            self._daily_pnl += pnl_dollars
            if pnl_dollars >= 0:
                self._wins += 1
            else:
                self._losses += 1
            self.positions.pop(ticker, None)

        trade_record = {
            "ticker":      ticker,
            "side":        side,
            "count":       count,
            "entry_price": entry,
            "exit_price":  exit_price,
            "entry_time":  pos["entry_time"],
            "exit_time":   datetime.now().isoformat(),
            "pnl":         round(pnl_dollars, 4),
            "pnl_pct":     round(pnl_pct, 2),
            "reason":      reason,
            "edge_score":  pos.get("edge_score", 0),
            "event":       "sell",
        }
        self._record_trade(trade_record)

        if self.settings.get("notify_sms") or self.settings.get("notify_email"):
            if pnl_dollars >= 0:
                notifier.notify_profit(
                    ticker, side, count, entry, exit_price,
                    round(pnl_dollars, 2), round(pnl_pct, 1),
                    round(self._daily_pnl, 2), self._wins, self._losses
                )
            else:
                notifier.notify_loss(
                    ticker, side, count, entry, exit_price,
                    round(abs(pnl_dollars), 2), round(abs(pnl_pct), 1),
                    round(self._daily_pnl, 2)
                )

        # Check daily limit after recording
        if self._daily_limit_hit():
            cap = self.settings.get("daily_loss_cap", 50.0)
            if self.settings.get("notify_sms") or self.settings.get("notify_email"):
                notifier.notify_daily_limit(cap, abs(self._daily_pnl))
            self._status_msg = f"Paused — daily limit ${cap:.0f} hit"

    def force_exit(self, ticker):
        """Manually close a position immediately at current market price."""
        with self._lock:
            pos = self.positions.get(ticker)
        if not pos:
            return False, "position not found"
        try:
            raw = kapi.get_market(ticker)
            yes_bid = raw.get("yes_bid", 0) if raw else 0
            yes_ask = raw.get("yes_ask", 0) if raw else 0
            exit_price = yes_bid if pos["side"] == "yes" else (100 - yes_ask)
            if exit_price <= 0:
                exit_price = pos["entry_price"]  # fallback to entry if no price
        except Exception:
            exit_price = pos["entry_price"]
        self._exit_position(ticker, pos, exit_price, "manual_exit")
        return True, f"exited {ticker} @ {exit_price}¢"

    def force_exit_all(self):
        """Close every open position — checks both local tracker and live Kalshi positions."""
        tickers = set(self.positions.keys())

        # Also pull live positions from Kalshi so we catch anything from before restart
        try:
            live = kapi._get("/portfolio/positions")
            for p in live.get("market_positions", []):
                if float(p.get("position_fp", 0)) != 0:
                    ticker = p.get("ticker", "")
                    if ticker and ticker not in tickers:
                        # Rebuild a minimal position dict so _exit_position can work
                        contracts = abs(float(p.get("position_fp", 0)))
                        side = "yes" if float(p.get("position_fp", 0)) > 0 else "no"
                        with self._lock:
                            self.positions[ticker] = {
                                "ticker":      ticker,
                                "side":        side,
                                "count":       int(contracts),
                                "entry_price": 50,   # unknown — use mid as fallback
                                "entry_cost":  float(p.get("total_traded_dollars", 0)),
                                "entry_time":  p.get("last_updated_ts", ""),
                                "order_id":    "recovered",
                                "edge_score":  0,
                                "title":       ticker,
                            }
                        tickers.add(ticker)
        except Exception as e:
            print(f"  ⚠ Live position fetch error: {e}")

        count = len(tickers)
        for ticker in list(tickers):
            self.force_exit(ticker)
        return count

    # ── API Response Helpers ──────────────────────────────────────────────────

    def get_status(self):
        positions_list = []
        for ticker, pos in self.positions.items():
            positions_list.append({
                "ticker":      ticker,
                "side":        pos["side"],
                "count":       pos["count"],
                "entry_price": pos["entry_price"],
                "entry_cost":  pos["entry_cost"],
                "entry_time":  pos["entry_time"],
                "edge_score":  pos.get("edge_score", 0),
                "title":       pos.get("title", ticker),
            })

        return {
            "running":         self.running,
            "status_msg":      self._status_msg,
            "daily_pnl":       round(self._daily_pnl, 4),
            "daily_limit_hit": self._daily_limit_hit(),
            "wins":            self._wins,
            "losses":          self._losses,
            "open_positions":  positions_list,
            "position_count":  len(self.positions),
            "last_scan":       self._last_scan,
            "market_count":    self._last_scan_count,
            "settings":        self.settings,
        }

    def get_trades(self):
        return list(reversed(self.trades[-200:]))  # newest first

    def get_positions(self):
        return list(self.positions.values())

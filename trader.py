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
from concurrent.futures import ThreadPoolExecutor, as_completed
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

try:
    import injury as injury_scanner
    injury_scanner.start()
    INJURY_ENABLED = True
except Exception as e:
    print(f"  ⚠ Injury scanner disabled: {e}")
    INJURY_ENABLED = False

try:
    import clv as clv_tracker
    clv_tracker.start()
    CLV_ENABLED = True
except Exception as e:
    print(f"  ⚠ CLV tracker disabled: {e}")
    CLV_ENABLED = False

# ── 5i Synthesis Engine — final decision gate ─────────────────────────────────
# Tries localhost first (if 5i is running locally), falls back to Railway.
# Only called after all other filters pass — rare, cheap (~$0.017/call).
_5I_LOCAL   = "http://localhost:5562/ask"
_5I_RAILWAY = "https://web-production-94a13.up.railway.app/ask"
_5I_ENABLED = True   # set False to disable without touching _should_enter

# ── Paper trading mode ────────────────────────────────────────────────────────
# When True: all logic runs (signals, 5i votes, P&L tracking) but NO real
# orders are sent to Kalshi. Safe to run indefinitely with zero financial risk.
# Flip to False when the strategy is proven profitable in paper mode.
PAPER_TRADING = True

# ── Default Settings (all fader-controlled from UI) ───────────────────────────
DEFAULT_SETTINGS = {
    "max_spend":       1.00,    # $ per trade — conservative until bot proven
    "max_positions":   5,       # tight cap while rebuilding trust
    "daily_loss_cap":  5.00,    # $ before bot stops for the day
    "min_volume":      500,     # contracts in pool (game markets have lower volume)
    "take_profit_pct": 10.0,    # % gain to exit (achievable intraday on prediction markets)
    "stop_loss_pct":   12.0,    # % loss to exit (wrong is wrong, exit fast)
    "min_edge_score":  75.0,    # higher threshold — cross-market bonus must be real
    "scan_interval":   120,     # seconds between scans
    "notify_sms":      False,   # send SMS on trades
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
        self._last_scan  = None
        self._last_scan_count = 0
        self._status_msg = "Idle"
        self._last_error = None   # UX: persists until next successful scan

        # FIX Bug 2: cooldown after stop-loss — ticker → timestamp of last SL exit
        # No re-entry within STOP_COOLDOWN_HOURS after a stop loss
        self._stopped_out = {}  # type: dict
        STOP_COOLDOWN_HOURS = 24

        # UX: skip-reason counters — surfaced in status so operator knows WHY
        # the bot scanned N markets and entered 0 positions
        self._last_skip_reasons = {}   # e.g. {"no_cross_signal": 12, "price_floor": 3}
        self._last_entries_count = 0

        # Spread penalty attribution — session-level counters reset at daily rollover
        self._spread_blocked    = 0   # markets where spread penalty caused edge_score_low
        self._spread_downgraded = 0   # markets entered despite a spread penalty

        # FIX Bug 7: rebuild daily P&L/wins/losses from trades.json on every startup
        # so the daily loss cap survives restarts
        self._daily_pnl, self._wins, self._losses = self._rebuild_daily_pnl()

        # Markout tracker — measures adverse selection quality of entries
        # Each completed trade stores midpoint changes at +5s, +15s, +60s
        self._markout_executor = ThreadPoolExecutor(max_workers=8, thread_name_prefix="markout")

        # Shared market price cache — written by scan loop, read by monitor loop
        self._latest_markets   = {}
        self._this_cycle_entries = set()

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

    def _rebuild_daily_pnl(self):
        """
        FIX Bug 7: Reconstruct today's P&L from trades.json on startup.
        Without this, restarting the bot resets the daily loss cap to 0,
        meaning a $7 cap can be bypassed by restarting after every $6.90 loss.
        """
        today = str(date.today())
        daily = 0.0
        wins = 0
        losses = 0
        for t in self.trades:
            if t.get("event") != "sell":
                continue
            exit_time = t.get("exit_time", "")
            if not exit_time.startswith(today):
                continue
            pnl = float(t.get("pnl", 0))
            daily += pnl
            if pnl >= 0:
                wins += 1
            else:
                losses += 1
        if daily != 0:
            print(f"  📊 Rebuilt daily P&L from trades: ${daily:.2f} ({wins}W/{losses}L)")
        return daily, wins, losses

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
        self._monitor_thread = threading.Thread(target=self._monitor_loop, daemon=True)
        self._monitor_thread.start()
        print("  ✅ KK Trader engine started (scan=120s / monitor=10s)")
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
                    # FIX Bug 1: track tickers entered THIS cycle so _monitor_loop
                    # doesn't immediately stop-loss them on bid-ask spread in same cycle
                    self._this_cycle_entries = set()
                    self._last_error = None   # clear stale errors on each clean scan cycle
                    markets = self._fetch_markets()
                    self._last_scan = datetime.now().isoformat()
                    self._last_scan_count = len(markets)
                    # Publish latest market prices for the monitor thread to use
                    with self._lock:
                        self._latest_markets = {m.get("ticker"): m for m in markets if m.get("ticker")}
                    self._scan_entries(markets)
                else:
                    self._status_msg = "Paused — daily limit hit"
            except Exception as e:
                print(f"  ⚠ Engine loop error: {e}")
                self._status_msg = f"Error: {str(e)[:60]}"

            interval = self._get_scan_interval()
            time.sleep(interval)

    def _monitor_loop(self):
        """
        Dedicated position monitor — runs every 10s independent of scan cycle.
        Decoupled from _engine_loop so TP/SL fires in seconds, not minutes.
        Uses the latest market price cache updated by the scan loop, and fetches
        directly from Kalshi if a ticker isn't in the cache.
        """
        time.sleep(15)   # let the first scan complete before monitoring starts
        while self.running:
            try:
                if self.positions:
                    with self._lock:
                        price_map = dict(self._latest_markets)
                    self._monitor_positions(price_map)
            except Exception as e:
                print(f"  ⚠ Monitor loop error: {e}")
            time.sleep(10)

    def _daily_check(self):
        today = str(date.today())
        if today != self._daily_reset_date:
            self._daily_reset_date = today
            self._daily_pnl = 0.0
            self._wins = 0
            self._losses = 0
            self._spread_blocked    = 0
            self._spread_downgraded = 0
            print("  🔄 Daily P&L reset")

    def _daily_limit_hit(self):
        if PAPER_TRADING:
            return False   # No daily cap in paper mode — trade freely
        cap = self.settings.get("daily_loss_cap", 50.0)
        return self._daily_pnl <= -cap

    def _get_scan_interval(self):
        """
        Dynamic scan interval based on injury windows and fresh signals.
        Normal: 120s. Injury window: 15s. Fresh OUT/Doubtful: 8s.
        This clusters contract entries around the windows that matter.
        """
        base = self.settings.get("scan_interval", 120)
        if not INJURY_ENABLED:
            return base
        interval = injury_scanner.recommended_scan_interval(base)
        if interval < base:
            window = injury_scanner.active_window_name()
            fresh  = injury_scanner.has_fresh_signal(20)
            tag = "fresh injury" if fresh else f"window:{window}"
            print(f"  ⚡ Fast scan ({interval}s) — {tag}")
        return interval

    # ── Market Fetching ───────────────────────────────────────────────────────

    # Game-level series to fetch — moneylines, spreads, totals
    # Excludes season-long futures (KXNBA, KXMLB, KXNHL without GAME/SPREAD/TOTAL)
    SPORTS_SERIES = [
        "KXNBAGAME", "KXMLBGAME", "KXNHLGAME",
        "KXNBASPREAD", "KXMLBSPREAD", "KXNHLSPREAD",
        "KXNBATOTAL", "KXMLBTOTAL", "KXNHLTOTAL",
    ]

    def _fetch_one_series(self, series):
        """Fetch a single sports series from Kalshi. Called in parallel."""
        try:
            data = kapi._get("/markets", params={
                "limit": 100,
                "status": "open",
                "series_ticker": series,
            })
            raw = data.get("markets", [])
            results = []
            for m in raw:
                ya  = round(float(m.get("yes_ask_dollars") or 0) * 100)
                yb  = round(float(m.get("yes_bid_dollars") or 0) * 100)
                vol = round(float(m.get("volume_fp") or 0))
                if ya == 0 and yb == 0:
                    continue
                results.append({
                    "ticker":       m.get("ticker", ""),
                    "event_ticker": m.get("event_ticker", ""),
                    "title":        m.get("title", ""),
                    "yes_ask":      ya,
                    "yes_bid":      yb,
                    "volume":       vol,
                    "score":        70,
                    "close_time":   m.get("close_time") or m.get("expected_expiration_time"),
                    "category":     "Sports",
                })
            return results
        except Exception as e:
            print(f"  ⚠ Sports fetch error ({series}): {e}")
            return []

    def _fetch_sports_direct(self):
        """
        Fetch all sports series in PARALLEL via ThreadPoolExecutor.
        Serial fetching of 9 series × ~1s each = up to 9s blocking the scan loop.
        Parallel fetching completes in ~1s regardless of series count.
        """
        all_markets = []
        with ThreadPoolExecutor(max_workers=len(self.SPORTS_SERIES)) as pool:
            futures = {pool.submit(self._fetch_one_series, s): s for s in self.SPORTS_SERIES}
            for f in as_completed(futures):
                all_markets.extend(f.result())
        print(f"  ⚽ {len(all_markets)} sports markets fetched (parallel)")
        return all_markets

    # ─────────────────────────────────────────────────────────────────────────
    # TARGET SERIES — direct Kalshi fetches for high-signal short-term markets
    # These are the event series that actually resolve in days/hours, not years.
    # ─────────────────────────────────────────────────────────────────────────
    TARGET_EVENT_PREFIXES = (
        # Weather — daily city temperature & rain, resolves same day / next day
        "KXHIGHNY", "KXHIGHCHI", "KXHIGHMIA", "KXHIGHLAX", "KXHIGHTDAL",
        "KXHIGHTHOU", "KXHIGHTBOS", "KXHIGHTDC", "KXHIGHTPHX", "KXHIGHTSFO",
        "KXHIGHTMIN", "KXHIGHTLV", "KXHIGHTAUS", "KXHIGHTDEN", "KXHIGHTSEA",
        "KXHIGHTATL", "KXHIGHTSATX", "KXHIGHTOKC", "KXHIGHTNOLA", "KXHIGHPHIL",
        "KXLOWTNYC", "KXLOWTCHI", "KXLOWTMIA", "KXLOWTLAX", "KXLOWTDAL",
        "KXLOWTHOU", "KXLOWTBOS", "KXLOWTDC", "KXLOWTPHX", "KXLOWTSFO",
        "KXLOWTMIN", "KXLOWTLV", "KXLOWTAUS", "KXLOWTDEN", "KXLOWTSEA",
        "KXLOWTATL", "KXLOWTSATX", "KXLOWTOKC", "KXLOWTNOLA", "KXLOWTPHIL",
        "KXRAINNYC", "KXRAINCHIM", "KXRAINDALM", "KXRAINAUSM", "KXRAINHOUM",
        "KXRAINLAXM", "KXRAINMIAM", "KXRAINSEAM", "KXRAINSFOM", "KXRAINDENM",
        # Econ — monthly reports, resolves on announcement day
        "KXCPI", "KXCPIYOY", "KXCPICORE", "KXCPICOREYOY",
        "KXECONSTATCPI", "KXECONSTATCPIYOY", "KXECONSTATCPICORE", "KXECONSTATCPICOREYOY",
        "KXECONSTATCORECPIYOY",
        "KXU3", "KXECONSTATU3",
        "KXPCECORE",
        "KXGDP",
        "KXFEDDECISION", "KXFED-",
        # Crypto — daily price range, resolves at 5pm ET same day
        "KXBTC-", "KXBTCD-", "KXBTCE-",
        "KXETH-", "KXETHD-", "KXETHE-",
        "KXSOL-", "KXSOLD-", "KXSOLE-",
        "KXDOGE-", "KXDOGED-",
        "KXXRP-", "KXXRPD-",
    )

    def _fetch_target_markets(self):
        """
        Directly fetch short-term weather/econ/crypto markets from Kalshi events API.
        These are the only markets that actually resolve in hours/days — not years.
        Returns a list of market dicts in the same format as KK's feed.
        """
        from datetime import timezone
        now = datetime.now(timezone.utc)
        markets = []
        seen = set()

        # Fetch all open events, page through until we have enough
        cursor = None
        all_events = []
        for _ in range(15):   # max 15 pages = 3000 events
            params = {"limit": 200, "status": "open"}
            if cursor:
                params["cursor"] = cursor
            try:
                data = kapi._get("/events", params=params)
            except Exception as e:
                print(f"  ⚠ events fetch error: {e}")
                break
            events = data.get("events", [])
            if not events:
                break
            all_events.extend(events)
            cursor = data.get("cursor", "")
            if not cursor:
                break

        # Filter to target event series only
        target_events = []
        for ev in all_events:
            eticker = ev.get("event_ticker", "")
            if any(eticker.startswith(p) for p in self.TARGET_EVENT_PREFIXES):
                target_events.append(eticker)

        print(f"  🎯 {len(target_events)} target events from {len(all_events)} total")

        # Fetch markets for each target event in parallel
        def fetch_event_markets(event_ticker):
            results = []
            try:
                data = kapi._get("/markets", params={"event_ticker": event_ticker, "limit": 50, "status": "open"})
                for m in data.get("markets", []):
                    ticker = m.get("ticker", "")
                    if not ticker or ticker in seen:
                        continue
                    # Only include markets closing within 72h — filters out year-long bets
                    close_time = m.get("close_time") or m.get("expected_expiration_time", "")
                    if close_time:
                        try:
                            exp = datetime.fromisoformat(close_time.replace("Z", "+00:00"))
                            hours_left = (exp - now).total_seconds() / 3600
                            if hours_left < 2 or hours_left > 96:
                                continue
                        except Exception:
                            pass

                    # Normalize to KK-style market dict
                    yes_bid = int(m.get("yes_bid", 0))
                    yes_ask = int(m.get("yes_ask", 0))
                    volume  = int(m.get("volume", 0))
                    if yes_bid == 0 and yes_ask == 0:
                        continue

                    results.append({
                        "ticker":     ticker,
                        "title":      m.get("title", ticker),
                        "yes_bid":    yes_bid,
                        "yes_ask":    yes_ask,
                        "volume":     volume,
                        "score":      72,   # baseline — will be gated by edge/signal filters
                        "close_time": close_time,
                        "_source":    "direct",
                    })
            except Exception:
                pass
            return results

        with ThreadPoolExecutor(max_workers=12) as ex:
            futures = {ex.submit(fetch_event_markets, et): et for et in target_events}
            for fut in as_completed(futures):
                batch = fut.result()
                for m in batch:
                    if m["ticker"] not in seen:
                        seen.add(m["ticker"])
                        markets.append(m)

        print(f"  🌤 {len(markets)} direct target markets fetched")
        return markets

    def _fetch_markets(self):
        """
        Dual-source fetch:
        1. Direct Kalshi events API for target series (weather/econ/crypto, <96h to close)
        2. KK scored feed as supplementary — but filtered to <30 day horizon
        Both sources are cross-market enriched before scanning.
        """
        from datetime import timezone
        now = datetime.now(timezone.utc)

        # ── Source 1: Direct target series ───────────────────────────────────
        direct_markets = self._fetch_target_markets()
        direct_tickers = {m["ticker"] for m in direct_markets}

        # ── Source 2: KK feed — filtered to <30 day horizon ──────────────────
        kk_markets = []
        try:
            r = requests.get(KK_API, timeout=8)
            if r.status_code == 200:
                data = r.json()
                raw = data if isinstance(data, list) else data.get("markets", [])
                for m in raw:
                    if m.get("ticker") in direct_tickers:
                        continue  # already have it from direct fetch
                    # Option B: time horizon filter — skip anything >30 days out
                    close_time = m.get("close_time") or m.get("expected_expiration_time", "")
                    if close_time:
                        try:
                            exp = datetime.fromisoformat(close_time.replace("Z", "+00:00"))
                            days_left = (exp - now).total_seconds() / 86400
                            if days_left > 30:
                                continue
                        except Exception:
                            pass
                    kk_markets.append(m)
                if kk_markets:
                    print(f"  📡 {len(kk_markets)} KK markets (≤30d horizon)")
        except Exception:
            pass

        markets = direct_markets + kk_markets
        print(f"  📊 {len(markets)} total markets to scan")

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

        # Balance check — paper mode uses virtual $1000, live checks real balance
        max_spend = s.get("max_spend", 2.50)
        if PAPER_TRADING:
            available = 1000.0   # virtual paper balance — no real funds needed
        else:
            available = kapi.get_balance() / 100.0
            if available < max_spend:
                self._status_msg = f"Low balance (${available:.2f}) — need ${max_spend:.2f} to trade"
                print(f"  ⚠ Skipping entries — balance ${available:.2f} < max_spend ${max_spend:.2f}")
                return

        # Track entries per event ticker to prevent over-concentration
        MAX_PER_EVENT = 2
        event_entries = {}

        skip_reasons = {}
        entries_this_scan = 0
        for m in markets:
            if len(self.positions) >= max_pos:
                break
            if available < max_spend:
                print(f"  ⚠ Balance depleted (${available:.2f}), stopping entries")
                break
            event = m.get("event_ticker", m.get("ticker", "")[:20])
            if event_entries.get(event, 0) >= MAX_PER_EVENT:
                skip_reasons["event_cap"] = skip_reasons.get("event_cap", 0) + 1
                continue
            if self._should_enter(m, s, skip_reasons=skip_reasons):
                self._enter_position(m, s)
                available -= max_spend
                event_entries[event] = event_entries.get(event, 0) + 1
                entries_this_scan += 1
                time.sleep(0.5)

        # Store skip reasons for dashboard visibility
        self._last_skip_reasons = skip_reasons
        self._last_entries_count = entries_this_scan
        if entries_this_scan == 0 and skip_reasons:
            top = max(skip_reasons, key=skip_reasons.get)
            self._status_msg = f"Scanning — top skip: {top} ({skip_reasons[top]})"

    # Season-long futures — block entirely (0% win rate historically)
    SEASON_PREFIXES = ("KXNBA-", "KXMLB-", "KXNHL-", "KXNFL-", "KXNASCAR-")

    # Game-level sports markets — fine to trade with cross-market signal
    SPORTS_PREFIXES = (
        "KXNBAGAME", "KXMLBGAME", "KXNHLGAME",
        "KXNBASPREAD", "KXMLBSPREAD", "KXNHLSPREAD",
        "KXNBATOTAL", "KXMLBTOTAL", "KXNHLTOTAL",
        "KXNBASTG",   "KXMLBSTG",   "KXNHLSTG",
    )

    STOP_COOLDOWN_HOURS = 24
    _5I_MIN_AGREE      = 3     # require 3/5 models (claude+gpt+grok reliable; gemini/mistral may error)

    def _is_sports_game(self, ticker):
        """True for game-level sports markets (KXNBAGAME-*, etc.) — NOT season futures."""
        return any(ticker.startswith(p) for p in self.SPORTS_PREFIXES)

    def _ask_5i(self, market, cross_signal, ext_prob, gap_pct, sources, inj_signal=None):
        """
        Final decision gate — asks 5i (5-model synthesis engine) whether this
        trade has real edge. Returns True if ≥ _5I_MIN_AGREE models say TRADE.

        Prompt is tightly structured so models answer TRADE or SKIP, not prose.
        Tries localhost:5562 first, falls back to Railway URL.
        Times out at 18s — scan interval is 120s so this is fine.
        """
        if not _5I_ENABLED:
            return True   # bypassed — treat as approved

        title    = market.get("title") or market.get("ticker", "")
        yes_ask  = market.get("yes_ask", 50)
        side     = "YES" if cross_signal == "YES" else "NO"
        buy_prob = yes_ask if cross_signal == "YES" else (100 - yes_ask)

        # Build injury context line if available
        inj_context = ""
        if inj_signal and inj_signal.get("affected"):
            players = ", ".join(
                f"{a['player']} ({a['status']})"
                for a in inj_signal["affected"][:3]
            )
            inj_context = f"Injury context: {players}\n"

        prompt = (
            f'Prediction market trade evaluation.\n\n'
            f'Market: "{title}"\n'
            f'Kalshi price: {buy_prob}¢ (implies {buy_prob}% probability of YES)\n'
            f'External consensus: {round(ext_prob*100)}% from {", ".join(sources)}\n'
            f'Edge gap: {gap_pct:.1f}% — external sources price it {gap_pct:.1f}% '
            f'{"higher" if side == "YES" else "lower"} than Kalshi\n'
            f'{inj_context}'
            f'Proposed trade: BUY {side} at {buy_prob}¢\n\n'
            f'This edge passed volume, spread, cooldown, and direction filters. '
            f'Your job: catch anything those filters miss.\n'
            f'Red flags: stale data mismatch, scope difference (Polymarket covers '
            f'different outcome), market resolving very soon, gap already priced in.\n\n'
            f'First line only: TRADE or SKIP\n'
            f'Second line: one sentence reason.'
        )

        body = {"prompt": prompt, "verdict": True, "models": ["claude", "gpt", "gemini", "grok", "mistral"]}

        for url in [_5I_LOCAL, _5I_RAILWAY]:
            try:
                r = requests.post(url, json=body, timeout=18)
                if not r.ok:
                    continue
                data = r.json()

                # Count how many individual model responses say TRADE
                results = data.get("results") or {}
                trade_count = 0
                skip_count  = 0
                for model, text in results.items():
                    first_word = (text or "").strip().split()[0].upper().rstrip(".,:")
                    if first_word == "TRADE":
                        trade_count += 1
                    elif first_word == "SKIP":
                        skip_count += 1

                verdict = (data.get("verdict") or "").strip()
                verdict_word = verdict.split()[0].upper().rstrip(".:,") if verdict else ""

                approved = trade_count >= self._5I_MIN_AGREE
                verdict_tag = '✅ TRADE' if approved else '❌ SKIP'
                print(
                    f"  🤖 5i: {trade_count}✓/{skip_count}✗ → {verdict_tag} | {title[:55]}"
                )
                # Per-model breakdown — shows which model is the outlier
                for model, text in results.items():
                    first = (text or "").strip().split()[0].upper().rstrip(".,:")
                    reason = (text or "").strip()
                    # Get second line if available
                    lines = [l.strip() for l in (text or "").strip().splitlines() if l.strip()]
                    detail = lines[1][:80] if len(lines) > 1 else lines[0][:80] if lines else ""
                    icon = "  ✓" if first == "TRADE" else "  ✗"
                    print(f"     {icon} {model:<10} {detail}")
                if verdict:
                    print(f"     💬 Synthesis: {verdict[:100]}")
                return approved

            except Exception as e:
                print(f"  ⚠ 5i unreachable ({url.split('/')[2]}): {e}")
                continue

        # Both URLs failed — fail open (don't block trade on infra issue)
        print(f"  ⚠ 5i unavailable — proceeding without synthesis gate")
        return True

    def _should_enter(self, m, s, skip_reasons=None):
        """
        Multi-signal entry filter. ALL conditions must pass.
        skip_reasons: optional dict to accumulate why markets were rejected —
                      surfaced in get_status() so operator can see 'why no entries'.
        """
        def reject(reason):
            if skip_reasons is not None:
                skip_reasons[reason] = skip_reasons.get(reason, 0) + 1
            return False

        ticker = m.get("ticker", "")
        if not ticker:
            return reject("no_ticker")

        # Block ALL sports — season futures AND game markets.
        # Sportsbooks + arb bots price these to near-zero edge. Bot loses here.
        # Real edge lives in: weather, fed/econ, crypto, politics, pop culture.
        ALL_SPORTS = self.SEASON_PREFIXES + self.SPORTS_PREFIXES
        if any(ticker.startswith(p) for p in ALL_SPORTS):
            return reject("sports_blocked")

        # Already in this market
        if ticker in self.positions:
            return reject("already_held")

        # FIX Bug 2: cooldown after stop-loss — block re-entry for 24h
        stopped_at = self._stopped_out.get(ticker)
        if stopped_at:
            hours_since = (time.time() - stopped_at) / 3600
            if hours_since < self.STOP_COOLDOWN_HOURS:
                return reject("stop_loss_cooldown")
            else:
                del self._stopped_out[ticker]

        # FIX Bug 6: require confirmed cross-market signal
        ce = m.get("cross_edge") or {}
        if ce.get("bonus", 0) == 0:
            return reject("no_cross_signal")
        cross_signal = ce.get("signal")
        if cross_signal not in ("YES", "NO"):
            return reject("conflicted_cross_signal")

        # ── Category filter ──────────────────────────────────────────────
        # Focus: weather > econ > crypto (secondary). Block: politics, other.
        # Crypto allowed but requires a tighter spread (max 12¢) to avoid
        # choppy late-entry around volatile price swings.
        category = self._detect_category(ticker, m.get("title", ""))
        m["_category"] = category   # stash so _enter_position can read it

        BLOCKED_CATEGORIES = {"politics", "other", "pop_culture"}
        if category in BLOCKED_CATEGORIES:
            return reject(f"category_blocked_{category}")

        # Edge score check
        score = float(m.get("score", 0))
        if score < s.get("min_edge_score", 65):
            if m.get("spread_penalty"):
                self._spread_blocked += 1   # spread penalty was the deciding factor
            return reject("edge_score_low")

        # Volume check — crypto needs higher liquidity threshold to avoid thin markets
        volume = int(m.get("volume", 0))
        min_vol = s.get("min_volume", 500)
        if category == "crypto":
            min_vol = max(min_vol, 1000)
        if volume < min_vol:
            return reject("low_volume")

        # Price data
        yes_bid = m.get("yes_bid", 0)
        yes_ask = m.get("yes_ask", 0)
        if not yes_bid or not yes_ask:
            return reject("no_price_data")

        # FIX Bug 3+4: minimum price floor
        likely_entry = yes_ask if cross_signal == "YES" else (100 - yes_bid)
        if likely_entry < 15:
            return reject("price_below_floor")

        # Spread quality score — gates hard rejects AND penalizes borderline markets
        # spread_ratio = spread / entry as a % of stop_loss → how much of the buffer is eaten on entry
        spread = yes_ask - yes_bid
        # Crypto gets a tighter spread gate — choppier markets, late entries get burned fast
        if category == "crypto":
            max_spread = 10
        elif cross_signal:
            max_spread = 18
        else:
            max_spread = 25
        if spread > max_spread:
            return reject("spread_too_wide")

        base_sl   = s.get("stop_loss_pct", 20.0)
        entry_p   = likely_entry
        spread_sl_ratio = (spread / entry_p / base_sl * 100) if entry_p > 0 else 100  # % of stop loss consumed

        # Hard reject: spread alone would trigger stop loss on entry
        if spread_sl_ratio >= 100:
            return reject("spread_exceeds_stop_loss")

        # Score penalty: bad spreads eat into signal quality.
        # The 60–100% zone is graduated — 97% of stop is meaningfully worse than 61%.
        # <30%    of SL → no penalty (tight spread, full edge retained)
        # 30–60%  of SL → -7  pts (mild: manageable but worth discounting)
        # 60–75%  of SL → -12 pts (entering near the edge of profitability)
        # 75–90%  of SL → -17 pts (stop loss likely on first adverse tick)
        # 90–100% of SL → -22 pts (barely above hard reject; almost certain early exit)
        if spread_sl_ratio >= 90:
            spread_penalty = -22
        elif spread_sl_ratio >= 75:
            spread_penalty = -17
        elif spread_sl_ratio >= 60:
            spread_penalty = -12
        elif spread_sl_ratio >= 30:
            spread_penalty = -7
        else:
            spread_penalty = 0

        if spread_penalty:
            m["score"] = float(m.get("score", 0)) + spread_penalty
            m["spread_penalty"]   = spread_penalty
            m["spread_sl_ratio"]  = round(spread_sl_ratio, 1)
        else:
            m["spread_sl_ratio"]  = round(spread_sl_ratio, 1)  # always store for winners/losers stat

        # Time to expiry
        close_time = m.get("close_time") or m.get("expected_expiration_time")
        if close_time:
            try:
                exp = datetime.fromisoformat(close_time.replace("Z", "+00:00"))
                from datetime import timezone
                hours_left = (exp - datetime.now(timezone.utc)).total_seconds() / 3600
                if hours_left < 2:
                    return reject("expiring_soon")
            except Exception:
                pass

        # Direction — must align with cross-market signal
        side = self._determine_side(yes_bid, yes_ask, cross_signal)
        if side is None:
            return reject("no_direction")
        if cross_signal == "YES" and side != "yes":
            return reject("direction_mismatch")
        if cross_signal == "NO" and side != "no":
            return reject("direction_mismatch")

        # ── Injury Signal ─────────────────────────────────────────────────────
        # Check if a key player is OUT/Doubtful for this game.
        # Fresh injury (< 30 min) = Kalshi almost certainly hasn't repriced.
        # Boost the score and note it for the 5i prompt.
        inj_signal = {"impact": "none", "boost": 0, "fresh": False, "affected": []}
        if INJURY_ENABLED:
            inj_signal = injury_scanner.get_injury_signal(
                m.get("title", ""), m.get("ticker", "")
            )
            if inj_signal["boost"] > 0:
                old_score = float(m.get("score", 0))
                m["score"] = old_score + inj_signal["boost"]
                m["injury_signal"] = inj_signal
                tag = "🚨 FRESH" if inj_signal["fresh"] else "🏥"
                print(f"  {tag} Injury boost +{inj_signal['boost']}pts → score={m['score']:.0f} | "
                      f"{ticker} | "
                      f"{', '.join(a['player'] + ' ' + a['status'] for a in inj_signal['affected'][:2])}")

        # Re-check edge score after injury boost
        score = float(m.get("score", 0))
        if score < s.get("min_edge_score", 65):
            if m.get("spread_penalty"):
                self._spread_blocked += 1
            return reject("edge_score_low_post_injury")

        # ── 5i Final Gate ─────────────────────────────────────────────────────
        # Only reached if ALL other filters passed. Asks 5 AI models whether
        # the edge is real. Requires ≥ 3/5 to say TRADE. ~$0.017/call.
        # Fresh injury bypasses 5i — speed matters more than AI approval when
        # the window is 5–15 minutes.
        gaps    = [g for _, g in ce.get("gaps", [])]
        sources = ce.get("sources", [])
        ext_prob = (sum(abs(g) for g in gaps) / len(gaps) + yes_ask / 100) if gaps else yes_ask / 100
        gap_pct  = abs(sum(gaps) / len(gaps)) * 100 if gaps else 0

        if inj_signal.get("fresh"):
            print(f"  ⚡ 5i bypassed — fresh injury signal, speed is the edge")
        elif not self._ask_5i(m, cross_signal, ext_prob, gap_pct, sources, inj_signal):
            return reject("5i_rejected")

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

    # ── Category detection ────────────────────────────────────────────────────
    @staticmethod
    def _detect_category(ticker, title=""):
        """Tag each market with a category for P&L tracking."""
        t  = ticker.upper()
        tl = title.lower()

        # ── Ticker-prefix detection (high confidence) ────────────────────────
        # Weather — NOAA daily city temp/rain series (KXHIGH*, KXLOWT*, KXRAIN*)
        WEATHER_PREFIXES = (
            "KXHIGHNY", "KXHIGHCHI", "KXHIGHMIA", "KXHIGHLAX", "KXHIGHTDAL",
            "KXHIGHTHOU", "KXHIGHTBOS", "KXHIGHTDC", "KXHIGHTPHX", "KXHIGHTSFO",
            "KXHIGHTMIN", "KXHIGHTLV", "KXHIGHTAUS", "KXHIGHTDEN", "KXHIGHTSEA",
            "KXHIGHTATL", "KXHIGHTSATX", "KXHIGHTOKC", "KXHIGHTNOLA", "KXHIGHPHIL",
            "KXLOWTNYC", "KXLOWTCHI", "KXLOWTMIA", "KXLOWTLAX", "KXLOWTDAL",
            "KXLOWTHOU", "KXLOWTBOS", "KXLOWTDC", "KXLOWTPHX", "KXLOWTSFO",
            "KXLOWTMIN", "KXLOWTLV", "KXLOWTAUS", "KXLOWTDEN", "KXLOWTSEA",
            "KXLOWTATL", "KXLOWTSATX", "KXLOWTOKC", "KXLOWTNOLA", "KXLOWTPHIL",
            "KXRAINNYC", "KXRAINCHIM", "KXRAINDALM", "KXRAINAUSM", "KXRAINHOUM",
            "KXRAINLAXM", "KXRAINMIAM", "KXRAINSEAM", "KXRAINSFOM", "KXRAINDENM",
            "KXHMONTHRANGE", "KXTROPSTORM",
        )
        if any(t.startswith(p) for p in WEATHER_PREFIXES):
            return "weather"

        # Crypto — daily price range series
        CRYPTO_PREFIXES = (
            "KXBTC-", "KXBTCD-", "KXBTCE-",
            "KXETH-", "KXETHD-", "KXETHE-",
            "KXSOL-", "KXSOLD-", "KXSOLE-",
            "KXDOGE-", "KXDOGED-",
            "KXXRP-", "KXXRPD-",
            "KXBTCMAX", "KXBTCMIN", "KXETHMAX", "KXETHMIN",
            "KXSOLMAX", "KXSOLMIN", "KXDOGEMAX", "KXDOGEMIN", "KXXRPMAX", "KXXRPMIN",
        )
        if any(t.startswith(p) for p in CRYPTO_PREFIXES):
            return "crypto"

        # Econ — CPI, jobs, Fed, GDP report series
        ECON_PREFIXES = (
            "KXCPI", "KXECONSTATCPI", "KXECONSTATCORECPI", "KXCPICOREYOY", "KXCPIYOY",
            "KXU3", "KXECONSTATU3",
            "KXPCECORE",
            "KXGDP", "KXNGDPQ",
            "KXFEDDECISION", "KXFEDCOMBO", "KXFOMCDISSENT",
            "KXUE-", "KXARMOMINF", "KXJPMOMINF", "KXCACPIYOY", "KXUKCPIYOY",
            "KXDEGDP", "KXESGDP", "KXEZGDP", "KXFRGDP", "KXITGDP",
            "KXTARIFFRATE", "KXSHELTERCPI", "KXUSGASCPI", "KXUSEDCARCPI",
            "KXAIRFARECPI", "KXTOBACCPI",
        )
        if any(t.startswith(p) for p in ECON_PREFIXES):
            return "econ"

        # ── Title-keyword fallback ────────────────────────────────────────────
        if any(w in tl for w in ["highest temperature", "lowest temperature", "will it rain", "inches of rain",
                                   "rainfall", "snowfall", "precipitation", "hurricane", "tornado", "wildfire"]):
            return "weather"
        if any(w in tl for w in ["bitcoin price", "ethereum price", "btc price", "eth price",
                                   "solana price", "dogecoin price", "xrp price", "ripple price"]):
            return "crypto"
        if any(w in tl for w in ["cpi", "inflation rate", "unemployment rate", "fed funds rate",
                                   "fomc", "interest rate", "gdp growth", "nonfarm payroll", "pce"]):
            return "econ"
        if any(x in t for x in ("KXPOL", "KXELECT", "KXGOV", "KXCONGRESS", "KXPRES", "KXSENATE", "KXSCOTUS")):
            return "politics"
        if any(w in tl for w in ["president", "congress", "senate", "election", "vote", "supreme court",
                                   "legislation", "bill passes", "executive order"]):
            return "politics"
        if any(w in tl for w in ["oscar", "grammy", "emmy", "box office", "album", "chart", "music video"]):
            return "pop_culture"
        return "other"

    # ── Dynamic position sizing ───────────────────────────────────────────────
    @staticmethod
    def _calc_position_size(base_spend, cross_edge):
        """
        Scale position size based on signal conviction.
        More sources + bigger gap = bigger bet, up to 3x base.

        Multiplier breakdown:
          Sources: 1 → 1.0x | 2 → 1.5x | 3+ → 2.0x
          Gap:     7-10% → +0.0 | 10-15% → +0.25 | 15-20% → +0.5 | 20%+ → +1.0
          Cap: 3.0x max
        """
        gaps      = [abs(g) for _, g in (cross_edge.get("gaps") or [])]
        n_sources = len(cross_edge.get("sources") or [])
        avg_gap   = sum(gaps) / len(gaps) if gaps else 0

        source_mult = 1.0 + max(0, n_sources - 1) * 0.5

        if avg_gap >= 0.20:
            gap_bonus = 1.0
        elif avg_gap >= 0.15:
            gap_bonus = 0.5
        elif avg_gap >= 0.10:
            gap_bonus = 0.25
        else:
            gap_bonus = 0.0

        multiplier  = min(3.0, source_mult + gap_bonus)
        sized_spend = round(base_spend * multiplier, 2)
        return sized_spend, multiplier

    def _enter_position(self, m, s):
        ticker       = m.get("ticker", "")
        yes_bid      = m.get("yes_bid", 0)
        yes_ask      = m.get("yes_ask", 0)
        cross_signal = (m.get("cross_edge") or {}).get("signal")
        cross_edge   = m.get("cross_edge") or {}
        side  = self._determine_side(yes_bid, yes_ask, cross_signal)
        score = float(m.get("score", 0))
        category = m.get("_category") or self._detect_category(ticker, m.get("title", ""))

        if side == "yes":
            entry_price = int(round(yes_ask))
        else:
            entry_price = int(round(100 - yes_bid))

        if entry_price <= 0 or entry_price >= 100:
            return

        # Dynamic position sizing — scales with conviction
        base_spend          = s.get("max_spend", 10.0)
        sized_spend, mult   = self._calc_position_size(base_spend, cross_edge)
        count = kapi.contracts_for_spend(sized_spend, entry_price)
        if count <= 0:
            return

        actual_cost = (count * entry_price) / 100.0
        sources_str = "+".join(cross_edge.get("sources", [])) or "none"

        mode_tag = "📋 PAPER" if PAPER_TRADING else "🟢 LIVE"
        print(f"  {mode_tag} ENTERING: {ticker} | {side.upper()} | {count}x @ {entry_price}¢ | "
              f"${actual_cost:.2f} ({mult:.1f}x) | score={score:.1f} | cat={category} | src={sources_str}")

        try:
            if PAPER_TRADING:
                order_id = f"paper-{uuid.uuid4().hex[:8]}"
            else:
                order = kapi.place_order(ticker, side, count, entry_price, action="buy")
                order_id = order.get("order_id") or order.get("id", f"sim-{uuid.uuid4().hex[:8]}")

            spread_penalty   = m.get("spread_penalty", 0)
            spread_sl_ratio  = m.get("spread_sl_ratio", 0.0)
            if spread_penalty:
                self._spread_downgraded += 1

            entry_mid = (yes_bid + yes_ask) / 2.0

            # Compute market remaining life at entry — used by proportional time stop
            secs_to_close_at_entry = None
            close_time = m.get("close_time") or m.get("expected_expiration_time")
            if close_time:
                try:
                    from datetime import timezone as _tz
                    exp = datetime.fromisoformat(close_time.replace("Z", "+00:00"))
                    secs_to_close_at_entry = (exp - datetime.now(_tz.utc)).total_seconds()
                except Exception:
                    pass

            position = {
                "ticker":                  ticker,
                "side":                    side,
                "count":                   count,
                "entry_price":             entry_price,
                "entry_mid":               round(entry_mid, 2),
                "entry_cost":              actual_cost,
                "entry_time":              datetime.now().isoformat(),
                "order_id":                order_id,
                "edge_score":              score,
                "title":                   m.get("title", ticker),
                "category":                category,
                "size_mult":               mult,
                "sources":                 cross_edge.get("sources", []),
                "peak_pnl_pct":            0.0,
                "peak_favorable":          0.0,
                "worst_adverse":           0.0,
                "markout":                 {},
                "spread_penalty":          spread_penalty,
                "spread_sl_ratio":         spread_sl_ratio,
                "secs_to_close_at_entry":  secs_to_close_at_entry,
            }
            with self._lock:
                self.positions[ticker] = position

            # Spawn markout tracker — runs in background, polls at +5/15/60s
            self._markout_executor.submit(self._track_markout, ticker, entry_mid, side)

            # FIX Bug 1: mark as entered this cycle so _monitor_positions
            # doesn't evaluate it for stop-loss before the market can reprice
            if hasattr(self, "_this_cycle_entries"):
                self._this_cycle_entries.add(ticker)

            trade_entry = {**position, "event": "buy", "pnl": 0}
            self._record_trade(trade_entry)

            # CLV: record entry now, resolve closing price near market close
            if CLV_ENABLED:
                inj = m.get("injury_signal", {})
                if inj.get("fresh"):
                    sig_type = "injury_fresh"
                elif inj.get("boost", 0) > 0:
                    sig_type = "injury_boosted"
                else:
                    sig_type = "cross_market"
                close_time = m.get("close_time") or m.get("expected_expiration_time") or ""
                clv_tracker.record_entry(
                    ticker, side, entry_price, close_time,
                    title=m.get("title", ticker),
                    signal_type=sig_type,
                )

            if self.settings.get("notify_sms") or self.settings.get("notify_email"):
                notifier.notify_buy(ticker, side, count, entry_price, actual_cost)
            self._status_msg = f"Entered {ticker} {side.upper()} @ {entry_price}¢"

        except Exception as e:
            import traceback
            traceback.print_exc()
            self._status_msg = f"Order error: {str(e)[:60]}"

    # ── Markout Tracker ───────────────────────────────────────────────────────

    def _track_markout(self, ticker, entry_mid, side):
        """
        Background thread: records midpoint at +5s, +15s, +60s after entry.
        Measures adverse selection — did the market move for us or against us
        immediately after we entered?  Positive markout = market agreed with us.
        """
        checkpoints = [5, 15, 60]
        prev = 0
        for secs in checkpoints:
            time.sleep(secs - prev)
            prev = secs
            try:
                raw = kapi.get_market(ticker)
                if not raw:
                    continue
                bid = raw.get("yes_bid", 0)
                ask = raw.get("yes_ask", 0)
                if not bid or not ask:
                    continue
                mid = (bid + ask) / 2.0
                # Markout in cents from our perspective:
                # YES position  — want mid to rise   (+mid_change = good)
                # NO  position  — want mid to fall   (-mid_change = good)
                if side == "yes":
                    mo = mid - entry_mid
                else:
                    mo = entry_mid - mid
                with self._lock:
                    if ticker in self.positions:
                        self.positions[ticker].setdefault("markout", {})[f"s{secs}"] = round(mo, 2)
                        # Track peak favorable and worst adverse mid-moves
                        pos = self.positions[ticker]
                        if mo > pos.get("peak_favorable", 0.0):
                            pos["peak_favorable"] = round(mo, 2)
                        if mo < pos.get("worst_adverse", 0.0):
                            pos["worst_adverse"] = round(mo, 2)
            except Exception:
                pass

    # ── Exit Logic ────────────────────────────────────────────────────────────

    def _monitor_positions(self, price_map):
        """
        Called by _monitor_loop every 10s with a pre-built {ticker: market} dict.
        Checks TP / trailing stop / SL / proportional time stop / expiry window.
        """
        if not self.positions:
            return

        s = self.settings
        take_profit = s.get("take_profit_pct", 10.0)   # lowered: 10% is achievable intraday
        _base_sl    = s.get("stop_loss_pct", 12.0)     # tighter: wrong is wrong, exit fast

        from datetime import timezone as _tz

        for ticker, pos in list(self.positions.items()):
            # Grace period: skip tickers entered this scan cycle (bid-ask spread protection)
            if hasattr(self, "_this_cycle_entries") and ticker in self._this_cycle_entries:
                continue

            # Hard 3-minute grace period (handles restarts too)
            entry_time_str = pos.get("entry_time", "")
            secs_held = 0
            if entry_time_str:
                try:
                    entered_at = datetime.fromisoformat(entry_time_str)
                    secs_held  = (datetime.now() - entered_at).total_seconds()
                    if secs_held < 180:
                        continue
                except Exception:
                    pass

            current_market = price_map.get(ticker)

            # Fetch fresh price if not in latest scan cache
            if not current_market:
                raw = kapi.get_market(ticker)
                if raw:
                    current_market = raw

            if not current_market:
                if PAPER_TRADING:
                    entry_p = pos["entry_price"]
                    cat = pos.get("category", "other")
                    print(f"  🗑 dead_market_expire: {ticker} [{cat}] — market gone, closing at entry ({entry_p}¢)")
                    self._exit_position(ticker, pos, entry_p, "dead_market_expire")
                continue

            yes_bid = current_market.get("yes_bid", 0)
            yes_ask = current_market.get("yes_ask", 0)
            side    = pos["side"]

            current_price = yes_bid if side == "yes" else (100 - yes_ask)
            pnl_pct = kapi.pnl_pct(pos["entry_price"], current_price)

            is_sports = self._is_sports_game(ticker)
            stop_loss = _base_sl * 1.4 if is_sports else _base_sl

            # ── Take profit ───────────────────────────────────────────────────
            if pnl_pct >= take_profit:
                self._exit_position(ticker, pos, current_price, "take_profit")
                continue

            # ── Trailing stop: protect gains ≥ 6% — don't give back more than half ──
            peak = pos.get("peak_pnl_pct", 0.0)
            if pnl_pct > peak:
                with self._lock:
                    self.positions[ticker]["peak_pnl_pct"] = pnl_pct
                peak = pnl_pct
            if peak >= 6.0 and pnl_pct < (peak * 0.45):
                self._exit_position(ticker, pos, current_price, "trailing_stop")
                continue

            # ── Stop loss ─────────────────────────────────────────────────────
            if pnl_pct <= -stop_loss:
                self._exit_position(ticker, pos, current_price, "stop_loss")
                continue

            # ── Proportional time stop ────────────────────────────────────────
            # If we've used >40% of the market's remaining life at entry and P&L
            # is stuck flat (-5% to +3%), the edge has likely already been priced
            # in or was never real. Exit and free the capital.
            # NOT applied to sports (noisy by nature) or markets <30min to close
            # (the expiry window below handles those).
            close_time = current_market.get("close_time") or current_market.get("expected_expiration_time")
            if close_time and not is_sports and secs_held > 180:
                try:
                    exp = datetime.fromisoformat(close_time.replace("Z", "+00:00"))
                    now_utc = datetime.now(_tz.utc)
                    secs_to_close  = (exp - now_utc).total_seconds()
                    life_at_entry  = pos.get("secs_to_close_at_entry")   # stored on entry

                    if life_at_entry and life_at_entry > 1800:   # only for markets >30min horizon
                        pct_life_used = secs_held / life_at_entry
                        is_stuck = -5.0 <= pnl_pct <= 3.0
                        if pct_life_used >= 0.40 and is_stuck:
                            cat = pos.get("category", "")
                            print(
                                f"  ⏱ time_stop: {ticker} [{cat}] — "
                                f"{pct_life_used*100:.0f}% of market life used, "
                                f"P&L stuck at {pnl_pct:+.1f}%"
                            )
                            self._exit_position(ticker, pos, current_price, "time_stop")
                            continue
                except Exception:
                    pass

            # ── Expiry window exit ────────────────────────────────────────────
            # Sports: exit 1h before close (avoid in-game volatility)
            # All others: exit 2h before close (lock in drift, avoid resolution noise)
            if close_time:
                try:
                    exp = datetime.fromisoformat(close_time.replace("Z", "+00:00"))
                    hours_left = (exp - datetime.now(_tz.utc)).total_seconds() / 3600
                    exit_window = 1.0 if is_sports else 2.0
                    if hours_left < exit_window:
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

        mode = "📋" if PAPER_TRADING else ("🟢" if pnl_dollars >= 0 else "🔴")
        print(f"  {mode} EXIT{'[PAPER]' if PAPER_TRADING else ''}: {ticker} | {reason} | "
              f"entry={entry}¢ exit={exit_price}¢ | pnl=${pnl_dollars:.2f} ({pnl_pct:.1f}%)")

        # FIX Bug 2: record stop-loss cooldown so we don't re-enter the same
        # losing market every 120 seconds until the daily cap hits
        if reason == "stop_loss":
            self._stopped_out[ticker] = time.time()

        # FIX Bug 9: verify exit order fills — don't silently mark closed if
        # the limit order rests unfilled in a thin market
        exit_filled = False
        try:
            if PAPER_TRADING:
                exit_filled = True   # paper: always "filled" instantly
                order_id = ""
            else:
                order = kapi.place_order(ticker, side, count, exit_price, action="sell")
                order_id = order.get("order_id") or order.get("id", "")
            if order_id:
                time.sleep(2)   # give Kalshi matching engine time to fill
                order_status = kapi.get_order_status(order_id)
                exit_filled = order_status.get("status") == "executed"
                if not exit_filled:
                    # Try 3¢ lower to increase fill probability
                    kapi.cancel_order(order_id)
                    fire_price = max(1, exit_price - 3)
                    order2 = kapi.place_order(ticker, side, count, fire_price, action="sell")
                    order_id2 = order2.get("order_id") or order2.get("id", "")
                    if order_id2:
                        time.sleep(2)
                        s2 = kapi.get_order_status(order_id2)
                        exit_filled = s2.get("status") == "executed"
                    if not exit_filled:
                        msg = f"Exit order RESTING unfilled: {ticker} — check Kalshi"
                        print(f"  ⚠ {msg}")
                        self._last_error = msg   # UX: persists on dashboard until cleared
            else:
                exit_filled = True  # no order_id = likely executed inline
        except Exception as e:
            err_msg = f"Exit order error: {ticker} — {str(e)[:80]}"
            print(f"  ⚠ {err_msg}")
            self._last_error = err_msg

        with self._lock:
            self._daily_pnl += pnl_dollars
            if pnl_dollars >= 0:
                self._wins += 1
            else:
                self._losses += 1
            self.positions.pop(ticker, None)

        trade_record = {
            "ticker":          ticker,
            "side":            side,
            "count":           count,
            "entry_price":     entry,
            "exit_price":      exit_price,
            "entry_time":      pos["entry_time"],
            "exit_time":       datetime.now().isoformat(),
            "pnl":             round(pnl_dollars, 4),
            "pnl_pct":         round(pnl_pct, 2),
            "reason":          reason,
            "edge_score":      pos.get("edge_score", 0),
            "category":        pos.get("category", "other"),
            "spread_penalty":  pos.get("spread_penalty", 0),
            "spread_sl_ratio": pos.get("spread_sl_ratio", 0.0),
            "event":           "sell",
            # Markout data — adverse selection analysis
            "entry_mid":       pos.get("entry_mid", 0),
            "markout":         pos.get("markout", {}),
            "peak_favorable":  pos.get("peak_favorable", 0.0),
            "worst_adverse":   pos.get("worst_adverse", 0.0),
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
                        total_traded = float(p.get("total_traded_dollars") or 0)

                        # FIX Bug 8: compute real avg entry price from Kalshi data
                        # total_traded_dollars / contracts = avg cost per contract
                        # Multiply by 100 to convert dollars → cents
                        if contracts > 0 and total_traded > 0:
                            real_entry_cents = int(round((total_traded / contracts) * 100))
                        else:
                            real_entry_cents = 50   # last resort — logged below
                            print(f"  ⚠ force_exit_all: no cost data for {ticker}, using 50¢ fallback")

                        with self._lock:
                            self.positions[ticker] = {
                                "ticker":      ticker,
                                "side":        side,
                                "count":       int(contracts),
                                "entry_price": real_entry_cents,
                                "entry_cost":  total_traded,
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
        # Build positions list with live unrealized P&L
        positions_list = []
        for ticker, pos in self.positions.items():
            entry = pos["entry_price"]
            side  = pos["side"]
            count = pos["count"]
            # Fetch current price for unrealized P&L — non-blocking, cached by session
            try:
                raw = kapi.get_market(ticker)
                yes_bid = raw.get("yes_bid", entry)
                yes_ask = raw.get("yes_ask", entry)
                current = yes_bid if side == "yes" else (100 - yes_ask)
            except Exception:
                current = entry
            unreal_pnl = round(((current - entry) * count) / 100.0, 4)
            unreal_pct = round(kapi.pnl_pct(entry, current), 1) if entry > 0 else 0

            positions_list.append({
                "ticker":       ticker,
                "side":         side,
                "count":        count,
                "entry_price":  entry,
                "entry_cost":   pos["entry_cost"],
                "entry_time":   pos["entry_time"],
                "edge_score":   pos.get("edge_score", 0),
                "title":        pos.get("title", ticker),
                "current_price": current,
                "unreal_pnl":   unreal_pnl,
                "unreal_pct":   unreal_pct,
            })

        # Cooldown list — tickers blocked from re-entry and how long until clear
        now = time.time()
        cooldowns = []
        for ticker, ts in list(self._stopped_out.items()):
            hours_left = self.STOP_COOLDOWN_HOURS - (now - ts) / 3600
            if hours_left > 0:
                cooldowns.append({"ticker": ticker, "hours_left": round(hours_left, 1)})
            else:
                del self._stopped_out[ticker]

        # Cross-market data freshness (from cross_market module)
        cm_freshness = {}
        try:
            import cross_market
            import fedwatch
            import noaa
            age_pm  = round((now - cross_market._pm_ts) / 60, 1) if cross_market._pm_ts else None
            age_pi  = round((now - cross_market._pi_ts) / 60, 1) if cross_market._pi_ts else None
            import ndfd as ndfd_mod
            import econ_signals as econ_mod
            import coingecko as cg_mod
            econ_status = econ_mod.status_summary()
            cg_status   = cg_mod.status_summary()
            cm_freshness = {
                "polymarket_age_min":  age_pm,
                "polymarket_count":    len(cross_market._pm_markets),
                "predictit_age_min":   age_pi,
                "predictit_count":     len(cross_market._pi_markets),
                "fedwatch_meetings":   len(fedwatch._cache),
                "noaa_cities":         len(noaa._forecasts),
                "ndfd_cities":         len(ndfd_mod._hourly),
                "econ_series":         econ_status.get("series_loaded", 0),
                "econ_gdpnow":         econ_status.get("gdpnow"),
                "cg_btc":              cg_status.get("btc_price"),
                "cg_eth":              cg_status.get("eth_price"),
                "cg_coins":            cg_status.get("coins_loaded", 0),
                "cg_age_min":          cg_status.get("age_min"),
            }
        except Exception:
            pass

        # Category P&L breakdown — win rate per market type
        cat_stats = {}
        for t in self.trades:
            if t.get("event") != "sell":
                continue
            cat = t.get("category", "other")
            pnl = float(t.get("pnl", 0))
            if cat not in cat_stats:
                cat_stats[cat] = {"wins": 0, "losses": 0, "pnl": 0.0}
            cat_stats[cat]["pnl"] = round(cat_stats[cat]["pnl"] + pnl, 4)
            if pnl >= 0:
                cat_stats[cat]["wins"] += 1
            else:
                cat_stats[cat]["losses"] += 1

        # Spread penalty attribution — did spread quality correlate with outcome?
        spread_attr = {
            "blocked_today":     self._spread_blocked,
            "downgraded_entered": self._spread_downgraded,
            "avg_ratio_winners":  None,
            "avg_ratio_losers":   None,
        }
        winner_ratios = [
            float(t.get("spread_sl_ratio", 0))
            for t in self.trades
            if t.get("event") == "sell" and float(t.get("pnl", 0)) >= 0
            and t.get("spread_sl_ratio") is not None
        ]
        loser_ratios = [
            float(t.get("spread_sl_ratio", 0))
            for t in self.trades
            if t.get("event") == "sell" and float(t.get("pnl", 0)) < 0
            and t.get("spread_sl_ratio") is not None
        ]
        if winner_ratios:
            spread_attr["avg_ratio_winners"] = round(sum(winner_ratios) / len(winner_ratios), 1)
        if loser_ratios:
            spread_attr["avg_ratio_losers"] = round(sum(loser_ratios) / len(loser_ratios), 1)

        # Dead-market expiry stats — tracks how often the bot held a market
        # that silently vanished (API returns None). Bucketed by category so we
        # can spot which market types go dark most often and tune data sources.
        dead_stats = {"total": 0, "by_category": {}}
        for t in self.trades:
            if t.get("reason") != "dead_market_expire":
                continue
            dead_stats["total"] += 1
            cat = t.get("category", "other")
            dead_stats["by_category"][cat] = dead_stats["by_category"].get(cat, 0) + 1

        # CLV summary — the strategy health indicator
        clv_summary = {}
        if CLV_ENABLED:
            try:
                clv_summary = clv_tracker.get_summary()
            except Exception:
                pass

        # Injury scanner status
        injury_status = {}
        if INJURY_ENABLED:
            try:
                injury_status = injury_scanner.status_summary()
            except Exception:
                pass

        # Markout summary — average midpoint drift at each checkpoint
        closed_sells = [t for t in self.trades if t.get("event") == "sell" and t.get("markout")]
        def _avg_markout(key):
            vals = [t["markout"][key] for t in closed_sells if key in t.get("markout", {})]
            return round(sum(vals) / len(vals), 2) if vals else None
        def _avg_field(field):
            vals = [float(t.get(field, 0)) for t in closed_sells if t.get(field) is not None]
            return round(sum(vals) / len(vals), 2) if vals else None

        markout_summary = {
            "n":                 len(closed_sells),
            "avg_5s":            _avg_markout("s5"),
            "avg_15s":           _avg_markout("s15"),
            "avg_60s":           _avg_markout("s60"),
            "avg_peak_favorable": _avg_field("peak_favorable"),
            "avg_worst_adverse":  _avg_field("worst_adverse"),
        }

        return {
            "running":          self.running,
            "paper_trading":    PAPER_TRADING,
            "status_msg":       self._status_msg,
            "last_error":       self._last_error,
            "daily_pnl":        round(self._daily_pnl, 4),
            "daily_limit_hit":  self._daily_limit_hit(),
            "wins":             self._wins,
            "losses":           self._losses,
            "open_positions":   positions_list,
            "position_count":   len(self.positions),
            "last_scan":        self._last_scan,
            "market_count":     self._last_scan_count,
            "settings":         self.settings,
            "skip_reasons":     self._last_skip_reasons,
            "last_entries":     self._last_entries_count,
            "cooldowns":        cooldowns,
            "cross_market":     cm_freshness,
            "category_pnl":     cat_stats,
            "spread_attribution": spread_attr,
            "dead_market_stats":  dead_stats,
            "clv":              clv_summary,
            "injury":           injury_status,
            "markout":          markout_summary,
        }

    def get_trades(self):
        return list(reversed(self.trades[-200:]))  # newest first

    def get_positions(self):
        return list(self.positions.values())

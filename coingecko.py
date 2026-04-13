"""
CoinGecko — Real-time crypto prices for Kalshi crypto market edge.

Free tier API (no key required): ~10-30 req/min.
Demo key (free): up to 30 req/min with headers. Set COINGECKO_API_KEY in .env.

Data pulled per coin:
  - Current USD price
  - 24h price change %
  - 24h volume
  - Market cap
  - 24h high / 24h low

Kalshi crypto markets we target:
  KXBTC-*    — Bitcoin daily close above/below threshold
  KXBTCD-*   — Bitcoin high/low (daily range)
  KXETH-*    — Ethereum close
  KXETHD-*   — Ethereum high/low
  KXSOL-*    — Solana close
  KXSOLD-*   — Solana high/low
  KXDOGE-*   — Dogecoin
  KXXRP-*    — XRP / Ripple
  KXBTCMAX/MIN, KXETHMAX/MIN — weekly/monthly extremes

Probability model:
  Binary "will price be ABOVE/BELOW X at close" estimated from:
    1. Current price vs threshold (primary signal)
    2. 24h range — if high already cleared threshold → YES almost certain
    3. Momentum (24h % change direction)
    4. Distance-based curve calibrated for same-day resolution

Refresh: every 3 minutes — crypto is volatile, stale data is dangerous.
"""

import os
import re
import time
import threading
import requests
from datetime import datetime, timezone

_session = requests.Session()

REFRESH_TTL   = 180     # 3 minutes — crypto moves fast
_lock         = threading.Lock()
_prices       = {}      # coin_id → { price, change_24h, high_24h, low_24h, volume, market_cap, ts }
_last_fetch   = 0.0

# ── Coin registry ─────────────────────────────────────────────────────────────
# Maps internal coin_id (CoinGecko) → metadata for Kalshi ticker matching

COINS = {
    "bitcoin": {
        "symbol": "btc",
        "ticker_patterns": ["KXBTC-", "KXBTCD-", "KXBTCE-", "KXBTCMAX", "KXBTCMIN"],
        "title_kws": ["bitcoin", "btc"],
    },
    "ethereum": {
        "symbol": "eth",
        "ticker_patterns": ["KXETH-", "KXETHD-", "KXETHE-", "KXETHMAX", "KXETHMIN"],
        "title_kws": ["ethereum", "eth "],
    },
    "solana": {
        "symbol": "sol",
        "ticker_patterns": ["KXSOL-", "KXSOLD-", "KXSOLE-", "KXSOLMAX", "KXSOLMIN"],
        "title_kws": ["solana", " sol "],
    },
    "dogecoin": {
        "symbol": "doge",
        "ticker_patterns": ["KXDOGE-", "KXDOGED-", "KXDOGEMAX", "KXDOGEMIN"],
        "title_kws": ["dogecoin", "doge"],
    },
    "ripple": {
        "symbol": "xrp",
        "ticker_patterns": ["KXXRP-", "KXXRPD-", "KXXRPMAX", "KXXRPMIN"],
        "title_kws": ["xrp", "ripple"],
    },
    "cardano": {
        "symbol": "ada",
        "ticker_patterns": ["KXADA-", "KXADAD-"],
        "title_kws": ["cardano", " ada "],
    },
    "avalanche-2": {
        "symbol": "avax",
        "ticker_patterns": ["KXAVAX-"],
        "title_kws": ["avalanche", "avax"],
    },
    "chainlink": {
        "symbol": "link",
        "ticker_patterns": ["KXLINK-"],
        "title_kws": ["chainlink", " link "],
    },
    "litecoin": {
        "symbol": "ltc",
        "ticker_patterns": ["KXLTC-"],
        "title_kws": ["litecoin", " ltc "],
    },
    "the-open-network": {
        "symbol": "ton",
        "ticker_patterns": ["KXTON-"],
        "title_kws": [" ton "],
    },
}

COIN_IDS = list(COINS.keys())


# ── API fetch ─────────────────────────────────────────────────────────────────

def _build_headers():
    headers = {
        "User-Agent": "KKTrader/1.0 (kalshi crypto; underwaterfile@proton.me)",
        "Accept":     "application/json",
    }
    key = os.getenv("COINGECKO_API_KEY", "")
    if key:
        headers["x-cg-demo-api-key"] = key
    return headers


def _fetch_all():
    """Fetch market data for all tracked coins from CoinGecko /coins/markets."""
    global _last_fetch
    ids_str = ",".join(COIN_IDS)
    url = (
        "https://api.coingecko.com/api/v3/coins/markets"
        f"?vs_currency=usd&ids={ids_str}"
        "&order=market_cap_desc&per_page=50&page=1"
        "&sparkline=false&price_change_percentage=24h"
    )
    try:
        r = requests.get(url, headers=_build_headers(), timeout=10)
        if r.status_code == 429:
            print("  [CoinGecko] Rate limited — will retry next cycle")
            return
        if not r.ok:
            print(f"  [CoinGecko] HTTP {r.status_code}")
            return

        data = r.json()
        now  = time.time()
        fresh = {}
        for c in data:
            cid = c.get("id", "")
            if cid not in COINS:
                continue
            fresh[cid] = {
                "price":       c.get("current_price"),
                "change_24h":  c.get("price_change_percentage_24h") or 0.0,
                "high_24h":    c.get("high_24h"),
                "low_24h":     c.get("low_24h"),
                "volume_24h":  c.get("total_volume"),
                "market_cap":  c.get("market_cap"),
                "ts":          now,
            }

        with _lock:
            _prices.update(fresh)
            _last_fetch = now

        print(f"  [CoinGecko] {len(fresh)} coins refreshed — BTC=${fresh.get('bitcoin', {}).get('price', '?'):,.0f}" if fresh else "  [CoinGecko] no data")

    except Exception as e:
        print(f"  [CoinGecko] fetch error: {e}")


# ── Background thread ─────────────────────────────────────────────────────────

def start():
    def loop():
        _fetch_all()
        while True:
            time.sleep(REFRESH_TTL)
            _fetch_all()
    threading.Thread(target=loop, daemon=True, name="coingecko-bg").start()
    print("  [CoinGecko] Price feed started (3 min refresh) — BTC/ETH/SOL/DOGE/XRP+")


# ── Market matching ───────────────────────────────────────────────────────────

def _detect_coin(ticker, title):
    """Return CoinGecko coin_id that matches this Kalshi market, or None."""
    t  = ticker.upper()
    tl = title.lower()
    for coin_id, info in COINS.items():
        for pat in info["ticker_patterns"]:
            if t.startswith(pat.upper()):
                return coin_id
        for kw in info["title_kws"]:
            if kw in tl:
                return coin_id
    return None


def _extract_threshold(title):
    """
    Pull the numeric price threshold and direction from a Kalshi market title.

    Examples:
      "Will Bitcoin be above $80,000 on..."   → (80000, "above")
      "Will ETH close below $2,500?"          → (2500, "below")
      "BTC at or above $84,000?"              → (84000, "above")
      "Will BTC high exceed $90k?"            → (90000, "above")
    Returns (threshold: float, direction: str) or (None, None).
    """
    tl = title.lower()

    # Direction
    if any(w in tl for w in ["above", "exceed", "over", "at least", "or higher",
                               "or more", "high", "maximum", "max", "highest"]):
        direction = "above"
    elif any(w in tl for w in ["below", "under", "at most", "or lower",
                                 "or less", "low", "minimum", "min", "lowest"]):
        direction = "below"
    else:
        direction = "above"  # Kalshi crypto defaults to "above" threshold

    # Threshold — handle $80,000 / $80k / 80000 / 80,000
    # Try $-prefixed first
    m = re.search(r'\$([0-9,]+(?:\.[0-9]+)?)\s*k?\b', title, re.IGNORECASE)
    if m:
        raw = m.group(1).replace(",", "")
        val = float(raw)
        if title[m.start() + len(m.group(0)) - 1].lower() == 'k' or \
           (m.group(0).lower().endswith('k')):
            val *= 1000
        # Re-check for trailing 'k' more reliably
        full_match = m.group(0)
        if full_match.lower().rstrip().endswith('k'):
            val *= 1000
        return val, direction

    # Bare number with commas (e.g. "80,000")
    m = re.search(r'\b([0-9]{2,3}(?:,[0-9]{3})+(?:\.[0-9]+)?)\b', title)
    if m:
        return float(m.group(1).replace(",", "")), direction

    # k-notation without $ (e.g. "80k")
    m = re.search(r'\b(\d+(?:\.\d+)?)\s*k\b', title, re.IGNORECASE)
    if m:
        return float(m.group(1)) * 1000, direction

    # Plain number ≥ 100 (catches cents-scale coins like DOGE at 0.15)
    m = re.search(r'\b(\d{3,}(?:\.\d+)?)\b', title)
    if m:
        return float(m.group(1)), direction

    # Small decimal (DOGE, XRP, ADA — e.g. "$0.45")
    m = re.search(r'\$\s*(0\.\d+)', title)
    if m:
        return float(m.group(1)), direction

    return None, direction


def _prob_above(current_price, threshold, change_24h, high_24h, low_24h):
    """
    Estimate P(close > threshold) given current market data.

    Approach:
      1. Range check: if today's 24h high already cleared threshold → very likely YES
      2. Distance curve: how far current is above/below threshold
      3. Momentum nudge: trending direction slightly shifts probability
    """
    if current_price is None or threshold is None:
        return None

    ratio    = current_price / threshold
    dist_pct = (current_price - threshold) / threshold  # positive = above

    # Hard boundaries
    if high_24h and high_24h > threshold * 1.01:
        # Already traded above — strong YES signal
        base = 0.82
    elif low_24h and low_24h > threshold * 1.01:
        # Entire 24h range is above — very confident YES
        base = 0.93
    elif dist_pct >= 0.15:
        base = 0.91
    elif dist_pct >= 0.10:
        base = 0.84
    elif dist_pct >= 0.05:
        base = 0.73
    elif dist_pct >= 0.02:
        base = 0.62
    elif dist_pct >= 0.00:
        base = 0.54
    elif dist_pct >= -0.02:
        base = 0.46
    elif dist_pct >= -0.05:
        base = 0.36
    elif dist_pct >= -0.10:
        base = 0.24
    elif dist_pct >= -0.15:
        base = 0.14
    else:
        base = 0.07

    # Momentum nudge — max ±0.04
    momentum = (change_24h / 100.0) * 0.4
    momentum = max(-0.04, min(0.04, momentum))

    return round(min(0.97, max(0.03, base + momentum)), 4)


def _prob_below(current_price, threshold, change_24h, high_24h, low_24h):
    """P(close < threshold) = 1 - P(close > threshold) with range check."""
    if current_price is None or threshold is None:
        return None

    # If entire 24h range is below threshold — very confident YES (below)
    if high_24h and high_24h < threshold * 0.99:
        dist_pct = (threshold - current_price) / threshold
        if dist_pct >= 0.05:
            return 0.91
        return 0.82

    p_above = _prob_above(current_price, threshold, change_24h, high_24h, low_24h)
    if p_above is None:
        return None
    return round(1.0 - p_above, 4)


def match_market(kalshi_market):
    """
    Match a Kalshi crypto market against CoinGecko live prices.

    Returns:
        {
          "prob":       float,   # estimated YES probability
          "source":     "CoinGecko",
          "coin":       str,     # coin_id
          "symbol":     str,     # btc/eth/sol/...
          "price":      float,   # current price
          "threshold":  float,   # extracted from title
          "direction":  str,     # above / below
          "detail":     str,
        }
    or None if no match.
    """
    ticker = kalshi_market.get("ticker", "")
    title  = kalshi_market.get("title", "")

    coin_id = _detect_coin(ticker, title)
    if not coin_id:
        return None

    with _lock:
        coin_data = _prices.get(coin_id)

    if not coin_data:
        # Try a synchronous fetch for this coin only
        _fetch_all()
        with _lock:
            coin_data = _prices.get(coin_id)
    if not coin_data or coin_data.get("price") is None:
        return None

    price      = coin_data["price"]
    change_24h = coin_data["change_24h"]
    high_24h   = coin_data["high_24h"]
    low_24h    = coin_data["low_24h"]
    symbol     = COINS[coin_id]["symbol"]

    # Detect market type: high/low/close
    tl = title.lower()
    t  = ticker.upper()
    is_high = any(p in t for p in ["D-T", "DMAX", "BTCD", "ETHD", "SOLD", "DOGE"]) or \
              any(w in tl for w in ["high", "highest", "maximum", "exceed", "above"])
    is_low  = any(p in t for p in ["DMIN", "BTCE", "ETHE", "SOLE"]) or \
              any(w in tl for w in ["low", "lowest", "minimum", "below", "under"])

    threshold, direction = _extract_threshold(title)
    if threshold is None:
        # Can't do anything without a threshold — but still return price data
        return None

    # Override direction from is_high/is_low if cleaner signal
    if is_high and not is_low:
        direction = "above"
    elif is_low and not is_high:
        direction = "below"

    if direction == "above":
        prob   = _prob_above(price, threshold, change_24h, high_24h, low_24h)
        detail = (
            f"CoinGecko {symbol.upper()}=${price:,.2f} vs threshold=${threshold:,.2f} "
            f"(dist={((price-threshold)/threshold*100):+.1f}%) "
            f"24h [{low_24h:,.2f}–{high_24h:,.2f}] chg={change_24h:+.1f}%"
        )
    else:
        prob   = _prob_below(price, threshold, change_24h, high_24h, low_24h)
        detail = (
            f"CoinGecko {symbol.upper()}=${price:,.2f} vs threshold=${threshold:,.2f} "
            f"(dist={((threshold-price)/threshold*100):+.1f}% below) "
            f"24h [{low_24h:,.2f}–{high_24h:,.2f}] chg={change_24h:+.1f}%"
        )

    if prob is None:
        return None

    return {
        "prob":      prob,
        "source":    "CoinGecko",
        "coin":      coin_id,
        "symbol":    symbol,
        "price":     price,
        "threshold": threshold,
        "direction": direction,
        "change_24h": change_24h,
        "high_24h":  high_24h,
        "low_24h":   low_24h,
        "detail":    detail,
    }


# ── Public helpers ────────────────────────────────────────────────────────────

def status_summary():
    with _lock:
        n     = len(_prices)
        ts    = _last_fetch
        btc   = _prices.get("bitcoin", {}).get("price")
        eth   = _prices.get("ethereum", {}).get("price")
    age_min = round((time.time() - ts) / 60, 1) if ts else None
    return {
        "coins_loaded": n,
        "age_min":      age_min,
        "btc_price":    btc,
        "eth_price":    eth,
    }

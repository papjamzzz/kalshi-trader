"""
Kalshi Trading API Client
Handles all read + write operations against the Kalshi REST API.

Speed notes:
  - Module-level Session with connection pooling + keep-alive
  - GET timeout 5s, POST timeout 7s (orders need breathing room)
  - Exponential backoff on 429 rate-limit, linear on transient errors
"""

import os
import time
import uuid
import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry
from datetime import datetime
from kalshi_auth import signed_headers

BASE_URL = "https://api.elections.kalshi.com/trade-api/v2"
API_PATH = "/trade-api/v2"

# ── Persistent session — reuse TCP connections across all calls ───────────────
_session = requests.Session()
_adapter = HTTPAdapter(
    pool_connections=4,
    pool_maxsize=8,
    max_retries=Retry(total=0),   # we handle retries manually
)
_session.mount("https://", _adapter)
_session.mount("http://",  _adapter)


def _get(path, params=None, retries=3):
    url = f"{BASE_URL}{path}"
    for attempt in range(retries):
        try:
            headers = signed_headers("GET", f"{API_PATH}{path}")
            r = _session.get(url, headers=headers, params=params, timeout=5)
            if r.status_code == 429:
                time.sleep(2 ** attempt)
                continue
            r.raise_for_status()
            return r.json()
        except requests.exceptions.Timeout:
            if attempt == retries - 1:
                raise
            time.sleep(0.5)
        except Exception as e:
            if attempt == retries - 1:
                raise
            time.sleep(1.0 * (attempt + 1))
    return {}


def _post(path, body, retries=2):
    url = f"{BASE_URL}{path}"
    for attempt in range(retries):
        try:
            headers = signed_headers("POST", f"{API_PATH}{path}")
            r = _session.post(url, headers=headers, json=body, timeout=7)
            if not r.ok:
                print(f"  [API] POST {path} → {r.status_code}: {r.text[:300]}")
            r.raise_for_status()
            return r.json()
        except requests.exceptions.HTTPError:
            raise   # surface immediately — don't retry 4xx
        except requests.exceptions.Timeout:
            if attempt == retries - 1:
                raise
        except Exception:
            if attempt == retries - 1:
                raise
            time.sleep(0.8)
    return {}


def _delete(path, retries=2):
    url = f"{BASE_URL}{path}"
    for attempt in range(retries):
        try:
            headers = signed_headers("DELETE", f"{API_PATH}{path}")
            r = _session.delete(url, headers=headers, timeout=5)
            r.raise_for_status()
            return r.json()
        except Exception:
            if attempt == retries - 1:
                raise
            time.sleep(0.8)
    return {}


# ── Account ───────────────────────────────────────────────────────────────────

def get_balance():
    """Returns available balance in cents."""
    try:
        data = _get("/portfolio/balance")
        return data.get("balance", 0)
    except Exception:
        return 0


def get_positions():
    """Returns list of open market positions (non-zero only)."""
    try:
        data = _get("/portfolio/positions")
        # API returns market_positions for individual markets
        all_pos = data.get("market_positions", [])
        return [p for p in all_pos if float(p.get("position", 0)) != 0]
    except Exception:
        return []


def get_event_positions():
    """Returns event-level position summary (includes realized P&L, fees, exposure)."""
    try:
        data = _get("/portfolio/positions")
        return data.get("event_positions", [])
    except Exception:
        return []


# ── Markets ───────────────────────────────────────────────────────────────────

def get_market(ticker):
    """Fetch live data for a single market ticker."""
    try:
        data = _get(f"/markets/{ticker}")
        return data.get("market", {})
    except Exception:
        return {}


# ── Orders ────────────────────────────────────────────────────────────────────

def place_order(ticker, side, count, price_cents, action="buy"):
    """
    Place a limit order via /portfolio/orders.

    ticker      : market ticker string
    side        : 'yes' or 'no'
    count       : number of contracts (each pays $1 if correct)
    price_cents : price in cents (1–99)
    action      : 'buy' or 'sell'
    """
    client_id = f"kkt-{uuid.uuid4().hex[:12]}"
    body = {
        "ticker":           ticker,
        "client_order_id":  client_id,
        "type":             "limit",
        "action":           action,
        "side":             side,
        "count":            int(count),
        "yes_price":        price_cents if side == "yes" else (100 - price_cents),
    }
    result = _post("/portfolio/orders", body)
    return result.get("order", result)


def cancel_order(order_id):
    """Cancel a resting order by ID."""
    try:
        return _delete(f"/portfolio/orders/{order_id}")
    except Exception as e:
        return {"error": str(e)}


def get_orders(status="resting"):
    """Get list of orders by status: resting | canceled | executed | all"""
    try:
        data = _get("/portfolio/orders", params={"status": status})
        return data.get("orders", [])
    except Exception:
        return []


# ── Helpers ───────────────────────────────────────────────────────────────────

def contracts_for_spend(max_spend_dollars, price_cents):
    """Calculate contracts to buy given a dollar budget and price."""
    if price_cents <= 0:
        return 0
    budget_cents = int(max_spend_dollars * 100)
    return max(1, budget_cents // price_cents)


def position_value_dollars(contracts, current_price_cents):
    """Current market value of a position in dollars."""
    return (contracts * current_price_cents) / 100.0


def pnl_pct(entry_price_cents, current_price_cents):
    """Return P&L as a percentage (positive = profit, negative = loss)."""
    if entry_price_cents <= 0:
        return 0
    return ((current_price_cents - entry_price_cents) / entry_price_cents) * 100

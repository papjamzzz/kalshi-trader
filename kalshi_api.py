"""
Kalshi Trading API Client
Handles all read + write operations against the Kalshi REST API.
"""

import os
import time
import uuid
import requests
from datetime import datetime

BASE_URL = "https://api.elections.kalshi.com/trade-api/v2"


def _headers():
    key = os.getenv("KALSHI_API_KEY", "")
    return {
        "Authorization": f"Token {key}",
        "Content-Type": "application/json",
    }


def _get(path, params=None, retries=3):
    url = f"{BASE_URL}{path}"
    for attempt in range(retries):
        try:
            r = requests.get(url, headers=_headers(), params=params, timeout=10)
            if r.status_code == 429:
                time.sleep(2 ** attempt)
                continue
            r.raise_for_status()
            return r.json()
        except Exception as e:
            if attempt == retries - 1:
                raise
            time.sleep(1.5 ** attempt)
    return {}


def _post(path, body, retries=2):
    url = f"{BASE_URL}{path}"
    for attempt in range(retries):
        try:
            r = requests.post(url, headers=_headers(), json=body, timeout=10)
            r.raise_for_status()
            return r.json()
        except Exception as e:
            if attempt == retries - 1:
                raise
            time.sleep(1)
    return {}


def _delete(path, retries=2):
    url = f"{BASE_URL}{path}"
    for attempt in range(retries):
        try:
            r = requests.delete(url, headers=_headers(), timeout=10)
            r.raise_for_status()
            return r.json()
        except Exception as e:
            if attempt == retries - 1:
                raise
            time.sleep(1)
    return {}


# ── Account ───────────────────────────────────────────────────────────────────

def get_balance():
    """Returns available balance in cents."""
    try:
        data = _get("/portfolio/balance")
        return data.get("balance", 0)  # in cents
    except Exception:
        return 0


def get_positions():
    """Returns list of open positions."""
    try:
        data = _get("/portfolio/positions")
        return data.get("market_positions", [])
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
    Place a limit order.

    ticker      : market ticker string
    side        : 'yes' or 'no'
    count       : number of contracts (each pays $1 if correct)
    price_cents : price in cents (1–99)
    action      : 'buy' or 'sell'

    Returns order dict or raises.
    """
    client_id = f"kkt-{uuid.uuid4().hex[:12]}"
    body = {
        "ticker": ticker,
        "client_order_id": client_id,
        "type": "limit",
        "action": action,
        "side": side,
        "count": count,
        "yes_price": price_cents if side == "yes" else (100 - price_cents),
    }
    result = _post("/orders", body)
    return result.get("order", result)


def cancel_order(order_id):
    """Cancel a resting order by ID."""
    try:
        return _delete(f"/orders/{order_id}")
    except Exception as e:
        return {"error": str(e)}


def get_orders(status="resting"):
    """Get list of orders by status: resting | canceled | executed | all"""
    try:
        data = _get("/orders", params={"status": status})
        return data.get("orders", [])
    except Exception:
        return []


# ── Helpers ───────────────────────────────────────────────────────────────────

def contracts_for_spend(max_spend_dollars, price_cents):
    """Calculate how many contracts to buy given a dollar budget and price."""
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

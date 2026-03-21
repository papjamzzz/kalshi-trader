"""
Polymarket API Client — free, no API key required.
Pulls active prediction markets and their current YES probabilities.

Uses a persistent session for connection reuse.
"""

import json
import requests
from requests.adapters import HTTPAdapter

GAMMA_API = "https://gamma-api.polymarket.com"

_session = requests.Session()
_session.mount("https://", HTTPAdapter(pool_connections=2, pool_maxsize=4))


def get_active_markets(limit=1000):
    """Fetch active Polymarket markets. Up to 1000 for broad coverage."""
    try:
        r = _session.get(
            f"{GAMMA_API}/markets",
            params={"closed": "false", "active": "true", "limit": limit},
            timeout=10,
        )
        if r.status_code == 200:
            return r.json()
    except Exception as e:
        print(f"  [Polymarket] fetch error: {e}")
    return []


def parse_market(raw):
    """Extract structured data from a raw Polymarket market."""
    question = raw.get("question", "")
    if not question:
        return None

    outcomes = raw.get("outcomePrices", [])
    if isinstance(outcomes, str):
        try:
            outcomes = json.loads(outcomes)
        except Exception:
            outcomes = []

    yes_prob = None
    if outcomes:
        try:
            yes_prob = float(outcomes[0])
        except Exception:
            pass

    return {
        "question":  question,
        "yes_prob":  yes_prob,
        "volume":    float(raw.get("volume", 0) or 0),
        "liquidity": float(raw.get("liquidity", 0) or 0),
        "slug":      raw.get("slug", ""),
    }

"""
PredictIt API Client — prediction market prices for political contracts.
Free, no API key required.

PredictIt is a direct Kalshi competitor on political markets.
When PredictIt prices an outcome at 70¢ and Kalshi prices it at 55¢,
that 15-point gap is the strongest possible edge signal — two live
markets both pricing real money, and one is wrong.

API: https://www.predictit.org/api/marketdata/all/
Returns all 250+ active markets with contract-level yes/no prices.
"""

import time
import requests

API_URL = "https://www.predictit.org/api/marketdata/all/"

_session = requests.Session()
_session.headers.update({
    "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7)",
    "Accept":     "application/json",
})

_cache    = []
_cache_ts = 0.0
CACHE_TTL = 300   # 5 minutes — PredictIt prices move, keep fresh


def _parse_market(raw):
    """
    Parse a PredictIt market into a flat list of tradeable contracts.
    Binary markets (1 contract) → single entry.
    Multi-contract markets → one entry per contract with enriched name.
    """
    market_name = raw.get("name", "")
    contracts   = raw.get("contracts", [])
    results     = []

    for c in contracts:
        if c.get("status") != "Open":
            continue
        yes_price = c.get("bestBuyYesCost") or c.get("lastTradePrice")
        if yes_price is None:
            continue
        try:
            yes_prob = float(yes_price)
        except (TypeError, ValueError):
            continue
        if yes_prob <= 0 or yes_prob >= 1:
            continue

        # For multi-contract markets, prepend the contract name to the market name
        # so matching can work on the full question
        contract_name = c.get("name", "").strip()
        if len(contracts) == 1 or not contract_name:
            full_name = market_name
        else:
            full_name = f"{market_name} — {contract_name}"

        results.append({
            "question": full_name,
            "yes_prob": round(yes_prob, 4),
            "market":   market_name,
            "contract": contract_name,
            "id":       c.get("id"),
        })

    return results


def get_markets():
    """Return all parsed PredictIt contracts, refreshing cache if stale."""
    global _cache, _cache_ts
    if time.time() - _cache_ts < CACHE_TTL and _cache:
        return _cache

    try:
        r = _session.get(API_URL, timeout=12)
        if r.ok:
            raw_markets = r.json().get("markets", [])
            parsed = []
            for m in raw_markets:
                parsed.extend(_parse_market(m))
            _cache    = parsed
            _cache_ts = time.time()
            print(f"  [PredictIt] {len(parsed)} contracts loaded ({len(raw_markets)} markets)")
            return parsed
    except Exception as e:
        print(f"  [PredictIt] fetch error: {e}")

    return _cache   # return stale on error

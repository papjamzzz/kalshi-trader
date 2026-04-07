"""
APILayer Odds API client — sportsbook lines for cross-market edge detection.
Aggregates DraftKings, FanDuel, BetMGM, Caesars, and 40+ books into
implied probabilities that the bot compares against Kalshi prices.

APILayer auth: apikey header (not query param like the-odds-api.com).
Free tier: 500 req/month. At 3 sports × ~55 fetches/month = ~165 req/month.

Sports: NBA, MLB, NHL only — these are the markets we actually trade.
Fetch cadence: controlled by cross_market.py ODDS_REFRESH_INTERVAL (6h default).
"""

import os
import requests
from concurrent.futures import ThreadPoolExecutor, as_completed
from requests.adapters import HTTPAdapter

BASE = "https://api.apilayer.com/odds/sports"

# NBA, MLB, NHL only — matches our SPORTS_SERIES in trader.py
SPORTS = [
    "basketball_nba",
    "baseball_mlb",
    "icehockey_nhl",
]

_session = requests.Session()
_session.mount("https://", HTTPAdapter(pool_connections=4, pool_maxsize=8))


def get_key():
    return os.getenv("ODDS_API_KEY", "")


def american_to_prob(american_odds):
    """American moneyline → vig-inclusive implied probability (0–1)."""
    try:
        o = float(american_odds)
        return 100 / (o + 100) if o > 0 else abs(o) / (abs(o) + 100)
    except Exception:
        return None


def _fetch_sport(sport_key, key):
    """Fetch one sport from APILayer. Auth via apikey header."""
    try:
        r = _session.get(
            f"{BASE}/{sport_key}/odds",
            headers={"apikey": key},
            params={
                "markets":    "h2h",
                "regions":    "us",
                "oddsFormat": "american",
            },
            timeout=10,
        )
        if r.status_code == 200:
            return _parse_events(r.json())
        else:
            print(f"  [OddsAPI] {sport_key} → {r.status_code}: {r.text[:120]}")
    except Exception as e:
        print(f"  [OddsAPI] {sport_key} error: {e}")
    return []


def _parse_events(raw_events):
    out = []
    for ev in raw_events:
        home = ev.get("home_team", "")
        away = ev.get("away_team", "")
        title = f"{away} vs {home}"

        home_probs, away_probs = [], []
        for bk in ev.get("bookmakers", []):
            for mkt in bk.get("markets", []):
                if mkt.get("key") != "h2h":
                    continue
                for outcome in mkt.get("outcomes", []):
                    prob = american_to_prob(outcome.get("price"))
                    if prob is None:
                        continue
                    if outcome.get("name") == home:
                        home_probs.append(prob)
                    else:
                        away_probs.append(prob)

        if home_probs and away_probs:
            h = sum(home_probs) / len(home_probs)
            a = sum(away_probs) / len(away_probs)
            total = h + a
            out.append({
                "title":      title,
                "home":       home,
                "away":       away,
                "home_prob":  round(h / total, 4),
                "away_prob":  round(a / total, 4),
                "bookmakers": len(ev.get("bookmakers", [])),
                "commence":   ev.get("commence_time", ""),
            })
    return out


def get_all_odds():
    """
    Pull NBA/MLB/NHL in parallel.
    Called by cross_market.py background thread every ODDS_REFRESH_INTERVAL.
    At 3 sports × ~55 fetches/month = ~165 API calls/month — well within 500.
    """
    key = get_key()
    if not key:
        print("  [OddsAPI] No ODDS_API_KEY set")
        return []

    all_events = []
    with ThreadPoolExecutor(max_workers=3) as pool:
        futures = {pool.submit(_fetch_sport, s, key): s for s in SPORTS}
        for fut in as_completed(futures):
            try:
                all_events.extend(fut.result())
            except Exception:
                pass

    print(f"  [OddsAPI] {len(all_events)} events (NBA/MLB/NHL)")
    return all_events

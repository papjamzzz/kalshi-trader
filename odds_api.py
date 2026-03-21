"""
The Odds API client — aggregates DraftKings, FanDuel, BetMGM, Caesars,
PointsBet, and 70+ sportsbooks into a single implied-probability feed.

Uses a persistent session + parallel sport fetching for minimal latency.
Free tier: 500 req/month. Set ODDS_API_KEY in .env.
"""

import os
import requests
from concurrent.futures import ThreadPoolExecutor, as_completed
from requests.adapters import HTTPAdapter

BASE = "https://api.the-odds-api.com/v4"

SPORTS = [
    "americanfootball_nfl",
    "americanfootball_ncaaf",
    "basketball_nba",
    "basketball_ncaab",
    "baseball_mlb",
    "icehockey_nhl",
    "soccer_usa_mls",
    "mma_mixed_martial_arts",
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
    """Fetch one sport. Returns list of parsed events."""
    try:
        r = _session.get(
            f"{BASE}/sports/{sport_key}/odds/",
            params={
                "apiKey":     key,
                "markets":    "h2h",
                "regions":    "us",
                "oddsFormat": "american",
            },
            timeout=8,
        )
        if r.status_code == 200:
            return _parse_events(r.json())
    except Exception:
        pass
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
    Pull all sports in parallel using a thread pool.
    Dramatically faster than sequential fetching.
    """
    key = get_key()
    if not key:
        return []

    all_events = []
    with ThreadPoolExecutor(max_workers=6) as pool:
        futures = {pool.submit(_fetch_sport, s, key): s for s in SPORTS}
        for fut in as_completed(futures):
            try:
                all_events.extend(fut.result())
            except Exception:
                pass

    print(f"  [OddsAPI] {len(all_events)} events across {len(SPORTS)} sports")
    return all_events

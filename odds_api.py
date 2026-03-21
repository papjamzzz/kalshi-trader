"""
The Odds API client — aggregates DraftKings, FanDuel, BetMGM, Caesars,
PointsBet, and 70+ other sportsbooks into a single feed.

Free tier: 500 requests/month. Sign up at https://the-odds-api.com
Set ODDS_API_KEY in .env to enable.

Converts sportsbook moneyline/spread odds → implied probabilities
so they're comparable to Kalshi cent prices.
"""

import os
import requests

BASE = "https://api.the-odds-api.com/v4"

# Sports with active Kalshi markets most commonly
SPORTS = [
    "americanfootball_nfl",
    "americanfootball_ncaaf",
    "basketball_nba",
    "basketball_ncaab",
    "baseball_mlb",
    "icehockey_nhl",
    "soccer_usa_mls",
    "politics",               # if supported
]


def get_key():
    return os.getenv("ODDS_API_KEY", "")


def american_to_prob(american_odds):
    """Convert American moneyline odds to implied probability (0–1)."""
    try:
        o = float(american_odds)
        if o > 0:
            return 100 / (o + 100)
        else:
            return abs(o) / (abs(o) + 100)
    except Exception:
        return None


def get_sport_odds(sport_key):
    """Fetch odds for one sport, returns list of events with implied probs."""
    key = get_key()
    if not key:
        return []
    try:
        r = requests.get(
            f"{BASE}/sports/{sport_key}/odds/",
            params={
                "apiKey":  key,
                "markets": "h2h",
                "regions": "us",
                "oddsFormat": "american",
            },
            timeout=10,
        )
        if r.status_code == 200:
            return _parse_events(r.json())
        if r.status_code == 401:
            print(f"  [OddsAPI] Invalid key")
        elif r.status_code == 422:
            pass  # sport not available
        else:
            print(f"  [OddsAPI] {sport_key} → HTTP {r.status_code}")
    except Exception as e:
        print(f"  [OddsAPI] {sport_key} error: {e}")
    return []


def _parse_events(raw_events):
    """Convert raw API events → list of {title, home_prob, away_prob, bookmakers}."""
    out = []
    for ev in raw_events:
        home = ev.get("home_team", "")
        away = ev.get("away_team", "")
        title = f"{away} vs {home}"  # "Team A vs Team B"

        # Aggregate implied probs across all bookmakers (average)
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
            # Remove vig: normalize so they sum to 1
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
    """Pull odds from all sports. Returns flat list of events."""
    key = get_key()
    if not key:
        return []
    all_events = []
    for sport in SPORTS:
        events = get_sport_odds(sport)
        all_events.extend(events)
    print(f"  [OddsAPI] {len(all_events)} events from sportsbooks")
    return all_events

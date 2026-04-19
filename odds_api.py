"""
Sportsbook Odds — real moneyline consensus for NBA/MLB/NHL game markets.

Source: APILayer Odds API (aggregates DraftKings, FanDuel, BetMGM, Caesars, 40+ books).
Free tier: 500 req/month. At 3 sports × ~2/day = ~180 req/month.

Refresh: every 30 minutes — games move fast near tip-off.
Background thread keeps a cache; cross_market.py reads it with zero latency.

match_game_market(kalshi_market) → {prob, detail} or None
  - Extracts team codes from Kalshi ticker (e.g. KXMLBGAME-26APR142010COLHOU-HOU → HOU)
  - Looks up sportsbook consensus win probability for that team
  - Returns vig-adjusted probability (overround removed)
"""

import os
import re
import time
import threading
import requests
from concurrent.futures import ThreadPoolExecutor, as_completed
from requests.adapters import HTTPAdapter

BASE    = "https://api.apilayer.com/odds/sports"
REFRESH = 1800   # 30 min

SPORTS = ["basketball_nba", "baseball_mlb", "icehockey_nhl"]

_session = requests.Session()
_session.mount("https://", HTTPAdapter(pool_connections=4, pool_maxsize=8))

_lock       = threading.Lock()
_cache      = []      # list of event dicts from _parse_events
_last_fetch = 0.0

# ── Team code → keyword fragments for fuzzy matching ──────────────────────────
# Key = 3-letter Kalshi code (uppercase), value = substrings to search in
# sportsbook team name (case-insensitive). First match wins.
TEAM_KEYWORDS = {
    # NBA
    "ATL": ["atlanta", "hawks"],
    "BOS": ["boston", "celtics"],
    "BKN": ["brooklyn", "nets"],
    "CHA": ["charlotte", "hornets"],
    "CHI": ["chicago", "bulls"],
    "CLE": ["cleveland", "cavaliers"],
    "DAL": ["dallas", "mavericks"],
    "DEN": ["denver", "nuggets"],
    "DET": ["detroit", "pistons"],
    "GSW": ["golden state", "warriors"],
    "HOU": ["houston", "rockets"],
    "IND": ["indiana", "pacers"],
    "LAC": ["clippers", "la clippers"],
    "LAL": ["lakers", "la lakers"],
    "MEM": ["memphis", "grizzlies"],
    "MIA": ["miami", "heat"],
    "MIL": ["milwaukee", "bucks"],
    "MIN": ["minnesota", "timberwolves"],
    "NOP": ["new orleans", "pelicans"],
    "NYK": ["new york", "knicks"],
    "OKC": ["oklahoma", "thunder"],
    "ORL": ["orlando", "magic"],
    "PHI": ["philadelphia", "76ers", "sixers"],
    "PHX": ["phoenix", "suns"],
    "POR": ["portland", "trail blazers"],
    "SAC": ["sacramento", "kings"],
    "SAS": ["san antonio", "spurs"],
    "TOR": ["toronto", "raptors"],
    "UTA": ["utah", "jazz"],
    "WAS": ["washington", "wizards"],
    # MLB
    "ARI": ["arizona", "diamondbacks"],
    "ATH": ["athletics", "oakland"],
    "BAL": ["baltimore", "orioles"],
    "BOS": ["boston", "red sox"],
    "CHC": ["chicago", "cubs"],
    "CWS": ["chicago", "white sox"],
    "CIN": ["cincinnati", "reds"],
    "CLE": ["cleveland", "guardians"],
    "COL": ["colorado", "rockies"],
    "DET": ["detroit", "tigers"],
    "HOU": ["houston", "astros"],
    "KCR": ["kansas city", "royals"],
    "LAA": ["los angeles", "angels"],
    "LAD": ["dodgers", "la dodgers"],
    "MIA": ["miami", "marlins"],
    "MIL": ["milwaukee", "brewers"],
    "MIN": ["minnesota", "twins"],
    "NYM": ["new york", "mets"],
    "NYY": ["new york", "yankees"],
    "OAK": ["oakland", "athletics"],
    "PHI": ["philadelphia", "phillies"],
    "PIT": ["pittsburgh", "pirates"],
    "SDP": ["san diego", "padres"],
    "SEA": ["seattle", "mariners"],
    "SFG": ["san francisco", "giants"],
    "STL": ["st. louis", "cardinals", "st louis"],
    "TBR": ["tampa bay", "rays"],
    "TEX": ["texas", "rangers"],
    "TOR": ["toronto", "blue jays"],
    "WSH": ["washington", "nationals"],
    # NHL
    "ANA": ["anaheim", "ducks"],
    "ARI": ["arizona", "coyotes"],
    "BOS": ["boston", "bruins"],
    "BUF": ["buffalo", "sabres"],
    "CAR": ["carolina", "hurricanes"],
    "CBJ": ["columbus", "blue jackets"],
    "CGY": ["calgary", "flames"],
    "CHI": ["chicago", "blackhawks"],
    "COL": ["colorado", "avalanche"],
    "DAL": ["dallas", "stars"],
    "DET": ["detroit", "red wings"],
    "EDM": ["edmonton", "oilers"],
    "FLA": ["florida", "panthers"],
    "LAK": ["los angeles", "kings"],
    "LA":  ["los angeles", "kings", "la kings"],
    "MIN": ["minnesota", "wild"],
    "MTL": ["montreal", "canadiens"],
    "NJD": ["new jersey", "devils"],
    "NJ":  ["new jersey", "devils"],
    "NSH": ["nashville", "predators"],
    "NYI": ["new york", "islanders"],
    "NYR": ["new york", "rangers"],
    "OTT": ["ottawa", "senators"],
    "PHI": ["philadelphia", "flyers"],
    "PIT": ["pittsburgh", "penguins"],
    "SEA": ["seattle", "kraken"],
    "SJS": ["san jose", "sharks"],
    "SJ":  ["san jose", "sharks"],
    "STL": ["st. louis", "blues", "st louis"],
    "TBL": ["tampa bay", "lightning"],
    "TB":  ["tampa bay", "lightning"],
    "TOR": ["toronto", "maple leafs"],
    "UTA": ["utah", "hockey club"],
    "VAN": ["vancouver", "canucks"],
    "VGK": ["vegas", "golden knights"],
    "WPG": ["winnipeg", "jets"],
    "WPJ": ["winnipeg", "jets"],
    "WSH": ["washington", "capitals"],
}


# ── API fetch ─────────────────────────────────────────────────────────────────

def get_key():
    return os.getenv("ODDS_API_KEY", "")


def american_to_prob(american_odds):
    """American moneyline → implied probability (0–1), before vig removal."""
    try:
        o = float(american_odds)
        return 100 / (o + 100) if o > 0 else abs(o) / (abs(o) + 100)
    except Exception:
        return None


def _fetch_sport(sport_key, key):
    try:
        r = _session.get(
            f"{BASE}/{sport_key}/odds",
            headers={"apikey": key},
            params={"markets": "h2h", "regions": "us", "oddsFormat": "american"},
            timeout=10,
        )
        if r.status_code == 200:
            return _parse_events(r.json())
        print(f"  [OddsAPI] {sport_key} → {r.status_code}: {r.text[:80]}")
    except Exception as e:
        print(f"  [OddsAPI] {sport_key} error: {e}")
    return []


def _parse_events(raw_events):
    out = []
    for ev in raw_events:
        home = ev.get("home_team", "")
        away = ev.get("away_team", "")
        home_probs, away_probs = [], []
        for bk in ev.get("bookmakers", []):
            for mkt in bk.get("markets", []):
                if mkt.get("key") != "h2h":
                    continue
                for outcome in mkt.get("outcomes", []):
                    p = american_to_prob(outcome.get("price"))
                    if p is None:
                        continue
                    if outcome.get("name") == home:
                        home_probs.append(p)
                    else:
                        away_probs.append(p)
        if home_probs and away_probs:
            h = sum(home_probs) / len(home_probs)
            a = sum(away_probs) / len(away_probs)
            total = h + a
            out.append({
                "home":       home,
                "away":       away,
                "home_prob":  round(h / total, 4),
                "away_prob":  round(a / total, 4),
                "bookmakers": len(ev.get("bookmakers", [])),
                "commence":   ev.get("commence_time", ""),
            })
    return out


def _fetch_all():
    global _last_fetch
    key = get_key()
    if not key:
        return
    fresh = []
    with ThreadPoolExecutor(max_workers=3) as pool:
        futures = {pool.submit(_fetch_sport, s, key): s for s in SPORTS}
        for fut in as_completed(futures):
            try:
                fresh.extend(fut.result())
            except Exception:
                pass
    with _lock:
        _cache.clear()
        _cache.extend(fresh)
        _last_fetch = time.time()
    print(f"  [OddsAPI] {len(fresh)} game lines cached (NBA/MLB/NHL)")


# ── Background thread ─────────────────────────────────────────────────────────

def start():
    def loop():
        _fetch_all()
        while True:
            time.sleep(REFRESH)
            _fetch_all()
    threading.Thread(target=loop, daemon=True, name="oddsapi-bg").start()
    print(f"  [OddsAPI] Sportsbook lines started (30 min refresh) — NBA/MLB/NHL")


# ── Matching ──────────────────────────────────────────────────────────────────

def _extract_team_codes(ticker):
    """
    KXMLBGAME-26APR142010COLHOU-HOU  →  team_a=COL, team_b=HOU, this_team=HOU
    KXNHLGAME-26APR13LASEA-LA        →  team_a=LA,  team_b=SEA, this_team=LA
    Returns (team_a, team_b, this_team) or (None, None, None).
    """
    # Pattern: SERIES-DATE[TIME]TEAM_ATEAM_B-THIS_TEAM
    m = re.match(r'^KX\w+GAME-\d{6,8}[A-Z0-9]*?([A-Z]{2,3})([A-Z]{2,3})-([A-Z]{2,3})$', ticker)
    if m:
        return m.group(1), m.group(2), m.group(3)
    return None, None, None


def _team_matches(code, team_name):
    """Does 3-letter code match a sportsbook team name string?"""
    kws = TEAM_KEYWORDS.get(code.upper(), [])
    name_lower = team_name.lower()
    return any(kw in name_lower for kw in kws)


def match_game_market(kalshi_market):
    """
    Match a Kalshi game market against sportsbook consensus lines.

    Returns:
        { "prob": float, "source": "OddsAPI", "books": int, "detail": str }
    or None.
    """
    ticker = kalshi_market.get("ticker", "")
    # Only handle game series markets
    if not any(ticker.startswith(p) for p in
               ("KXNBAGAME", "KXMLBGAME", "KXNHLGAME")):
        return None

    team_a, team_b, this_team = _extract_team_codes(ticker)
    if not this_team:
        return None

    with _lock:
        events = list(_cache)

    if not events:
        return None

    # Find the matching event
    for ev in events:
        home = ev["home"]
        away = ev["away"]
        home_match = _team_matches(team_a, home) or _team_matches(team_b, home)
        away_match = _team_matches(team_a, away) or _team_matches(team_b, away)
        if not (home_match and away_match):
            continue

        # Which team is THIS market for?
        if _team_matches(this_team, home):
            prob = ev["home_prob"]
            opp  = away
        else:
            prob = ev["away_prob"]
            opp  = home

        detail = (
            f"OddsAPI {this_team} vs {opp}: "
            f"books={ev['bookmakers']} consensus={prob:.1%}"
        )
        return {
            "prob":      prob,
            "source":    "OddsAPI",
            "books":     ev["bookmakers"],
            "detail":    detail,
        }

    return None


def status_summary():
    with _lock:
        n  = len(_cache)
        ts = _last_fetch
    return {
        "events": n,
        "age_min": round((time.time() - ts) / 60, 1) if ts else None,
    }

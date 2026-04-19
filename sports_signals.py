"""
Sports Signals — NBA / MLB / NHL injury-adjusted probabilities for Kalshi.

Real edge: Kalshi reprices slowly (5–20 min) after injury news drops.
ESPN updates injury status in real-time. We bridge that gap.

Market types handled:
  KXNHLGAME-*   — NHL game winner (1¢ spread, 200k-600k vol)
  KXMLBGAME-*   — MLB game winner (1¢ spread, 300k-700k vol)
  KXNBAGAME-*   — NBA game winner
  KXNBA3PT-*    — NBA player 3-pointer props
  KXNBAPTS-*    — NBA player points props
  KXNBAAST-*    — NBA player assists props
  KXNBAREB-*    — NBA player rebound props
  KXMLBHR-*     — MLB home run props
  KXMLBHITS-*   — MLB hits props
  KXMVE*        — Multi-leg parlays (parse each leg)

Signal logic:
  Game outcomes:
    Injury adjustment = sum of impact scores for OUT/Doubtful starters
    OUT starter  → −5% per player (key player = −8%)
    Doubtful     → −2% per player
    Net adj shifts implied win probability up/down

  Player props:
    Player OUT       → YES prob = 0.02 (won't play, prop fails)
    Player Doubtful  → YES prob *= 0.15
    Player Questionable → YES prob *= 0.65
    No injury info   → None (no signal, don't interfere)

  Parlays (KXMVE):
    If any YES-side player is OUT → whole YES = 0.02 (parlay fails)
    Count how many legs are compromised
"""

import re
import time
import threading

# ── Team code → full name mapping ────────────────────────────────────────────
# Keyed by 2–4 char code as it appears in Kalshi tickers
# Values must match ESPN's team.displayName exactly (used for injury lookup)

NBA_TEAMS = {
    "ATL": "Atlanta Hawks",       "BOS": "Boston Celtics",
    "BKN": "Brooklyn Nets",       "CHA": "Charlotte Hornets",
    "CHI": "Chicago Bulls",       "CLE": "Cleveland Cavaliers",
    "DAL": "Dallas Mavericks",    "DEN": "Denver Nuggets",
    "DET": "Detroit Pistons",     "GSW": "Golden State Warriors",
    "HOU": "Houston Rockets",     "IND": "Indiana Pacers",
    "LAC": "LA Clippers",         "LAL": "Los Angeles Lakers",
    "MEM": "Memphis Grizzlies",   "MIA": "Miami Heat",
    "MIL": "Milwaukee Bucks",     "MIN": "Minnesota Timberwolves",
    "NOP": "New Orleans Pelicans","NYK": "New York Knicks",
    "OKC": "Oklahoma City Thunder","ORL": "Orlando Magic",
    "PHI": "Philadelphia 76ers",  "PHX": "Phoenix Suns",
    "POR": "Portland Trail Blazers","SAC": "Sacramento Kings",
    "SAS": "San Antonio Spurs",   "TOR": "Toronto Raptors",
    "UTA": "Utah Jazz",           "WAS": "Washington Wizards",
    # Aliases Kalshi uses
    "GS":  "Golden State Warriors","NO": "New Orleans Pelicans",
    "NY":  "New York Knicks",     "SA": "San Antonio Spurs",
}

MLB_TEAMS = {
    "ARI": "Arizona Diamondbacks", "ATL": "Atlanta Braves",
    "BAL": "Baltimore Orioles",    "BOS": "Boston Red Sox",
    "CHC": "Chicago Cubs",         "CHW": "Chicago White Sox",
    "CIN": "Cincinnati Reds",      "CLE": "Cleveland Guardians",
    "COL": "Colorado Rockies",     "DET": "Detroit Tigers",
    "HOU": "Houston Astros",       "KCR": "Kansas City Royals",
    "LAA": "Los Angeles Angels",   "LAD": "Los Angeles Dodgers",
    "MIA": "Miami Marlins",        "MIL": "Milwaukee Brewers",
    "MIN": "Minnesota Twins",      "NYM": "New York Mets",
    "NYY": "New York Yankees",     "OAK": "Oakland Athletics",
    "PHI": "Philadelphia Phillies","PIT": "Pittsburgh Pirates",
    "SDP": "San Diego Padres",     "SEA": "Seattle Mariners",
    "SFG": "San Francisco Giants", "STL": "St. Louis Cardinals",
    "TBR": "Tampa Bay Rays",       "TEX": "Texas Rangers",
    "TOR": "Toronto Blue Jays",    "WSH": "Washington Nationals",
    # Aliases
    "AZ":  "Arizona Diamondbacks", "KC":  "Kansas City Royals",
    "LA":  "Los Angeles Dodgers",  "NY":  "New York Yankees",
    "NYM": "New York Mets",        "SF":  "San Francisco Giants",
    "SD":  "San Diego Padres",     "TB":  "Tampa Bay Rays",
    "WSH": "Washington Nationals", "WAS": "Washington Nationals",
    "CWS": "Chicago White Sox",    "WSN": "Washington Nationals",
}

NHL_TEAMS = {
    "ANA": "Anaheim Ducks",        "ARI": "Arizona Coyotes",
    "BOS": "Boston Bruins",        "BUF": "Buffalo Sabres",
    "CAR": "Carolina Hurricanes",  "CBJ": "Columbus Blue Jackets",
    "CGY": "Calgary Flames",       "CHI": "Chicago Blackhawks",
    "COL": "Colorado Avalanche",   "DAL": "Dallas Stars",
    "DET": "Detroit Red Wings",    "EDM": "Edmonton Oilers",
    "FLA": "Florida Panthers",     "LA":  "Los Angeles Kings",
    "LAK": "Los Angeles Kings",    "MIN": "Minnesota Wild",
    "MTL": "Montreal Canadiens",   "NJD": "New Jersey Devils",
    "NSH": "Nashville Predators",  "NYI": "New York Islanders",
    "NYR": "New York Rangers",     "OTT": "Ottawa Senators",
    "PHI": "Philadelphia Flyers",  "PIT": "Pittsburgh Penguins",
    "SEA": "Seattle Kraken",       "SJS": "San Jose Sharks",
    "STL": "St. Louis Blues",      "TBL": "Tampa Bay Lightning",
    "TOR": "Toronto Maple Leafs",  "VAN": "Vancouver Canucks",
    "VGK": "Vegas Golden Knights", "WPG": "Winnipeg Jets",
    "TB":  "Tampa Bay Lightning",  "NJ":  "New Jersey Devils",
    "NY":  "New York Rangers",     "NYI": "New York Islanders",
    "SJ":  "San Jose Sharks",
}

# Reverse: full name (lowercased) → abbr, for fuzzy lookup
_NAME_TO_CODE = {}
for _d in [NBA_TEAMS, MLB_TEAMS, NHL_TEAMS]:
    for k, v in _d.items():
        _NAME_TO_CODE[v.lower()] = k

# Key player positions — injuries to these matter more
KEY_POSITIONS = {"PG", "SG", "SF", "PF", "C",    # NBA
                 "SP", "RP", "C", "1B", "SS",     # MLB starters/key pos
                 "G", "LW", "RW", "C", "D"}       # NHL

_lock = threading.Lock()


# ── Helpers ───────────────────────────────────────────────────────────────────

def _get_team_injuries(team_code, sport_hint=None):
    """
    Look up injury list for a team by 2-4 char code.
    Returns list of injury dicts from injury.py cache.
    """
    try:
        import injury as inj
        with inj._lock:
            snap = dict(inj._injuries)
    except Exception:
        return []

    # Build candidate full names to search
    candidates = set()
    for d in ([NBA_TEAMS, MLB_TEAMS, NHL_TEAMS] if not sport_hint
              else [{"nba": NBA_TEAMS, "mlb": MLB_TEAMS, "nhl": NHL_TEAMS}.get(sport_hint, NBA_TEAMS)]):
        name = d.get(team_code.upper())
        if name:
            candidates.add(name.lower())

    for team_display, injuries in snap.items():
        td = team_display.lower()
        if any(c in td or td in c for c in candidates):
            return injuries
        # Also try partial match on city name or nickname
        for c in candidates:
            words = c.split()
            if any(w in td for w in words if len(w) > 3):
                return injuries

    return []


def _get_player_injury(player_name):
    """
    Look up a specific player's injury status.
    Returns injury dict or None.
    """
    try:
        import injury as inj
        with inj._lock:
            snap = dict(inj._injuries)
    except Exception:
        return None

    name_lower = player_name.lower().strip()
    # Try exact match first, then partial
    for team_injuries in snap.values():
        for inj_record in team_injuries:
            pname = inj_record.get("player", "").lower()
            if pname == name_lower:
                return inj_record
            # Last name match if first name also matches
            parts = name_lower.split()
            pparts = pname.split()
            if len(parts) >= 2 and len(pparts) >= 2:
                if parts[-1] == pparts[-1] and parts[0][0] == pparts[0][0]:
                    return inj_record
    return None


def _injury_impact_score(injuries, side="yes"):
    """
    Compute a probability adjustment from a team's injury list.
    Returns float: negative = reduces win prob, positive = helps.
    Applied to the team whose YES we're evaluating.
    """
    total = 0.0
    high_impact = {"Out", "Doubtful"}
    med_impact  = {"Questionable"}

    for inj in injuries:
        status = inj.get("status", "")
        pos    = inj.get("pos", "")
        is_key = pos.upper() in KEY_POSITIONS or pos == ""  # unknown pos = assume key

        if status in high_impact:
            total -= 0.06 if is_key else 0.03
        elif status in med_impact:
            total -= 0.02 if is_key else 0.01

    return max(-0.15, total)   # cap at -15%


# ── Market type parsers ───────────────────────────────────────────────────────

def _parse_game_ticker(ticker):
    """
    Parse KXNHLGAME-26APR13CARPHI-PHI → {sport, date_str, team_a, team_b, this_team}
    Parse KXMLBGAME-26APR131905LAANYY-NYY → {sport, team_a: LAA, team_b: NYY, this_team: NYY}
    """
    t = ticker.upper()
    result = {"sport": None, "team_a": None, "team_b": None, "this_team": None}

    if "KXNHLGAME" in t:
        result["sport"] = "nhl"
        # KXNHLGAME-26APR13CARPHI-PHI  → teams = CARPHI → CAR, PHI
        m = re.search(r'KXNHLGAME-\w+-([A-Z]{2,4})([A-Z]{2,4})-([A-Z]{2,4})$', t)
        if m:
            result.update(team_a=m.group(1), team_b=m.group(2), this_team=m.group(3))
        else:
            # fallback: split on last dash
            parts = t.split("-")
            if len(parts) >= 3:
                result["this_team"] = parts[-1]

    elif "KXMLBGAME" in t:
        result["sport"] = "mlb"
        # KXMLBGAME-26APR131905LAANYY-NYY → teams after time = LAANYY → LAA, NYY
        m = re.search(r'KXMLBGAME-\d{2}[A-Z]{3}\d{2}\d{4}([A-Z]{2,3})([A-Z]{2,3})-([A-Z]{2,3})$', t)
        if m:
            result.update(team_a=m.group(1), team_b=m.group(2), this_team=m.group(3))
        else:
            parts = t.split("-")
            if len(parts) >= 3:
                result["this_team"] = parts[-1]

    elif "KXNBAGAME" in t:
        result["sport"] = "nba"
        m = re.search(r'KXNBAGAME-\w+-([A-Z]{2,4})([A-Z]{2,4})-([A-Z]{2,4})$', t)
        if m:
            result.update(team_a=m.group(1), team_b=m.group(2), this_team=m.group(3))
        else:
            parts = t.split("-")
            if len(parts) >= 3:
                result["this_team"] = parts[-1]

    return result


def _parse_player_prop(ticker, title):
    """
    Parse player prop markets.
    KXNBA3PT-26APR14MIACHA-CHACWHITE3-2 title="Coby White: 2+ threes"
    Returns {player, threshold, stat_type}
    """
    # Extract player name and threshold from title
    m = re.match(r'^(.+?):\s*(\d+)\+', title)
    if not m:
        return None
    player = m.group(1).strip()
    threshold = int(m.group(2))

    # Stat type from ticker
    t = ticker.upper()
    if "NBA3PT" in t or "3PT" in t:
        stat = "3-pointers"
    elif "NBAPTS" in t or "PTS" in t:
        stat = "points"
    elif "NBAAST" in t or "AST" in t:
        stat = "assists"
    elif "NBAREB" in t or "REB" in t:
        stat = "rebounds"
    elif "MLBHR" in t:
        stat = "home runs"
    elif "MLBHITS" in t:
        stat = "hits"
    elif "MLBK" in t or "STRIKEOUT" in t:
        stat = "strikeouts"
    else:
        stat = "stat"

    return {"player": player, "threshold": threshold, "stat": stat}


def _parse_parlay_legs(title):
    """
    Parse KXMVE parlay title into individual legs.
    "yes LaMelo Ball: 3+,yes Coby White: 1+,no Over 8.5 runs scored"
    Returns list of {side, player_or_desc, threshold}
    """
    legs = []
    for part in title.split(","):
        part = part.strip()
        m = re.match(r'^(yes|no)\s+(.+?)(?::\s*(\d+\+?))?$', part, re.IGNORECASE)
        if m:
            side  = m.group(1).lower()
            desc  = m.group(2).strip()
            thresh = m.group(3) or ""
            legs.append({"side": side, "description": desc, "threshold": thresh})
    return legs


# ── Main signal function ──────────────────────────────────────────────────────

def match_market(kalshi_market):
    """
    Given a Kalshi market dict, return a sports signal dict or None.

    Returns:
        {
          "prob":    float,   # estimated YES probability (0-1)
          "source":  "SportsSignals",
          "detail":  str,
          "injured_legs": int,   # for parlays
          "fresh":   bool,   # injury reported < 30min ago
        }
    or None if not a sports market or no signal.
    """
    ticker = kalshi_market.get("ticker", "")
    title  = kalshi_market.get("title", "")
    yes_ask = float(kalshi_market.get("yes_ask", 50)) / 100.0   # convert cents → 0-1

    t = ticker.upper()

    # ── Game outcome markets ──────────────────────────────────────────────────
    if any(k in t for k in ["KXNHLGAME", "KXMLBGAME", "KXNBAGAME"]):
        return _signal_game_outcome(ticker, title, yes_ask)

    # ── Individual player prop markets ────────────────────────────────────────
    if any(k in t for k in ["KXNBA3PT", "KXNBAPTS", "KXNBAAST", "KXNBAREB",
                             "KXMLBHR", "KXMLBHITS", "KXMLBK", "KXNBAPTS"]):
        return _signal_player_prop(ticker, title, yes_ask)

    # ── KXMVE parlays ─────────────────────────────────────────────────────────
    if "KXMVE" in t:
        return _signal_parlay(ticker, title, yes_ask)

    return None


def _signal_game_outcome(ticker, title, yes_ask):
    """Injury-adjusted win probability for a game outcome market."""
    parsed = _parse_game_ticker(ticker)
    sport  = parsed.get("sport")
    this_team = parsed.get("this_team")
    opp_team  = parsed.get("team_a") if parsed.get("team_b") == this_team else parsed.get("team_b")

    if not this_team:
        return None

    # Get injuries for both teams
    this_inj = _get_team_injuries(this_team, sport)
    opp_inj  = _get_team_injuries(opp_team, sport) if opp_team else []

    this_adj = _injury_impact_score(this_inj)
    opp_adj  = _injury_impact_score(opp_inj)

    # Net adjustment: our team worse → lower, opp team worse → higher
    net_adj = this_adj - opp_adj   # negative = bad for our team

    if abs(net_adj) < 0.03:
        return None   # < 3% adjustment = not enough signal

    # Adjusted probability
    adjusted_prob = min(0.97, max(0.03, yes_ask + net_adj))

    # Is it fresh?
    fresh = _has_fresh_signal(this_team, sport) or _has_fresh_signal(opp_team or "", sport)

    # Build detail
    parts = []
    if this_inj:
        hi = [i["player"] for i in this_inj if i["status"] in {"Out","Doubtful"}]
        if hi:
            parts.append(f"{this_team} OUT: {', '.join(hi[:3])}")
    if opp_inj:
        hi = [i["player"] for i in opp_inj if i["status"] in {"Out","Doubtful"}]
        if hi:
            parts.append(f"{opp_team} OUT: {', '.join(hi[:3])}")

    if not parts:
        return None

    detail = f"Injury adj {net_adj:+.1%}: " + " | ".join(parts)

    return {
        "prob":          round(adjusted_prob, 4),
        "source":        "SportsSignals",
        "sport":         sport,
        "this_team":     this_team,
        "detail":        detail,
        "injured_legs":  0,
        "fresh":         fresh,
    }


def _signal_player_prop(ticker, title, yes_ask):
    """If player is injured, YES prop should be near 0."""
    prop = _parse_player_prop(ticker, title)
    if not prop:
        return None

    player = prop["player"]
    inj = _get_player_injury(player)
    if not inj:
        return None

    status = inj.get("status", "")
    detail_txt = inj.get("detail", "")

    if status == "Out":
        prob = 0.02
        detail = f"{player} OUT — won't play ({detail_txt[:60]})"
    elif status == "Doubtful":
        prob = max(0.03, yes_ask * 0.15)
        detail = f"{player} Doubtful — unlikely to play ({detail_txt[:50]})"
    elif status == "Questionable":
        prob = max(0.05, yes_ask * 0.65)
        detail = f"{player} Questionable — reduced confidence"
    else:
        return None

    fresh = _has_fresh_signal_player(player)

    return {
        "prob":         round(prob, 4),
        "source":       "SportsSignals",
        "sport":        "nba" if "NBA" in ticker.upper() else "mlb",
        "player":       player,
        "detail":       detail,
        "injured_legs": 1,
        "fresh":        fresh,
    }


def _signal_parlay(ticker, title, yes_ask):
    """
    If any YES-side player leg has an injured player, parlay YES collapses.
    """
    legs = _parse_parlay_legs(title)
    if not legs:
        return None

    injured_yes_legs = []
    fresh = False

    for leg in legs:
        if leg["side"] != "yes":
            continue
        desc = leg["description"]
        # Only check legs that look like player names (contain space, no numbers)
        # "LaMelo Ball: 3+" → player prop
        # "Charlotte" → team outcome, handled separately
        # "Over 8.5 runs scored" → total, skip
        if re.search(r'\d+\.?\d*\s*(runs|points|goals)', desc.lower()):
            continue  # game total — no player name
        if len(desc.split()) < 2:
            continue  # single word = team name, skip
        if any(w in desc.lower() for w in ["both teams", "over", "under", "wins by"]):
            continue

        # Looks like a player name
        inj = _get_player_injury(desc)
        if inj and inj.get("status") in {"Out", "Doubtful"}:
            injured_yes_legs.append({
                "player": desc,
                "status": inj["status"],
            })
            if _has_fresh_signal_player(desc):
                fresh = True

    if not injured_yes_legs:
        return None

    # Any YES leg failing = whole parlay collapses
    prob = 0.02
    names = ", ".join(f"{l['player']} ({l['status']})" for l in injured_yes_legs[:3])
    detail = f"Parlay collapses — {len(injured_yes_legs)} leg(s) OUT: {names}"

    return {
        "prob":         round(prob, 4),
        "source":       "SportsSignals",
        "sport":        "parlay",
        "detail":       detail,
        "injured_legs": len(injured_yes_legs),
        "fresh":        fresh,
    }


# ── Fresh signal helpers ──────────────────────────────────────────────────────

def _has_fresh_signal(team_code, sport=None):
    """Check if team has a fresh injury signal (< 30min)."""
    try:
        import injury as inj
        # Look up team full name
        for d in [NBA_TEAMS, MLB_TEAMS, NHL_TEAMS]:
            name = d.get(team_code.upper(), "")
            if name:
                with inj._lock:
                    ts = inj._fresh_signals.get(name, 0)
                if time.time() - ts < 1800:
                    return True
    except Exception:
        pass
    return False


def _has_fresh_signal_player(player_name):
    """Check if the player's team has a fresh signal."""
    try:
        import injury as inj
        with inj._lock:
            snap = dict(inj._injuries)
            fresh_snap = dict(inj._fresh_signals)
        name_lower = player_name.lower().strip()
        for team_name, injuries in snap.items():
            for inj_record in injuries:
                if inj_record.get("player", "").lower() == name_lower:
                    ts = fresh_snap.get(team_name, 0)
                    return time.time() - ts < 1800
    except Exception:
        pass
    return False


# ── Public helpers ────────────────────────────────────────────────────────────

def status_summary():
    """For dashboard display."""
    try:
        import injury as inj
        inj_status = inj.status_summary()
        return {
            "total_injuries": inj_status.get("total_injuries", 0),
            "high_impact":    inj_status.get("high_impact", 0),
            "fresh_signals":  inj_status.get("fresh_signals", 0),
        }
    except Exception:
        return {"total_injuries": 0, "high_impact": 0, "fresh_signals": 0}

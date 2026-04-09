"""
Injury Report Scanner — NBA / MLB / NHL

Strategy: injury reports drop at predictable times. Kalshi reprices slowly
(5–20 minutes lag). This module detects fresh injuries and signals the
trading engine to scan aggressively in that window.

Data source: ESPN unofficial API (no key required, widely used).

Injury report windows (US Eastern):
  NBA:   1:30 PM (early report) · 6:30 PM (final, ~90min before tip-off)
  MLB:   5:00–7:00 PM (lineup cards, ~2h before first pitch)
  NHL:   10:00–11:30 AM (morning skate) · 5:00 PM (pre-game)
  NFL:   Wed/Thu/Fri 4:00 PM (practice reports) · Sun morning 11 AM

During these windows the engine shifts from 120s → 15s scan interval.
A fresh OUT/DOUBTFUL signal for a starter drops the window to 8s.
"""

import time
import threading
import requests
from datetime import datetime, timedelta
from collections import defaultdict
try:
    from zoneinfo import ZoneInfo
    ET = ZoneInfo("America/New_York")
except ImportError:
    import pytz
    ET = pytz.timezone("America/New_York")

# ── ESPN endpoints (no auth required) ────────────────────────────────────────
_ESPN_BASE = "https://site.api.espn.com/apis/site/v2/sports"
_ESPN_NEWS = "https://now.core.api.espn.com/v1/sports/news"

SPORT_PATHS = {
    "nba": "basketball/nba",
    "mlb": "baseball/mlb",
    "nhl": "icehockey/nhl",
}

# ── Injury windows (hour_start, min_start, hour_end, min_end) ET ─────────────
INJURY_WINDOWS = [
    (10,  0, 11, 30, "nhl_skate"),
    (13, 30, 14, 30, "nba_early"),
    (15, 45, 16, 30, "nfl_report"),   # NFL Wed/Thu/Fri
    (17,  0, 19,  0, "mlb_lineups"),
    (18, 30, 19, 30, "nba_final"),
    (19,  0, 20,  0, "nhl_pregame"),
]

# Impact threshold — statuses that meaningfully affect team win probability
HIGH_IMPACT = {"Out", "Doubtful"}
MED_IMPACT  = {"Questionable"}

# ── Shared state ──────────────────────────────────────────────────────────────
_lock          = threading.Lock()
_injuries      = {}       # team_display → [{"player", "status", "detail", "ts"}]
_fresh_signals = {}       # team_display → timestamp of last HIGH_IMPACT update
_last_fetch    = 0.0
_fetch_errors  = 0

CACHE_TTL_NORMAL  = 600   # 10 min normal refresh
CACHE_TTL_WINDOW  = 120   # 2 min during injury windows
CACHE_TTL_FRESH   = 60    # 1 min when fresh injury just found


def _now_et():
    return datetime.now(ET)


def in_injury_window():
    """True if current ET time falls inside a known injury report window."""
    now = _now_et()
    mins = now.hour * 60 + now.minute
    for h0, m0, h1, m1, _ in INJURY_WINDOWS:
        if (h0 * 60 + m0) <= mins <= (h1 * 60 + m1):
            return True
    return False


def active_window_name():
    """Return the name of the current window, or None."""
    now = _now_et()
    mins = now.hour * 60 + now.minute
    for h0, m0, h1, m1, name in INJURY_WINDOWS:
        if (h0 * 60 + m0) <= mins <= (h1 * 60 + m1):
            return name
    return None


def has_fresh_signal(max_age_minutes=30):
    """True if any HIGH_IMPACT injury was detected in the last N minutes."""
    cutoff = time.time() - (max_age_minutes * 60)
    with _lock:
        return any(ts > cutoff for ts in _fresh_signals.values())


def recommended_scan_interval(base_interval=120):
    """
    Return the scan interval the engine should use right now.
    Called every loop iteration to adapt dynamically.
    """
    if has_fresh_signal(max_age_minutes=20):
        return 8    # fresh injury detected — scan hard
    if in_injury_window():
        return 15   # inside report window — stay alert
    return base_interval


# ── ESPN fetcher ──────────────────────────────────────────────────────────────

def _fetch_sport_injuries(sport_key):
    """
    Pull injuries for one sport from ESPN.
    Returns list of dicts: {team, player, status, detail}
    """
    path = SPORT_PATHS.get(sport_key, "")
    if not path:
        return []

    url = f"{_ESPN_BASE}/{path}/injuries"
    try:
        r = requests.get(url, timeout=8, headers={"User-Agent": "Mozilla/5.0"})
        if r.status_code != 200:
            return []
        data = r.json()
    except Exception:
        return []

    results = []
    # ESPN injury response: {"injuries": [{"team": {...}, "injuries": [...]}]}
    for team_block in data.get("injuries", []):
        team_info = team_block.get("team", {})
        team_name = team_info.get("displayName", team_info.get("name", ""))
        team_abbr = team_info.get("abbreviation", "")

        for inj in team_block.get("injuries", []):
            athlete = inj.get("athlete", {})
            player  = athlete.get("displayName", "Unknown")
            pos     = (athlete.get("position") or {}).get("abbreviation", "")
            status  = inj.get("status", "")
            detail  = inj.get("longComment") or inj.get("shortComment") or ""

            results.append({
                "sport":  sport_key,
                "team":   team_name,
                "abbr":   team_abbr,
                "player": player,
                "pos":    pos,
                "status": status,
                "detail": detail[:120],
                "ts":     time.time(),
            })

    return results


def _fetch_all():
    """Fetch injuries for all sports in parallel."""
    global _fetch_errors
    from concurrent.futures import ThreadPoolExecutor, as_completed

    all_results = []
    with ThreadPoolExecutor(max_workers=3) as pool:
        futures = {pool.submit(_fetch_sport_injuries, s): s for s in SPORT_PATHS}
        for fut in as_completed(futures):
            try:
                all_results.extend(fut.result())
            except Exception:
                _fetch_errors += 1

    # Group by team
    by_team = defaultdict(list)
    for r in all_results:
        by_team[r["team"]].append(r)

    # Detect fresh HIGH_IMPACT signals — compare vs what we had before
    now = time.time()
    new_fresh = {}
    with _lock:
        prev = dict(_injuries)

    for team, injuries in by_team.items():
        for inj in injuries:
            if inj["status"] in HIGH_IMPACT:
                # Check if this is newer than what we had
                prev_team = prev.get(team, [])
                prev_names = {i["player"] for i in prev_team if i["status"] in HIGH_IMPACT}
                if inj["player"] not in prev_names:
                    new_fresh[team] = now
                    print(f"  🚨 NEW INJURY: {inj['player']} ({team}) — {inj['status']} | {inj['detail'][:60]}")

    with _lock:
        _injuries.update(by_team)
        _fresh_signals.update(new_fresh)
        # Prune stale fresh signals > 3h old
        cutoff = now - 10800
        for k in list(_fresh_signals):
            if _fresh_signals[k] < cutoff:
                del _fresh_signals[k]

    total = sum(len(v) for v in by_team.values())
    if total:
        high = sum(1 for ilist in by_team.values() for i in ilist if i["status"] in HIGH_IMPACT)
        print(f"  🏥 Injuries: {total} total | {high} OUT/Doubtful | "
              f"window={'YES' if in_injury_window() else 'no'}")
    return total


# ── Background loop ───────────────────────────────────────────────────────────

def _bg_loop():
    global _last_fetch
    while True:
        try:
            _fetch_all()
            _last_fetch = time.time()
        except Exception as e:
            print(f"  [Injury] fetch error: {e}")

        # Dynamic sleep — shorter during windows or when fresh signal present
        if has_fresh_signal(20):
            sleep = CACHE_TTL_FRESH
        elif in_injury_window():
            sleep = CACHE_TTL_WINDOW
        else:
            sleep = CACHE_TTL_NORMAL
        time.sleep(sleep)


def start():
    """Start background injury polling. Call once at engine startup."""
    threading.Thread(target=_bg_loop, daemon=True, name="injury-bg").start()
    # Warm immediately
    threading.Thread(target=_fetch_all, daemon=True).start()
    print("  🏥 Injury scanner started (NBA/MLB/NHL)")


# ── Signal API ────────────────────────────────────────────────────────────────

def get_injury_signal(market_title, market_ticker=""):
    """
    Given a Kalshi market title/ticker, returns an injury impact dict:
    {
        "impact":   "high" | "medium" | "none",
        "affected": [{"team", "player", "status", "detail"}],
        "boost":    int,    # score bonus points (0-25)
        "fresh":    bool,   # True if injury < 30 min old
    }

    The 'boost' can be added to the cross-market edge score.
    'fresh' = True is the real signal — means Kalshi likely hasn't repriced yet.
    """
    result = {"impact": "none", "affected": [], "boost": 0, "fresh": False}
    if not market_title:
        return result

    title_lower = market_title.lower()
    now = time.time()
    fresh_cutoff = now - 1800   # 30 minutes

    with _lock:
        injuries_snap  = dict(_injuries)
        fresh_snap     = dict(_fresh_signals)

    affected = []
    for team, injuries in injuries_snap.items():
        team_lower = team.lower()
        # Simple name overlap: does the team appear in the market title?
        team_words = set(team_lower.replace("-", " ").split())
        title_words = set(title_lower.replace("-", " ").split())
        if not team_words & title_words:
            # Try abbreviation match from ticker
            # e.g. KXNBAGAME-PHI-BOS → check PHI and BOS
            if market_ticker:
                parts = market_ticker.upper().split("-")
                team_abbr = (next(
                    (i.get("abbr", "") for i in injuries if i.get("abbr")), ""
                ) or "").upper()
                if team_abbr not in parts:
                    continue
            else:
                continue

        for inj in injuries:
            if inj["status"] in HIGH_IMPACT or inj["status"] in MED_IMPACT:
                affected.append({
                    "team":   team,
                    "player": inj["player"],
                    "pos":    inj.get("pos", ""),
                    "status": inj["status"],
                    "detail": inj.get("detail", ""),
                    "ts":     inj.get("ts", 0),
                })

    if not affected:
        return result

    high = [a for a in affected if a["status"] in HIGH_IMPACT]
    med  = [a for a in affected if a["status"] in MED_IMPACT]

    # Fresh = injury reported in last 30 min AND team has a fresh signal
    team_names = {a["team"] for a in affected}
    is_fresh = any(
        fresh_snap.get(t, 0) > fresh_cutoff for t in team_names
    )

    # Score boost: fresh OUT = 25pts, stale OUT = 15pts, Doubtful = 10pts, Questionable = 5pts
    boost = 0
    if high:
        boost = 25 if is_fresh else 15
    elif med:
        boost = 5

    result.update(
        impact="high" if high else "medium",
        affected=affected[:4],   # cap at 4 for display
        boost=boost,
        fresh=is_fresh,
    )
    return result


def status_summary():
    """Short string for dashboard display."""
    with _lock:
        total = sum(len(v) for v in _injuries.values())
        high  = sum(1 for ilist in _injuries.values()
                    for i in ilist if i["status"] in HIGH_IMPACT)
        fresh = len(_fresh_signals)
    window = active_window_name()
    age = int((time.time() - _last_fetch) / 60) if _last_fetch else -1
    return {
        "total_injuries": total,
        "high_impact":    high,
        "fresh_signals":  fresh,
        "in_window":      window,
        "last_fetch_min": age,
        "errors":         _fetch_errors,
    }

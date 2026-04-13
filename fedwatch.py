"""
CME FedWatch — Fed rate decision probabilities for upcoming FOMC meetings.
No API key required.

Matches Kalshi markets like:
  "Will the Fed cut rates at the May 2025 meeting?"
  "Will the Federal Reserve hold rates in June?"
  "Fed funds rate above 4.5% after March meeting?"

Returns the CME-implied probability the market assigns to each outcome.
Gap between CME probability and Kalshi price = real, measurable edge.
"""

import re
import time
import requests
from datetime import datetime, timezone

# CME FedWatch — derived from 30-Day Fed Funds futures pricing
CME_URL = "https://www.cmegroup.com/CmeWS/mvc/MktData/FedWatch.json"

_session = requests.Session()
_session.headers.update({
    "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36",
    "Referer":    "https://www.cmegroup.com/markets/interest-rates/cme-fedwatch-tool.html",
    "Accept":     "application/json, text/plain, */*",
})

_cache    = []     # list of meeting dicts
_cache_ts = 0.0
CACHE_TTL = 3600   # 1 hour — FOMC probabilities don't move minute-to-minute

MONTHS = {
    "january": "01", "february": "02", "march": "03", "april": "04",
    "may": "05",     "june": "06",     "july": "07",  "august": "08",
    "september": "09", "october": "10", "november": "11", "december": "12",
    "jan": "01", "feb": "02", "mar": "03", "apr": "04",
    "jun": "06", "jul": "07", "aug": "08", "sep": "09",
    "oct": "10", "nov": "11", "dec": "12",
}


def _parse(data):
    """
    Parse CME FedWatch JSON defensively.
    CME has changed this structure before — handle both known formats.
    """
    meetings = []

    # Format A: {"meetings": [{meetingDate, probabilities: [{change, prob}]}]}
    raw = (
        data.get("meetings") or
        data.get("data", {}).get("meetings") or
        []
    )

    for m in raw:
        try:
            date_str = (
                m.get("meetingDate") or
                m.get("meeting_date") or
                m.get("date") or ""
            )[:10]  # YYYY-MM-DD

            probs_raw = m.get("probabilities") or m.get("prob") or []

            hold_p  = 0.0
            cut25_p = 0.0
            cut50_p = 0.0
            hike25_p = 0.0

            for p in probs_raw:
                change = str(p.get("change") or p.get("bps") or "0").strip()
                val    = float(p.get("prob") or p.get("probability") or 0) / 100.0
                if change in ("0", "UNCH", "unchanged"):
                    hold_p = val
                elif change in ("-25", "-0.25"):
                    cut25_p = val
                elif change in ("-50", "-0.50"):
                    cut50_p = val
                elif change in ("+25", "25", "+0.25"):
                    hike25_p = val

            if date_str:
                meetings.append({
                    "date":      date_str[:7],   # YYYY-MM
                    "full_date": date_str,
                    "hold":      round(hold_p,   4),
                    "cut25":     round(cut25_p,  4),
                    "cut50":     round(cut50_p,  4),
                    "hike25":    round(hike25_p, 4),
                    "cut_any":   round(cut25_p + cut50_p, 4),
                    "hike_any":  round(hike25_p, 4),
                    "change_any":round(cut25_p + cut50_p + hike25_p, 4),
                })
        except Exception:
            continue

    return meetings


def _fetch():
    global _cache, _cache_ts
    try:
        r = _session.get(CME_URL, timeout=12)
        if r.ok:
            data = r.json()
            meetings = _parse(data)
            if meetings:
                _cache    = meetings
                _cache_ts = time.time()
                print(f"  [FedWatch] {len(meetings)} FOMC meetings loaded")
                return meetings
            else:
                print(f"  [FedWatch] parsed 0 meetings — response may have changed format")
    except Exception as e:
        print(f"  [FedWatch] fetch error: {e}")
    return _cache  # return stale on error — don't break the bot


def get_meetings():
    """Return cached FOMC probabilities, refreshing if stale."""
    if time.time() - _cache_ts > CACHE_TTL or not _cache:
        return _fetch()
    return _cache


def match_market(kalshi_title):
    """
    Match a Kalshi market title against FOMC meeting probabilities.

    Returns:
        {"prob": float, "signal": "YES"|"NO", "source": "CME FedWatch",
         "meeting": str, "detail": str}
    or None if no match found.
    """
    title = kalshi_title.lower()

    # Must be a Fed/rate market
    fed_kws = ["fed", "federal reserve", "fomc", "rate", "funds", "basis point", "bps",
               "interest", "monetary", "cut", "hike", "hold", "raise", "lower"]
    if not any(k in title for k in fed_kws):
        return None

    meetings = get_meetings()
    if not meetings:
        return None

    # Determine what the market is asking
    is_cut   = any(w in title for w in ["cut", "lower", "decrease", "reduce", "below"])
    is_hike  = any(w in title for w in ["raise", "hike", "increase", "higher", "above"])
    is_hold  = any(w in title for w in ["hold", "unchanged", "pause", "maintain", "same"])
    is_change = any(w in title for w in ["change", "move", "adjust", "different"])

    # Find which meeting the market refers to
    target = None
    for month_name, month_num in MONTHS.items():
        if month_name in title:
            year_match = re.search(r'\b(202[5-9])\b', title)
            year = year_match.group(1) if year_match else str(datetime.now().year)
            ym = f"{year}-{month_num}"
            for m in meetings:
                if m["date"] == ym:
                    target = m
                    break
            if target:
                break

    # Fall back to next upcoming meeting
    if not target:
        today = datetime.now(timezone.utc).strftime("%Y-%m")
        upcoming = [m for m in meetings if m["date"] >= today]
        target = upcoming[0] if upcoming else (meetings[0] if meetings else None)

    if not target:
        return None

    # Assign yes_prob based on what market is asking
    if is_cut:
        yes_prob = target["cut_any"]
        detail   = f"cut_any={target['cut_any']:.1%}"
    elif is_hike:
        yes_prob = target["hike_any"]
        detail   = f"hike_any={target['hike_any']:.1%}"
    elif is_hold:
        yes_prob = target["hold"]
        detail   = f"hold={target['hold']:.1%}"
    elif is_change:
        yes_prob = target["change_any"]
        detail   = f"change_any={target['change_any']:.1%}"
    else:
        yes_prob = target["hold"]   # generic Fed market — default to hold
        detail   = f"hold(default)={target['hold']:.1%}"

    return {
        "prob":    yes_prob,
        "source":  "CME FedWatch",
        "meeting": target["full_date"],
        "detail":  detail,
    }

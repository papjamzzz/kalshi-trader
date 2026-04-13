"""
NOAA / NWS Weather Client — forecast probabilities for major US cities.
No API key required. Uses api.weather.gov (official NWS API).

Matches Kalshi weather markets like:
  "Will it snow in NYC on April 14?"
  "Will the high temperature in Chicago exceed 75°F on April 15?"
  "Will Miami receive more than 0.5 inches of rain this week?"

Strategy: NWS forecasts are highly accurate (better than the crowd).
When Kalshi prices a weather event at 40% and NWS says 70%, that 30-point
gap is real edge — the market is slow to reprice official forecasts.
"""

import re
import time
import threading
import requests
from datetime import datetime, timezone, timedelta

BASE = "https://api.weather.gov"

_session = requests.Session()
_session.headers.update({
    "User-Agent":  "KKTrader/1.0 (kalshi prediction market bot; contact: underwaterfile@proton.me)",
    "Accept":      "application/geo+json",
})

# ── City grid definitions ─────────────────────────────────────────────────────
# Format: city_name → {"lat": float, "lon": float, "aliases": [...]}
# Grid coords fetched once and cached.

CITIES = {
    "new york":     {"lat": 40.7128, "lon": -74.0060, "aliases": ["nyc", "new york city", "manhattan", "brooklyn"]},
    "los angeles":  {"lat": 34.0522, "lon": -118.2437, "aliases": ["la", "lax", "socal"]},
    "chicago":      {"lat": 41.8781, "lon": -87.6298,  "aliases": ["chi", "chicago"]},
    "houston":      {"lat": 29.7604, "lon": -95.3698,  "aliases": []},
    "miami":        {"lat": 25.7617, "lon": -80.1918,  "aliases": ["south florida"]},
    "seattle":      {"lat": 47.6062, "lon": -122.3321, "aliases": ["sea"]},
    "dallas":       {"lat": 32.7767, "lon": -96.7970,  "aliases": ["dfw", "fort worth"]},
    "boston":       {"lat": 42.3601, "lon": -71.0589,  "aliases": ["bos"]},
    "denver":       {"lat": 39.7392, "lon": -104.9903, "aliases": ["den"]},
    "atlanta":      {"lat": 33.7490, "lon": -84.3880,  "aliases": ["atl"]},
    "phoenix":      {"lat": 33.4484, "lon": -112.0740, "aliases": ["phx"]},
    "philadelphia": {"lat": 39.9526, "lon": -75.1652,  "aliases": ["philly", "phl"]},
    "washington":   {"lat": 38.9072, "lon": -77.0369,  "aliases": ["dc", "d.c.", "washington dc"]},
    "las vegas":    {"lat": 36.1699, "lon": -115.1398, "aliases": ["vegas", "lvs"]},
    "minneapolis":  {"lat": 44.9778, "lon": -93.2650,  "aliases": ["minn", "twin cities"]},
}

_grid_cache   = {}    # city_name → {"office": str, "gridX": int, "gridY": int}
_forecasts    = {}    # city_name → list of period dicts
_forecast_ts  = {}    # city_name → last fetch unix timestamp
_lock = threading.Lock()

FORECAST_TTL = 3600   # 1 hour — NWS updates hourly


# ── Grid lookup ───────────────────────────────────────────────────────────────

def _get_grid(city, lat, lon):
    """Fetch NWS gridpoint for a lat/lon. Cached — only called once per city."""
    if city in _grid_cache:
        return _grid_cache[city]
    try:
        r = _session.get(f"{BASE}/points/{lat},{lon}", timeout=8)
        if r.ok:
            props = r.json().get("properties", {})
            grid = {
                "office": props.get("gridId", ""),
                "gridX":  props.get("gridX", 0),
                "gridY":  props.get("gridY", 0),
                "forecast_url": props.get("forecast", ""),
            }
            _grid_cache[city] = grid
            return grid
    except Exception as e:
        print(f"  [NOAA] grid lookup failed for {city}: {e}")
    return None


def _get_forecast(city):
    """Fetch 7-day forecast for a city. Returns list of period dicts."""
    info = CITIES.get(city)
    if not info:
        return []

    grid = _get_grid(city, info["lat"], info["lon"])
    if not grid or not grid.get("forecast_url"):
        return []

    try:
        r = _session.get(grid["forecast_url"], timeout=10)
        if r.ok:
            periods = r.json().get("properties", {}).get("periods", [])
            parsed = []
            for p in periods:
                parsed.append({
                    "name":         p.get("name", ""),
                    "start":        p.get("startTime", ""),
                    "end":          p.get("endTime", ""),
                    "is_daytime":   p.get("isDaytime", True),
                    "temp_f":       p.get("temperature"),
                    "temp_unit":    p.get("temperatureUnit", "F"),
                    "precip_pct":   p.get("probabilityOfPrecipitation", {}).get("value") or 0,
                    "short_forecast": p.get("shortForecast", ""),
                    "detailed":     p.get("detailedForecast", ""),
                    "wind_speed":   p.get("windSpeed", ""),
                })
            return parsed
    except Exception as e:
        print(f"  [NOAA] forecast fetch failed for {city}: {e}")
    return []


# ── Background refresh ────────────────────────────────────────────────────────

def _refresh_all():
    """Refresh forecasts for all cities."""
    for city in CITIES:
        now = time.time()
        last = _forecast_ts.get(city, 0)
        if now - last < FORECAST_TTL:
            continue
        fc = _get_forecast(city)
        if fc:
            with _lock:
                _forecasts[city] = fc
                _forecast_ts[city] = now
    print(f"  [NOAA] Forecasts refreshed for {len(_forecasts)} cities")


def start():
    """Start background NOAA refresh thread."""
    def loop():
        _refresh_all()
        while True:
            time.sleep(FORECAST_TTL)
            _refresh_all()
    threading.Thread(target=loop, daemon=True, name="noaa-bg").start()
    print("  [NOAA] Background forecast thread started")


# ── Market matching ───────────────────────────────────────────────────────────

def _detect_city(title):
    """Find which city a market title refers to."""
    tl = title.lower()
    for city, info in CITIES.items():
        if city in tl:
            return city
        for alias in info.get("aliases", []):
            if alias in tl:
                return city
    return None


def _detect_date(title):
    """
    Extract a target date from a market title.
    Looks for: 'April 14', 'Apr 14', 'April 14, 2025', 'tomorrow', 'today', 'this week'
    Returns datetime.date or None.
    """
    tl = title.lower()
    today = datetime.now(timezone.utc).date()

    if "today" in tl:
        return today
    if "tomorrow" in tl:
        return today + timedelta(days=1)
    if "this week" in tl or "week" in tl:
        return today + timedelta(days=3)   # midpoint of week

    months = {
        "january":1,"february":2,"march":3,"april":4,"may":5,"june":6,
        "july":7,"august":8,"september":9,"october":10,"november":11,"december":12,
        "jan":1,"feb":2,"mar":3,"apr":4,"jun":6,"jul":7,"aug":8,
        "sep":9,"oct":10,"nov":11,"dec":12,
    }

    for month_name, month_num in months.items():
        if month_name in tl:
            day_match = re.search(r'\b(\d{1,2})\b', tl[tl.index(month_name):])
            if day_match:
                day = int(day_match.group(1))
                year = today.year
                try:
                    return datetime(year, month_num, day).date()
                except ValueError:
                    pass
    return None


def _periods_for_date(periods, target_date):
    """Filter forecast periods that cover the target date."""
    matching = []
    for p in periods:
        try:
            start = datetime.fromisoformat(p["start"].replace("Z", "+00:00")).date()
            if start == target_date:
                matching.append(p)
        except Exception:
            continue
    return matching


def match_market(kalshi_title):
    """
    Match a Kalshi weather market and return a probability signal.

    Returns:
        {"prob": float, "signal": "YES"|"NO", "source": "NOAA/NWS",
         "city": str, "detail": str}
    or None if no match.
    """
    title = kalshi_title.lower()

    # Must be a weather market
    weather_kws = ["snow", "rain", "temperature", "temp", "precip", "precipitation",
                   "storm", "wind", "heat", "cold", "freeze", "ice", "hurricane",
                   "tornado", "weather", "forecast", "high", "low", "degree", "inch",
                   "above", "below", "exceed"]
    if not any(k in title for k in weather_kws):
        return None

    city = _detect_city(kalshi_title)
    if not city:
        return None

    with _lock:
        periods = list(_forecasts.get(city, []))

    if not periods:
        # Try a fresh fetch
        fc = _get_forecast(city)
        if fc:
            with _lock:
                _forecasts[city] = fc
                _forecast_ts[city] = time.time()
            periods = fc

    if not periods:
        return None

    target_date = _detect_date(kalshi_title)
    if target_date:
        day_periods = _periods_for_date(periods, target_date)
    else:
        day_periods = periods[:2]   # default to next 12-24h

    if not day_periods:
        day_periods = periods[:2]

    # ── Determine what the market is asking ──────────────────────────────────

    is_snow    = any(w in title for w in ["snow", "snowfall", "blizzard"])
    is_rain    = any(w in title for w in ["rain", "precipitation", "precip"])
    is_temp_hi = any(w in title for w in ["high temperature", "high temp", "above", "exceed", "over"])
    is_temp_lo = any(w in title for w in ["low temperature", "low temp", "below", "under", "freeze"])

    # Average precip probability across matching periods
    precip_probs = [p["precip_pct"] / 100.0 for p in day_periods if p.get("precip_pct") is not None]
    avg_precip = sum(precip_probs) / len(precip_probs) if precip_probs else None

    # Average temperature
    temps = [p["temp_f"] for p in day_periods if p.get("temp_f") is not None]
    avg_temp = sum(temps) / len(temps) if temps else None

    # Snow: look for "snow" keyword in forecast text
    snow_mentioned = any(
        "snow" in p.get("short_forecast", "").lower() or
        "snow" in p.get("detailed", "").lower()
        for p in day_periods
    )

    # ── Build probability ─────────────────────────────────────────────────────
    yes_prob = None
    detail   = ""

    if is_snow:
        if snow_mentioned and avg_precip is not None:
            yes_prob = min(avg_precip * 1.1, 1.0)   # slight boost when explicitly forecast
            detail = f"snow mentioned in forecast, precip={avg_precip:.0%}"
        elif avg_precip is not None:
            yes_prob = avg_precip * 0.4   # snow is subset of all precip
            detail = f"no snow in forecast, precip={avg_precip:.0%}"
        else:
            yes_prob = 0.05 if not snow_mentioned else 0.5
            detail = f"snow={'yes' if snow_mentioned else 'no'} in forecast"

    elif is_rain:
        if avg_precip is not None:
            yes_prob = avg_precip
            detail = f"precip_prob={avg_precip:.0%}"

    elif is_temp_hi and avg_temp is not None:
        # Extract the threshold from the title e.g. "above 75°F"
        thresh_match = re.search(r'(\d+)\s*[°]?\s*[fF]', kalshi_title)
        if thresh_match:
            thresh = float(thresh_match.group(1))
            # Simple logistic-style estimate based on forecast vs threshold
            diff = avg_temp - thresh
            if diff >= 10:
                yes_prob = 0.92
            elif diff >= 5:
                yes_prob = 0.78
            elif diff >= 0:
                yes_prob = 0.58
            elif diff >= -5:
                yes_prob = 0.30
            else:
                yes_prob = 0.10
            detail = f"forecast_high={avg_temp:.0f}°F vs threshold={thresh:.0f}°F"

    elif is_temp_lo and avg_temp is not None:
        thresh_match = re.search(r'(\d+)\s*[°]?\s*[fF]', kalshi_title)
        if thresh_match:
            thresh = float(thresh_match.group(1))
            diff = thresh - avg_temp
            if diff >= 10:
                yes_prob = 0.92
            elif diff >= 5:
                yes_prob = 0.78
            elif diff >= 0:
                yes_prob = 0.58
            elif diff >= -5:
                yes_prob = 0.30
            else:
                yes_prob = 0.10
            detail = f"forecast_low={avg_temp:.0f}°F vs threshold={thresh:.0f}°F"

    if yes_prob is None:
        return None

    return {
        "prob":   round(yes_prob, 4),
        "source": "NOAA/NWS",
        "city":   city,
        "detail": detail,
    }

"""
NDFD — National Digital Forecast Database (via NWS API)
Hourly gridded forecast data for major US cities.

Complements noaa.py (12-hour periods) with hourly precision:
  - Exact temperature by hour
  - Hourly precipitation probability (%)
  - Wind speed (mph)
  - Relative humidity (%)
  - Dew point

Why NDFD beats regular NWS forecast for Kalshi:
  Kalshi weather markets resolve at a specific hour (e.g. "high temp on Apr 14").
  NWS 12-hour periods say "High: 72°F" — but NDFD says "3pm=74°F, 4pm=71°F, 5pm=68°F".
  That hourly granularity directly maps to Kalshi's daily high/low/rain questions.

API: api.weather.gov/gridpoints/{office}/{gridX},{gridY}/forecast/hourly
No key required. Same grid lookup as noaa.py.
"""

import re
import time
import threading
import requests
from datetime import datetime, timezone, timedelta

BASE = "https://api.weather.gov"

_session = requests.Session()
_session.headers.update({
    "User-Agent": "KKTrader/1.0 (kalshi weather; underwaterfile@proton.me)",
    "Accept":     "application/geo+json",
})

# ── Same cities as noaa.py ────────────────────────────────────────────────────
CITIES = {
    "new york":     {"lat": 40.7128, "lon": -74.0060, "aliases": ["nyc", "new york city", "manhattan"]},
    "los angeles":  {"lat": 34.0522, "lon": -118.2437, "aliases": ["la", "lax"]},
    "chicago":      {"lat": 41.8781, "lon": -87.6298,  "aliases": ["chi"]},
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
    "san antonio":  {"lat": 29.4241, "lon": -98.4936,  "aliases": ["satx", "san antonio"]},
    "oklahoma city":{"lat": 35.4676, "lon": -97.5164,  "aliases": ["okc", "oklahoma"]},
    "new orleans":  {"lat": 29.9511, "lon": -90.0715,  "aliases": ["nola", "new orleans"]},
    "san francisco":{"lat": 37.7749, "lon": -122.4194, "aliases": ["sfo", "sf"]},
}

_grid_cache  = {}   # city → {office, gridX, gridY, hourly_url}
_hourly      = {}   # city → list of hourly period dicts
_hourly_ts   = {}   # city → unix timestamp of last fetch
_lock = threading.Lock()

HOURLY_TTL = 3600   # refresh every hour — NWS updates hourly


# ── Grid + hourly URL ─────────────────────────────────────────────────────────

def _get_grid(city, lat, lon):
    if city in _grid_cache:
        return _grid_cache[city]
    try:
        r = _session.get(f"{BASE}/points/{lat},{lon}", timeout=8)
        if r.ok:
            props = r.json().get("properties", {})
            grid = {
                "office":      props.get("gridId", ""),
                "gridX":       props.get("gridX", 0),
                "gridY":       props.get("gridY", 0),
                "hourly_url":  props.get("forecastHourly", ""),
            }
            _grid_cache[city] = grid
            return grid
    except Exception as e:
        print(f"  [NDFD] grid lookup failed for {city}: {e}")
    return None


def _fetch_hourly(city):
    """Fetch hourly forecast for a city. Returns list of hourly dicts."""
    info = CITIES.get(city)
    if not info:
        return []
    grid = _get_grid(city, info["lat"], info["lon"])
    if not grid or not grid.get("hourly_url"):
        return []
    try:
        r = _session.get(grid["hourly_url"], timeout=12)
        if not r.ok:
            return []
        periods = r.json().get("properties", {}).get("periods", [])
        parsed = []
        for p in periods:
            # Extract numeric wind speed (e.g. "12 mph" → 12)
            wind_raw = p.get("windSpeed", "0 mph")
            wind_mph = 0
            wm = re.search(r"(\d+)", str(wind_raw))
            if wm:
                wind_mph = int(wm.group(1))

            # Humidity and dew point — only available in some periods
            humidity = None
            dp       = None
            for prop in (p.get("relativeHumidity") or {}, ):
                v = prop.get("value")
                if v is not None:
                    humidity = float(v)
            for prop in (p.get("dewpoint") or {}, ):
                v = prop.get("value")
                if v is not None:
                    # NWS returns Celsius — convert
                    dp = round(v * 9/5 + 32, 1)

            parsed.append({
                "start":       p.get("startTime", ""),
                "end":         p.get("endTime", ""),
                "is_daytime":  p.get("isDaytime", True),
                "temp_f":      p.get("temperature"),
                "precip_pct":  (p.get("probabilityOfPrecipitation") or {}).get("value") or 0,
                "wind_mph":    wind_mph,
                "humidity":    humidity,
                "dew_point_f": dp,
                "short_forecast": p.get("shortForecast", ""),
            })
        return parsed
    except Exception as e:
        print(f"  [NDFD] hourly fetch failed for {city}: {e}")
    return []


# ── Background refresh ────────────────────────────────────────────────────────

def _refresh_all():
    count = 0
    for city in CITIES:
        now  = time.time()
        last = _hourly_ts.get(city, 0)
        if now - last < HOURLY_TTL:
            continue
        data = _fetch_hourly(city)
        if data:
            with _lock:
                _hourly[city]    = data
                _hourly_ts[city] = now
            count += 1
    if count:
        print(f"  [NDFD] Hourly forecasts refreshed for {count} cities ({len(_hourly)} total)")


def start():
    def loop():
        _refresh_all()
        while True:
            time.sleep(HOURLY_TTL)
            _refresh_all()
    threading.Thread(target=loop, daemon=True, name="ndfd-bg").start()
    print("  [NDFD] Hourly forecast thread started")


# ── Market matching ───────────────────────────────────────────────────────────

def _detect_city(title):
    tl = title.lower()
    for city, info in CITIES.items():
        if city in tl:
            return city
        for alias in info.get("aliases", []):
            if alias in tl:
                return city
    return None


def _detect_date(title):
    tl    = title.lower()
    today = datetime.now(timezone.utc).date()
    if "today" in tl:
        return today
    if "tomorrow" in tl:
        return today + timedelta(days=1)
    months = {
        "january":1,"february":2,"march":3,"april":4,"may":5,"june":6,
        "july":7,"august":8,"september":9,"october":10,"november":11,"december":12,
        "jan":1,"feb":2,"mar":3,"apr":4,"jun":6,"jul":7,"aug":8,
        "sep":9,"oct":10,"nov":11,"dec":12,
    }
    for mn, mv in months.items():
        if mn in tl:
            idx = tl.index(mn)
            dm  = re.search(r'\b(\d{1,2})\b', tl[idx:])
            if dm:
                try:
                    return datetime(today.year, mv, int(dm.group(1))).date()
                except ValueError:
                    pass
    return None


def _hours_for_date(periods, target_date):
    """Return hourly periods that fall on target_date (UTC)."""
    out = []
    for p in periods:
        try:
            dt = datetime.fromisoformat(p["start"].replace("Z", "+00:00"))
            if dt.date() == target_date:
                out.append(p)
        except Exception:
            pass
    return out


def match_market(kalshi_title):
    """
    Match a Kalshi weather market against NDFD hourly data.

    Returns:
        {"prob": float, "signal": str, "source": "NDFD",
         "city": str, "detail": str, "fields": dict}
    or None if no match.

    Precision over noaa.match_market:
      - Uses per-hour temperature → exact daily high/low
      - Precipitation probability per hour → better YES/NO for rain
      - Wind and humidity available for future extension
    """
    title = kalshi_title.lower()

    # Must look like a weather market
    weather_kws = ["temperature", "temp", "rain", "snow", "precip", "precipitation",
                   "wind", "heat", "cold", "freeze", "ice", "storm", "highest", "lowest",
                   "high", "low", "above", "below", "exceed", "inch", "degree", "humid"]
    if not any(k in title for k in weather_kws):
        return None

    city = _detect_city(kalshi_title)
    if not city:
        return None

    with _lock:
        periods = list(_hourly.get(city, []))

    if not periods:
        data = _fetch_hourly(city)
        if data:
            with _lock:
                _hourly[city]    = data
                _hourly_ts[city] = time.time()
            periods = data
    if not periods:
        return None

    target_date = _detect_date(kalshi_title)
    if target_date:
        day_hours = _hours_for_date(periods, target_date)
    else:
        # Default: next 24 hours
        day_hours = periods[:24]
    if not day_hours:
        day_hours = periods[:12]

    # ── Extract structured fields ─────────────────────────────────────────────
    temps     = [p["temp_f"] for p in day_hours if p.get("temp_f") is not None]
    precips   = [p["precip_pct"] for p in day_hours if p.get("precip_pct") is not None]
    winds     = [p["wind_mph"] for p in day_hours if p.get("wind_mph") is not None]
    humids    = [p["humidity"] for p in day_hours if p.get("humidity") is not None]

    daily_high  = max(temps)  if temps  else None
    daily_low   = min(temps)  if temps  else None
    max_precip  = max(precips) if precips else 0
    avg_precip  = sum(precips) / len(precips) if precips else 0
    max_wind    = max(winds)   if winds  else None
    avg_humidity= sum(humids) / len(humids) if humids else None

    snow_mentioned = any(
        "snow" in p.get("short_forecast", "").lower()
        for p in day_hours
    )

    # ── Market type detection ─────────────────────────────────────────────────
    is_temp_hi = any(w in title for w in ["highest temperature", "high temperature",
                                           "high temp", "above", "exceed", "over",
                                           "highest temp"])
    is_temp_lo = any(w in title for w in ["lowest temperature", "low temperature",
                                           "low temp", "below", "under", "freeze",
                                           "lowest temp"])
    is_rain    = any(w in title for w in ["rain", "precipitation", "precip", "inches"])
    is_snow    = any(w in title for w in ["snow", "snowfall", "blizzard"])

    yes_prob = None
    detail   = ""
    fields   = {
        "daily_high_f":  daily_high,
        "daily_low_f":   daily_low,
        "max_precip_pct": max_precip,
        "avg_precip_pct": avg_precip,
        "max_wind_mph":  max_wind,
        "avg_humidity":  avg_humidity,
        "snow_in_forecast": snow_mentioned,
        "hours_sampled": len(day_hours),
    }

    # ── Temperature high ─────────────────────────────────────────────────────
    if is_temp_hi and daily_high is not None:
        thresh_m = re.search(r'(\d+)\s*[°]?\s*[fF]', kalshi_title)
        if thresh_m:
            thresh  = float(thresh_m.group(1))
            diff    = daily_high - thresh
            # Sharper curve than NWS 12h because we have exact hourly high
            if diff >= 8:
                yes_prob = 0.95
            elif diff >= 4:
                yes_prob = 0.82
            elif diff >= 1:
                yes_prob = 0.65
            elif diff >= -1:
                yes_prob = 0.50
            elif diff >= -4:
                yes_prob = 0.30
            elif diff >= -8:
                yes_prob = 0.12
            else:
                yes_prob = 0.04
            detail = f"NDFD hourly high={daily_high:.0f}°F vs threshold={thresh:.0f}°F (diff={diff:+.0f})"

    # ── Temperature low ──────────────────────────────────────────────────────
    elif is_temp_lo and daily_low is not None:
        thresh_m = re.search(r'(\d+)\s*[°]?\s*[fF]', kalshi_title)
        if thresh_m:
            thresh = float(thresh_m.group(1))
            diff   = thresh - daily_low   # positive = low below threshold = YES
            if diff >= 8:
                yes_prob = 0.95
            elif diff >= 4:
                yes_prob = 0.82
            elif diff >= 1:
                yes_prob = 0.65
            elif diff >= -1:
                yes_prob = 0.50
            elif diff >= -4:
                yes_prob = 0.30
            elif diff >= -8:
                yes_prob = 0.12
            else:
                yes_prob = 0.04
            detail = f"NDFD hourly low={daily_low:.0f}°F vs threshold={thresh:.0f}°F (diff={diff:+.0f})"

    # ── Rain ─────────────────────────────────────────────────────────────────
    elif is_rain:
        # Use max hourly precip probability for binary "will it rain" questions
        yes_prob = max_precip / 100.0
        detail   = f"NDFD max_hourly_precip={max_precip:.0f}%  avg={avg_precip:.0f}%"

    # ── Snow ─────────────────────────────────────────────────────────────────
    elif is_snow:
        if snow_mentioned:
            yes_prob = min((max_precip / 100.0) * 1.1, 1.0)
            detail   = f"NDFD snow in forecast, max_precip={max_precip:.0f}%"
        else:
            yes_prob = (max_precip / 100.0) * 0.3
            detail   = f"NDFD no snow forecast, max_precip={max_precip:.0f}%"

    if yes_prob is None:
        return None

    return {
        "prob":   round(yes_prob, 4),
        "source": "NDFD",
        "city":   city,
        "detail": detail,
        "fields": fields,
    }


# ── Public helpers ────────────────────────────────────────────────────────────

def status_summary():
    """Returns how many cities have hourly data loaded."""
    with _lock:
        cities_loaded = len(_hourly)
        oldest = min(_hourly_ts.values()) if _hourly_ts else 0
    age_min = round((time.time() - oldest) / 60) if oldest else None
    return {
        "cities_loaded": cities_loaded,
        "age_min": age_min,
    }

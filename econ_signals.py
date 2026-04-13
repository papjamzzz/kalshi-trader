"""
Econ Signals — Macro data edge for Kalshi econ markets.

Strategy: pull actual release data, compute trend projections for next print,
compare to what Kalshi is pricing. When gap > threshold → real edge.

Sources (all free, no API key required):
  1. BLS API        — CPI, Core CPI, Unemployment, Nonfarm Payrolls (official)
  2. FRED CSV       — GDP, Fed Funds Rate, PCE (St. Louis Fed, no key needed)
  3. Atlanta GDPNow — live GDP nowcast from Atlanta Fed page (scraped)
  4. BEA API        — GDP growth rates (free guest access)

Kalshi econ market types we match:
  - KXCPIYOY-*     — CPI year-over-year (e.g. "Will inflation be above 3.0%?")
  - KXECONSTATCPI-*— CPI month-over-month
  - KXECONSTATU3-* — Unemployment rate (e.g. "Will unemployment be above 4.5%?")
  - KXU3-*         — Same
  - KXGDP-*        — GDP growth (e.g. "Will GDP be above 2.5%?")
  - KXFEDDECISION-*— Fed rate decision (CME FedWatch handles this better — we augment)
  - KXPCECORE-*    — Core PCE inflation

Architecture:
  - Background thread refreshes data every 4 hours (releases are monthly)
  - match_market() returns probability estimate for any Kalshi econ market
  - No FRED API key needed — uses public CSV endpoint
"""

import re
import time
import threading
import requests
from datetime import datetime, timezone, timedelta, date

_session = requests.Session()
_session.headers.update({
    "User-Agent": "KKTrader/1.0 (macro econ signals; underwaterfile@proton.me)",
    "Content-Type": "application/json",
})

# ── Cache ─────────────────────────────────────────────────────────────────────
_lock   = threading.Lock()
_data   = {}   # series_id → {"values": [...(date, float)], "ts": float}
_gdpnow = {"value": None, "ts": 0.0}

REFRESH_TTL = 4 * 3600   # 4 hours — monthly releases don't change intra-day

# ── BLS Series we care about ──────────────────────────────────────────────────
BLS_SERIES = {
    "cpi_all":    "CUUR0000SA0",    # CPI All Urban Consumers (not seasonally adj)
    "cpi_core":   "CUUR0000SA0L1E", # Core CPI (excl food & energy)
    "unrate":     "LNS14000000",    # Unemployment rate (SA)
    "payrolls":   "CES0000000001",  # Total Nonfarm Payrolls (SA, thousands)
}

# ── FRED series (keyless CSV) ─────────────────────────────────────────────────
FRED_SERIES = {
    "cpi_fred":    "CPIAUCSL",   # CPI All Urban (SA) — crosscheck BLS
    "pce_core":    "PCEPILFE",   # Core PCE Price Index
    "gdp":         "GDP",        # Nominal GDP (quarterly, billions)
    "gdp_real":    "GDPC1",      # Real GDP (quarterly, chained 2017$)
    "fedfunds":    "FEDFUNDS",   # Effective Fed Funds Rate
    "gdp_growth":  "A191RL1Q225SBEA",  # Real GDP % change SAAR
}


# ── BLS Fetcher ───────────────────────────────────────────────────────────────

def _fetch_bls():
    """Fetch last 24 months for all BLS series in one request."""
    current_year = datetime.now().year
    payload = {
        "seriesid": list(BLS_SERIES.values()),
        "startyear": str(current_year - 2),
        "endyear":   str(current_year),
    }
    try:
        r = _session.post(
            "https://api.bls.gov/publicAPI/v2/timeseries/data/",
            json=payload,
            timeout=15,
        )
        if not r.ok:
            return
        raw = r.json()
        series_list = raw.get("Results", {}).get("series", [])
        for s in series_list:
            sid  = s["seriesID"]
            name = next((k for k, v in BLS_SERIES.items() if v == sid), sid)
            vals = []
            for d in s.get("data", []):
                year  = int(d["year"])
                month = int(d["period"].replace("M", ""))
                try:
                    dt  = date(year, month, 1)
                    val = float(d["value"])
                    vals.append((dt, val))
                except Exception:
                    pass
            vals.sort(key=lambda x: x[0])
            with _lock:
                _data[name] = {"values": vals, "ts": time.time()}
        print(f"  [Econ] BLS: loaded {len(series_list)} series")
    except Exception as e:
        print(f"  [Econ] BLS fetch error: {e}")


# ── FRED Fetcher ──────────────────────────────────────────────────────────────

def _fetch_fred():
    """Fetch FRED series via public CSV endpoint (no API key)."""
    loaded = 0
    for name, series_id in FRED_SERIES.items():
        try:
            r = _session.get(
                f"https://fred.stlouisfed.org/graph/fredgraph.csv?id={series_id}",
                timeout=10,
            )
            if not r.ok:
                continue
            vals = []
            for line in r.text.strip().split("\n")[1:]:   # skip header
                parts = line.split(",")
                if len(parts) < 2:
                    continue
                try:
                    dt  = datetime.strptime(parts[0].strip(), "%Y-%m-%d").date()
                    val = float(parts[1].strip())
                    vals.append((dt, val))
                except Exception:
                    pass
            if vals:
                with _lock:
                    _data[name] = {"values": vals, "ts": time.time()}
                loaded += 1
        except Exception as e:
            print(f"  [Econ] FRED {name} error: {e}")
    print(f"  [Econ] FRED: loaded {loaded}/{len(FRED_SERIES)} series")


# ── Atlanta Fed GDPNow ────────────────────────────────────────────────────────

def _fetch_gdpnow():
    """Scrape Atlanta Fed GDPNow latest estimate from their public page."""
    try:
        r = _session.get(
            "https://www.atlantafed.org/cqer/research/gdpnow",
            timeout=12,
        )
        if not r.ok:
            return

        text = r.text

        # Multiple patterns — try each, take first match
        patterns = [
            r'GDPNow[^<]{0,200}?([-\d.]+)\s*percent',
            r'latest\s+estimate[^<]{0,100}?([-\d.]+)\s*percent',
            r'model\s+estimate[^<]{0,100}?([-\d.]+)\s*percent',
            # Their page sometimes embeds as: "X.X%" near "Q1 2026"
            r'Q[1-4]\s+\d{4}[^<]{0,100}?([-\d.]+)\s*%',
            # Fallback: grab standalone percentage near "GDP"
            r'(?:GDP|growth)[^<]{0,150}?([-\d.]+)\s*%',
        ]
        for pat in patterns:
            m = re.search(pat, text, re.IGNORECASE)
            if m:
                val = float(m.group(1))
                if -10 < val < 20:   # sanity check: GDP growth -10 to +20%
                    with _lock:
                        _gdpnow["value"] = val
                        _gdpnow["ts"]    = time.time()
                    print(f"  [Econ] GDPNow: {val:.1f}%")
                    return

        print("  [Econ] GDPNow: could not parse value from page")
    except Exception as e:
        print(f"  [Econ] GDPNow fetch error: {e}")


# ── Projection helpers ────────────────────────────────────────────────────────

def _latest(name, n=1):
    """Return the last n values for a series as (date, value) tuples."""
    with _lock:
        entry = _data.get(name)
    if not entry:
        return []
    return entry["values"][-n:]


def _project_next_month(name, months_back=6):
    """
    Simple trend projection: average MoM change over last N months.
    Returns projected next value, or None if insufficient data.
    """
    with _lock:
        entry = _data.get(name)
    if not entry or len(entry["values"]) < months_back + 1:
        return None
    vals = entry["values"][-(months_back + 1):]
    changes = []
    for i in range(1, len(vals)):
        prev, curr = vals[i-1][1], vals[i][1]
        if prev > 0:
            changes.append((curr - prev) / prev)
    if not changes:
        return None
    avg_change = sum(changes) / len(changes)
    last_val   = vals[-1][1]
    return last_val * (1 + avg_change)


def _yoy_change(name):
    """Return year-over-year % change from latest available month."""
    with _lock:
        entry = _data.get(name)
    if not entry or len(entry["values"]) < 13:
        return None
    latest_date, latest_val = entry["values"][-1]
    # Find same month prior year
    target = date(latest_date.year - 1, latest_date.month, 1)
    prior_val = None
    for d, v in entry["values"]:
        if d == target:
            prior_val = v
            break
    if prior_val is None or prior_val == 0:
        return None
    return (latest_val - prior_val) / prior_val * 100


def _projected_yoy(name):
    """Project next month's YoY using trend projection."""
    with _lock:
        entry = _data.get(name)
    if not entry or len(entry["values"]) < 13:
        return None
    latest_date = entry["values"][-1][0]
    projected   = _project_next_month(name)
    if projected is None:
        return None
    # Prior year value for next month
    next_month = latest_date.month + 1 if latest_date.month < 12 else 1
    next_year  = latest_date.year if latest_date.month < 12 else latest_date.year + 1
    target     = date(next_year - 1, next_month, 1)
    prior_val  = None
    for d, v in entry["values"]:
        if d == target:
            prior_val = v
            break
    if prior_val is None or prior_val == 0:
        return None
    return (projected - prior_val) / prior_val * 100


# ── Probability from projected vs threshold ───────────────────────────────────

def _prob_above(projected, threshold, uncertainty=0.15):
    """
    Probability that the actual print exceeds threshold,
    given projected value and uncertainty band.
    uncertainty = ±% of projected value (default ±0.15% for CPI-scale numbers)

    Uses a simple linear interpolation over a ±2σ band.
    """
    if projected is None or threshold is None:
        return None
    sigma = abs(projected) * uncertainty if projected != 0 else 0.3
    if sigma == 0:
        return 1.0 if projected > threshold else 0.0
    diff  = projected - threshold
    ratio = diff / sigma
    # Clip at ±2.5σ
    ratio = max(-2.5, min(2.5, ratio))
    # Linear approximation of normal CDF over ±2.5σ
    prob  = 0.5 + ratio * 0.18   # ~0.5 ± 0.45 over the ±2.5σ range
    return round(max(0.03, min(0.97, prob)), 4)


def _prob_below(projected, threshold, uncertainty=0.15):
    p = _prob_above(projected, threshold, uncertainty)
    return None if p is None else round(1.0 - p, 4)


# ── Background refresh ────────────────────────────────────────────────────────

def _refresh():
    _fetch_bls()
    _fetch_fred()
    _fetch_gdpnow()


def start():
    def loop():
        _refresh()
        while True:
            time.sleep(REFRESH_TTL)
            _refresh()
    threading.Thread(target=loop, daemon=True, name="econ-bg").start()
    print("  [Econ] Macro signal thread started — BLS / FRED / GDPNow (4h refresh)")


# ── Market matching ───────────────────────────────────────────────────────────

def _extract_threshold(title):
    """
    Pull the numeric threshold from a Kalshi market title.
    e.g. "CPI YoY above 3.2%?" → 3.2
         "unemployment below 4.5%?" → 4.5
         "GDP growth above 2%?" → 2.0
    Returns (threshold, direction) where direction is "above" or "below".
    """
    tl = title.lower()
    direction = "above" if any(w in tl for w in ["above", "exceed", "over", "higher", "more"]) else "below"
    # Match: 3.2%, 3.2, -0.1%, 0.3
    m = re.search(r'([-\d.]+)\s*%?', tl)
    if m:
        try:
            return float(m.group(1)), direction
        except ValueError:
            pass
    return None, direction


def _extract_month_year(ticker):
    """
    Extract target month/year from Kalshi ticker.
    e.g. KXCPIYOY-26APR → (4, 2026)
         KXECONSTATU3-26MAY → (5, 2026)
    """
    months = {
        "JAN":1, "FEB":2, "MAR":3, "APR":4, "MAY":5, "JUN":6,
        "JUL":7, "AUG":8, "SEP":9, "OCT":10, "NOV":11, "DEC":12,
    }
    m = re.search(r'(\d{2})([A-Z]{3})', ticker.upper())
    if m:
        year  = 2000 + int(m.group(1))
        month = months.get(m.group(2))
        if month:
            return month, year
    return None, None


def match_market(kalshi_market):
    """
    Match a Kalshi econ market and return a probability signal.

    Returns:
        {"prob": float, "signal": "YES"|"NO", "source": "EconSignals",
         "projected": float, "threshold": float, "series": str, "detail": str}
    or None if no match.
    """
    ticker = kalshi_market.get("ticker", "").upper()
    title  = kalshi_market.get("title", "")
    tl     = title.lower()

    yes_ask  = float(kalshi_market.get("yes_ask", 50))
    kalshi_p = yes_ask / 100.0

    prob     = None
    series   = None
    proj     = None
    detail   = ""

    # ── CPI YoY ─────────────────────────────────────────────────────────────
    is_cpi_yoy = (
        "KXCPIYOY" in ticker or "KXECONSTATCPIYOY" in ticker or
        any(w in tl for w in ["cpi year-over-year", "inflation in", "cpi yoy", "inflation rate yoy"])
    )
    if is_cpi_yoy:
        proj   = _projected_yoy("cpi_all")
        if proj is None:
            proj = _yoy_change("cpi_all")   # fallback to last known YoY
        series = "CPI YoY"
        thresh, direction = _extract_threshold(title)
        if proj is not None and thresh is not None:
            prob = _prob_above(proj, thresh) if direction == "above" else _prob_below(proj, thresh)
            detail = f"Projected CPI YoY={proj:.2f}% vs threshold={thresh}% (dir={direction})"

    # ── CPI MoM ──────────────────────────────────────────────────────────────
    elif "KXECONSTATCPI-" in ticker or "KXCPI-" in ticker or "cpi month-over-month" in tl:
        with _lock:
            entry = _data.get("cpi_all")
        if entry and len(entry["values"]) >= 2:
            last2  = entry["values"][-2:]
            actual_mom = (last2[1][1] - last2[0][1]) / last2[0][1] * 100
            proj   = actual_mom   # use last actual as projection
            series = "CPI MoM"
            thresh, direction = _extract_threshold(title)
            if thresh is not None:
                prob   = _prob_above(proj, thresh, uncertainty=0.3) if direction == "above" else _prob_below(proj, thresh, uncertainty=0.3)
                detail = f"Last CPI MoM={proj:.3f}% vs threshold={thresh}% (dir={direction})"

    # ── Core CPI ─────────────────────────────────────────────────────────────
    elif "KXCPICORE" in ticker or "KXECONSTATCPICORE" in ticker or "core cpi" in tl or "core inflation" in tl:
        proj   = _projected_yoy("cpi_core")
        if proj is None:
            proj = _yoy_change("cpi_core")
        series = "Core CPI YoY"
        thresh, direction = _extract_threshold(title)
        if proj is not None and thresh is not None:
            prob   = _prob_above(proj, thresh) if direction == "above" else _prob_below(proj, thresh)
            detail = f"Projected Core CPI YoY={proj:.2f}% vs threshold={thresh}%"

    # ── Unemployment ─────────────────────────────────────────────────────────
    elif "KXECONSTATU3" in ticker or "KXU3-" in ticker or "unemployment" in tl:
        latest = _latest("unrate", 1)
        if latest:
            proj   = latest[0][1]   # unemployment moves slowly; use current as projection
            series = "Unemployment"
            thresh, direction = _extract_threshold(title)
            if thresh is not None:
                # Unemployment: uncertainty ±0.2 pp
                prob   = _prob_above(proj, thresh, uncertainty=0.05) if direction == "above" else _prob_below(proj, thresh, uncertainty=0.05)
                detail = f"Current Unemployment={proj:.1f}% vs threshold={thresh}% (dir={direction})"

    # ── GDP ───────────────────────────────────────────────────────────────────
    elif "KXGDP" in ticker or "gdp growth" in tl or "gdp above" in tl or "gdp below" in tl:
        series = "GDP Growth"
        # Prefer GDPNow for live estimate
        with _lock:
            gdp_val = _gdpnow.get("value")
            gdp_age = time.time() - _gdpnow.get("ts", 0)
        if gdp_val is not None and gdp_age < 86400:
            proj = gdp_val
            detail_src = "GDPNow"
        else:
            # Fall back to last FRED quarterly GDP growth rate
            latest = _latest("gdp_growth", 1)
            proj = latest[0][1] if latest else None
            detail_src = "FRED"
        thresh, direction = _extract_threshold(title)
        if proj is not None and thresh is not None:
            prob   = _prob_above(proj, thresh, uncertainty=0.25) if direction == "above" else _prob_below(proj, thresh, uncertainty=0.25)
            detail = f"{detail_src} GDP={proj:.1f}% vs threshold={thresh}% (dir={direction})"

    # ── Core PCE ─────────────────────────────────────────────────────────────
    elif "KXPCECORE" in ticker or "pce" in tl:
        proj   = _projected_yoy("pce_core")
        if proj is None:
            proj = _yoy_change("pce_core")
        series = "Core PCE YoY"
        thresh, direction = _extract_threshold(title)
        if proj is not None and thresh is not None:
            prob   = _prob_above(proj, thresh) if direction == "above" else _prob_below(proj, thresh)
            detail = f"Projected Core PCE YoY={proj:.2f}% vs threshold={thresh}%"

    if prob is None or series is None:
        return None

    return {
        "prob":      prob,
        "source":    "EconSignals",
        "series":    series,
        "projected": round(proj, 4) if proj is not None else None,
        "detail":    detail,
    }


# ── Status summary ────────────────────────────────────────────────────────────

def status_summary():
    with _lock:
        series_loaded = {k: len(v["values"]) for k, v in _data.items()}
        gdp_val = _gdpnow.get("value")
        gdp_age = round((time.time() - _gdpnow.get("ts", 0)) / 60) if _gdpnow.get("ts") else None
    latest = {}
    for name in ["cpi_all", "unrate", "payrolls", "gdp_growth"]:
        vals = _latest(name, 1)
        if vals:
            latest[name] = {"date": str(vals[0][0]), "value": vals[0][1]}
    return {
        "series_loaded":  series_loaded,
        "gdpnow":         gdp_val,
        "gdpnow_age_min": gdp_age,
        "latest":         latest,
    }

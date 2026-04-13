"""
Cross-Market Edge Engine — v3

Strategy: buy Kalshi markets that external consensus prices higher.
Every gap is real edge — Kalshi reprices slower than the sources below.

Sources (in priority order):
  1. Polymarket     — prediction market consensus (free, 1000 markets/cycle)
  2. CME FedWatch   — FOMC meeting probabilities from futures pricing (free)
  3. NOAA/NWS       — official weather forecasts for 15 major US cities (free)

Sports/sportsbooks removed — those markets are already efficiently priced.
Real edge lives in weather, Fed/econ, crypto, politics, pop culture.

Architecture:
  - Background threads update each source independently.
  - Polymarket: every 90s. FedWatch: every 1h. NOAA: every 1h.
  - enrich_markets() reads from in-memory cache — zero blocking latency.
  - Multi-source confidence:
      2+ sources agree → full bonus
      1 source only    → 65% bonus
      Sources disagree → 0 bonus (skip — conflicted signal)

Bonus scale (per gap magnitude):
  7–10%  → +8  pts
  10–15% → +14 pts
  15–20% → +20 pts
  20%+   → +28 pts
  Max total bonus: 30 pts
"""

import time
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
import polymarket
import predictit
import fedwatch
import noaa
import ndfd
import econ_signals
import coingecko

# ── Shared state — written by bg thread, read by scan ────────────────────────
_lock       = threading.Lock()
_pm_markets = []    # Polymarket parsed market dicts
_pi_markets = []    # PredictIt parsed contract dicts
_pm_ts      = 0.0
_pi_ts      = 0.0

PM_REFRESH_INTERVAL  = 90      # Polymarket: 90s (free, no quota)
PI_REFRESH_INTERVAL  = 300     # PredictIt: 5min (prices move)
FED_REFRESH_INTERVAL = 3600    # FedWatch: 1h (FOMC probs don't move fast)
NOAA_REFRESH_INTERVAL = 3600   # NOAA: 1h (NWS updates hourly)

GAP_THRESHOLD = 0.07   # 7% minimum gap to count as edge signal

STOP_WORDS = {
    "the","a","an","is","are","will","to","of","in","on","at","for","be","by",
    "or","and","that","this","as","it","its","not","from","with","have","has",
    "had","do","does","did","who","what","when","where","which","how","than",
    "vs","versus","against","win","wins","beat","beats","going",
}


# ── Keyword matching ──────────────────────────────────────────────────────────

def _word_set(text):
    tokens = (
        text.lower()
            .replace("?", "").replace(".", "").replace(",", "")
            .replace("'s", "").replace("-", " ")
            .split()
    )
    return set(tokens) - STOP_WORDS


def _similarity(a, b):
    """Jaccard word overlap (0–1)."""
    wa, wb = _word_set(a), _word_set(b)
    if not wa or not wb:
        return 0.0
    return len(wa & wb) / max(len(wa), len(wb))


def _best_match(kalshi_title, candidates, key_fn, threshold=0.25):
    """Return (best_candidate, similarity_score) or (None, 0)."""
    best, best_sim = None, 0.0
    for c in candidates:
        sim = _similarity(kalshi_title, key_fn(c))
        if sim > best_sim:
            best_sim = sim
            best = c
    return (best, best_sim) if best_sim >= threshold else (None, 0.0)


# ── Bonus calculation ─────────────────────────────────────────────────────────

def _gap_bonus(gap_abs):
    """Convert a probability gap (0–1) into a score bonus."""
    if gap_abs < GAP_THRESHOLD:
        return 0
    if gap_abs < 0.10:
        return 8
    if gap_abs < 0.15:
        return 14
    if gap_abs < 0.20:
        return 20
    return 28


# ── Background prefetch ───────────────────────────────────────────────────────

def _fetch_polymarket():
    global _pm_markets, _pm_ts
    raw    = polymarket.get_active_markets(limit=1000)
    parsed = [m for m in (polymarket.parse_market(r) for r in raw) if m]
    with _lock:
        _pm_markets = parsed
        _pm_ts      = time.time()
    print(f"  [Cross/bg] Polymarket: {len(parsed)} markets")


def _fetch_predictit():
    global _pi_markets, _pi_ts
    parsed = predictit.get_markets()
    with _lock:
        _pi_markets = parsed
        _pi_ts      = time.time()


def _pm_loop():
    while True:
        try:
            _fetch_polymarket()
        except Exception as e:
            print(f"  [Cross/pm] error: {e}")
        time.sleep(PM_REFRESH_INTERVAL)


def _pi_loop():
    while True:
        try:
            _fetch_predictit()
        except Exception as e:
            print(f"  [Cross/pi] error: {e}")
        time.sleep(PI_REFRESH_INTERVAL)


def start_background_fetcher():
    """Start all background data threads. Call once at startup."""
    threading.Thread(target=_pm_loop, daemon=True, name="cross-pm-bg").start()
    threading.Thread(target=_pi_loop, daemon=True, name="cross-pi-bg").start()
    fedwatch.get_meetings()    # warm FedWatch cache immediately
    noaa.start()               # starts NOAA background thread (12h periods)
    ndfd.start()               # starts NDFD background thread (hourly)
    econ_signals.start()       # starts BLS/FRED/GDPNow background thread (4h)
    coingecko.start()          # starts CoinGecko price feed (3 min refresh)
    threading.Thread(target=_fetch_polymarket, daemon=True).start()
    threading.Thread(target=_fetch_predictit,  daemon=True).start()
    print("  [Cross] Background prefetch started — Polymarket(90s) / PredictIt(5m) / FedWatch(1h) / NOAA(1h) / NDFD-hourly(1h) / EconSignals(4h) / CoinGecko(3m)")


# ── Core edge computation ─────────────────────────────────────────────────────

def compute_cross_edge(kalshi_market, pm_markets, pi_markets):
    """
    Compare Kalshi price against all external sources.
    Returns edge dict with bonus score and directional signal.
    """
    title    = kalshi_market.get("title") or kalshi_market.get("ticker", "")
    yes_ask  = float(kalshi_market.get("yes_ask", 50))
    kalshi_p = yes_ask / 100.0

    result = {
        "bonus":   0,
        "signal":  None,
        "sources": [],
        "gaps":    [],
        # Polymarket
        "pm_match": None,
        "pm_prob":  None,
        "pm_sim":   0.0,
        # PredictIt
        "pi_match": None,
        "pi_prob":  None,
        "pi_sim":   0.0,
        # FedWatch
        "fed_prob":    None,
        "fed_meeting": None,
        # NOAA/NWS
        "noaa_prob":   None,
        "noaa_city":   None,
        "noaa_detail": None,
        # NDFD hourly
        "ndfd_prob":   None,
        "ndfd_city":   None,
        "ndfd_detail": None,
        "ndfd_fields": None,
        # Econ signals
        "econ_prob":   None,
        "econ_series": None,
        "econ_detail": None,
        # CoinGecko
        "cg_prob":     None,
        "cg_coin":     None,
        "cg_price":    None,
        "cg_detail":   None,
    }

    external_probs = []

    # ── 1. Polymarket ────────────────────────────────────────────────────────
    pm, pm_sim = _best_match(title, pm_markets, lambda m: m["question"])
    if pm and pm.get("yes_prob") is not None:
        pm_p = pm["yes_prob"]
        gap  = pm_p - kalshi_p
        result.update(pm_match=pm["question"], pm_prob=round(pm_p, 4), pm_sim=round(pm_sim, 3))
        result["sources"].append("Polymarket")
        result["gaps"].append(("Polymarket", gap))
        external_probs.append(pm_p)

    # ── 2. PredictIt ─────────────────────────────────────────────────────────
    pi, pi_sim = _best_match(title, pi_markets, lambda m: m["question"])
    if pi and pi.get("yes_prob") is not None:
        pi_p = pi["yes_prob"]
        gap  = pi_p - kalshi_p
        result.update(pi_match=pi["question"], pi_prob=round(pi_p, 4), pi_sim=round(pi_sim, 3))
        result["sources"].append("PredictIt")
        result["gaps"].append(("PredictIt", gap))
        external_probs.append(pi_p)

    # ── 3. CME FedWatch ──────────────────────────────────────────────────────
    fed = fedwatch.match_market(title)
    if fed and fed.get("prob") is not None:
        fed_p = fed["prob"]
        gap   = fed_p - kalshi_p
        result.update(fed_prob=round(fed_p, 4), fed_meeting=fed.get("meeting"))
        result["sources"].append("CME FedWatch")
        result["gaps"].append(("CME FedWatch", gap))
        external_probs.append(fed_p)

    # ── 4. NOAA/NWS (12-hour periods) ────────────────────────────────────────
    wx = noaa.match_market(title)
    if wx and wx.get("prob") is not None:
        wx_p = wx["prob"]
        gap  = wx_p - kalshi_p
        result.update(
            noaa_prob=round(wx_p, 4),
            noaa_city=wx.get("city"),
            noaa_detail=wx.get("detail"),
        )
        result["sources"].append("NOAA/NWS")
        result["gaps"].append(("NOAA/NWS", gap))
        external_probs.append(wx_p)

    # ── 5. NDFD hourly — higher precision than NWS periods ───────────────────
    # Only used for weather markets. If both NOAA and NDFD agree, confidence ↑.
    # If they disagree by >10%, prefer NDFD (more granular).
    nd = ndfd.match_market(title)
    if nd and nd.get("prob") is not None:
        nd_p = nd["prob"]
        gap  = nd_p - kalshi_p
        result.update(
            ndfd_prob=round(nd_p, 4),
            ndfd_city=nd.get("city"),
            ndfd_detail=nd.get("detail"),
            ndfd_fields=nd.get("fields"),
        )
        # If NOAA is already a source and they broadly agree (within 15%), merge
        # rather than double-count — take NDFD as the authoritative value.
        if "NOAA/NWS" in result["sources"] and result.get("noaa_prob") is not None:
            noaa_p = result["noaa_prob"]
            if abs(nd_p - noaa_p) <= 0.15:
                # Replace NOAA entry with NDFD (better precision, same signal)
                idx = result["sources"].index("NOAA/NWS")
                result["sources"][idx] = "NDFD"
                for i, (src, g) in enumerate(result["gaps"]):
                    if src == "NOAA/NWS":
                        result["gaps"][i] = ("NDFD", gap)
                external_probs[external_probs.index(noaa_p)] = nd_p
                result["noaa_prob"] = None   # superseded
            else:
                # They disagree — add as independent source, let consensus logic handle it
                result["sources"].append("NDFD")
                result["gaps"].append(("NDFD", gap))
                external_probs.append(nd_p)
        else:
            result["sources"].append("NDFD")
            result["gaps"].append(("NDFD", gap))
            external_probs.append(nd_p)

    # ── 6. Econ Signals (BLS/FRED/GDPNow) ───────────────────────────────────
    ec = econ_signals.match_market(kalshi_market)
    if ec and ec.get("prob") is not None:
        ec_p = ec["prob"]
        gap  = ec_p - kalshi_p
        result.update(
            econ_prob=round(ec_p, 4),
            econ_series=ec.get("series"),
            econ_detail=ec.get("detail"),
        )
        result["sources"].append("EconSignals")
        result["gaps"].append(("EconSignals", gap))
        external_probs.append(ec_p)

    # ── 7. CoinGecko — real-time crypto prices ───────────────────────────────
    cg = coingecko.match_market(kalshi_market)
    if cg and cg.get("prob") is not None:
        cg_p = cg["prob"]
        gap  = cg_p - kalshi_p
        result.update(
            cg_prob=round(cg_p, 4),
            cg_coin=cg.get("coin"),
            cg_price=cg.get("price"),
            cg_detail=cg.get("detail"),
        )
        result["sources"].append("CoinGecko")
        result["gaps"].append(("CoinGecko", gap))
        external_probs.append(cg_p)

    if not external_probs:
        return result

    # ── Direction consensus ──────────────────────────────────────────────────
    gaps      = [g for _, g in result["gaps"]]
    all_yes   = all(g > 0 for g in gaps)
    all_no    = all(g < 0 for g in gaps)
    consistent = all_yes or all_no

    if not consistent:
        # Sources disagree — conflicted signal, skip
        return result

    avg_external = sum(external_probs) / len(external_probs)
    avg_gap      = avg_external - kalshi_p
    n_sources    = len(external_probs)

    raw_bonus = _gap_bonus(abs(avg_gap))
    if raw_bonus == 0:
        return result

    # Multi-source confidence multiplier
    confidence  = 1.0 if n_sources >= 2 else 0.65
    final_bonus = min(30, int(raw_bonus * confidence))

    result["bonus"]  = final_bonus
    result["signal"] = "YES" if avg_gap > 0 else "NO"
    return result


# ── Public API ────────────────────────────────────────────────────────────────

def enrich_markets(markets):
    """
    Called by trader.py on every scan cycle.
    Reads from in-memory caches — no blocking API calls.
    Adds cross_edge dict to each market and boosts score when edge found.
    """
    with _lock:
        pm = list(_pm_markets)
        pi = list(_pi_markets)

    enriched = 0
    for m in markets:
        ce = compute_cross_edge(m, pm, pi)
        m["cross_edge"] = ce
        if ce["bonus"] > 0:
            base = float(m.get("score", 0))
            m["score"]        = base + ce["bonus"]
            m["score_boosted"] = True
            enriched += 1

    if enriched:
        print(f"  [Cross] {enriched}/{len(markets)} markets edge-boosted")
    return markets

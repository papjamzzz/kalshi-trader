"""
Cross-Market Edge Engine — v2

Strategy: buy Kalshi markets that external consensus prices higher.
Every penny of gap is real edge. We don't need a huge gap — we need
consistent, confirmed gaps across 1+ sources. Even a 3% discrepancy
is exploitable if Kalshi is slow to reprice and we hold through correction.

Architecture:
  - Background prefetch thread updates data every 90s independently.
    When the scan cycle hits, data is already warm — zero blocking latency.
  - Polymarket + OddsAPI fetched in parallel via ThreadPoolExecutor.
  - 1000 Polymarket markets loaded per cycle for maximum coverage.
  - Gap threshold: 3% (was 5%). Small edges replicate. We want volume.
  - Multi-source confidence:
      2+ sources agree on direction → full bonus
      1 source only              → 65% bonus
      Sources disagree           → 0 bonus (skip — conflicted)

Bonus scale (per source gap):
  3–5%  → +8  pts
  5–8%  → +14 pts
  8–12% → +20 pts
  12%+  → +28 pts
  Max total bonus: 30 pts

Score of 50 base + 12 cross bonus = 62 → above threshold → buy.
"""

import time
import threading
from concurrent.futures import ThreadPoolExecutor, wait, ALL_COMPLETED
import polymarket
import odds_api

# ── Shared state — written by bg thread, read by scan ────────────────────────
_lock = threading.Lock()
_pm_markets   = []   # list of parsed polymarket dicts
_odds_events  = []   # list of parsed sportsbook event dicts
_pm_ts        = 0.0
_odds_ts      = 0.0

REFRESH_INTERVAL = 90   # seconds between bg refreshes
GAP_THRESHOLD    = 0.03  # minimum external-vs-kalshi prob gap to count as edge

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


def _best_match(kalshi_title, candidates, key_fn, threshold=0.28):
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
    """Convert a probability gap (0–1) into a score bonus (0–28)."""
    if gap_abs < GAP_THRESHOLD:
        return 0
    if gap_abs < 0.05:
        return 8
    if gap_abs < 0.08:
        return 14
    if gap_abs < 0.12:
        return 20
    return 28


# ── Background prefetch ───────────────────────────────────────────────────────

def _fetch_polymarket():
    global _pm_markets, _pm_ts
    raw = polymarket.get_active_markets(limit=1000)
    parsed = [m for m in (polymarket.parse_market(r) for r in raw) if m]
    with _lock:
        _pm_markets = parsed
        _pm_ts = time.time()
    print(f"  [Cross/bg] Polymarket: {len(parsed)} markets")


def _fetch_odds():
    global _odds_events, _odds_ts
    events = odds_api.get_all_odds()
    with _lock:
        _odds_events = events
        _odds_ts = time.time()


def _bg_loop():
    """Background thread: fetch both sources in parallel every REFRESH_INTERVAL."""
    while True:
        try:
            with ThreadPoolExecutor(max_workers=2) as pool:
                f1 = pool.submit(_fetch_polymarket)
                f2 = pool.submit(_fetch_odds)
                wait([f1, f2], timeout=20, return_when=ALL_COMPLETED)
        except Exception as e:
            print(f"  [Cross/bg] error: {e}")
        time.sleep(REFRESH_INTERVAL)


def start_background_fetcher():
    """Call once at startup. Kicks off the prefetch thread."""
    t = threading.Thread(target=_bg_loop, daemon=True, name="cross-market-bg")
    t.start()
    # Warm the cache immediately — don't wait 90s for first data
    threading.Thread(target=_fetch_polymarket, daemon=True).start()
    threading.Thread(target=_fetch_odds,       daemon=True).start()
    print("  [Cross] Background prefetch thread started")


# ── Core edge computation ─────────────────────────────────────────────────────

def compute_cross_edge(kalshi_market, pm_markets, odds_events):
    """
    Returns edge dict with bonus score and directional signal.

    kalshi_prob = yes_ask / 100
    external sources price the same event differently → gap = edge
    """
    title    = kalshi_market.get("title") or kalshi_market.get("ticker", "")
    yes_ask  = float(kalshi_market.get("yes_ask", 50))
    kalshi_p = yes_ask / 100.0

    result = {
        "bonus":    0,
        "signal":   None,    # "YES" | "NO" | None
        "sources":  [],
        "gaps":     [],      # [(source_name, gap_float)]
        "pm_match": None,
        "pm_prob":  None,
        "pm_sim":   0.0,
        "bk_match": None,
        "bk_prob":  None,
        "bk_books": 0,
        "bk_sim":   0.0,
    }

    external_probs = []

    # ── Polymarket ──────────────────────────────────────────────────────────
    pm, pm_sim = _best_match(title, pm_markets, lambda m: m["question"])
    if pm and pm.get("yes_prob") is not None:
        pm_p = pm["yes_prob"]
        gap  = pm_p - kalshi_p
        result.update(pm_match=pm["question"], pm_prob=round(pm_p, 4), pm_sim=round(pm_sim, 3))
        result["sources"].append("polymarket")
        result["gaps"].append(("polymarket", gap))
        external_probs.append(pm_p)

    # ── Sportsbooks ─────────────────────────────────────────────────────────
    ev, ev_sim = _best_match(title, odds_events, lambda e: e["title"])
    if ev:
        home_words  = _word_set(ev.get("home", ""))
        title_words = _word_set(title)
        home_match  = len(home_words & title_words)
        ext_p = ev["home_prob"] if home_match > 0 else ev["away_prob"]
        gap   = ext_p - kalshi_p
        result.update(bk_match=ev["title"], bk_prob=round(ext_p, 4),
                      bk_books=ev["bookmakers"], bk_sim=round(ev_sim, 3))
        result["sources"].append(f"books({ev['bookmakers']})")
        result["gaps"].append(("sportsbooks", gap))
        external_probs.append(ext_p)

    if not external_probs:
        return result

    # ── Direction consensus ──────────────────────────────────────────────────
    gaps = [g for _, g in result["gaps"]]
    all_yes = all(g > 0 for g in gaps)
    all_no  = all(g < 0 for g in gaps)
    consistent = all_yes or all_no

    if not consistent:
        # Sources disagree — no trade, too risky
        return result

    avg_external = sum(external_probs) / len(external_probs)
    avg_gap      = avg_external - kalshi_p
    n_sources    = len(external_probs)

    raw_bonus = _gap_bonus(abs(avg_gap))
    if raw_bonus == 0:
        return result

    # Multi-source confidence multiplier
    confidence = 1.0 if n_sources >= 2 else 0.65
    final_bonus = min(30, int(raw_bonus * confidence))

    result["bonus"]  = final_bonus
    result["signal"] = "YES" if avg_gap > 0 else "NO"
    return result


# ── Public API ────────────────────────────────────────────────────────────────

def enrich_markets(markets):
    """
    Called by trader.py on every scan cycle.
    Reads from in-memory cache — no blocking API calls here.
    Adds cross_edge dict to each market and boosts score when edge found.
    """
    with _lock:
        pm  = list(_pm_markets)
        odds = list(_odds_events)

    if not pm and not odds:
        return markets

    enriched = 0
    for m in markets:
        ce = compute_cross_edge(m, pm, odds)
        m["cross_edge"] = ce
        if ce["bonus"] > 0:
            base = float(m.get("score", 0))
            m["score"] = base + ce["bonus"]
            m["score_boosted"] = True
            enriched += 1

    if enriched:
        print(f"  [Cross] {enriched}/{len(markets)} markets boosted")
    return markets

"""
Cross-Market Edge Engine

Compares Kalshi implied odds against external markets:
  1. Polymarket  — free, always on, prediction markets
  2. The Odds API — DraftKings, FanDuel, BetMGM, Caesars, etc. (ODDS_API_KEY)

When Kalshi's price diverges from external consensus → real, exploitable edge.

Adds a bonus (0–30 pts) to the KK base score.
Score of 58 base + 15 cross-market bonus = 73 → above threshold → buy.

Caches external data for 5 minutes to respect rate limits.
"""

import time
import polymarket
import odds_api

# ── Cache ────────────────────────────────────────────────────────────────────
_CACHE = {
    "polymarket": {"data": [], "ts": 0},
    "odds":       {"data": [], "ts": 0},
}
CACHE_TTL = 300  # 5 minutes

STOP_WORDS = {
    "the","a","an","is","are","will","to","of","in","on","at","for","be","by",
    "or","and","that","this","as","it","its","not","from","with","have","has",
    "had","do","does","did","who","what","when","where","which","how","than",
    "vs","versus","against","win","wins","beat","beats",
}


# ── Helpers ──────────────────────────────────────────────────────────────────

def _word_set(text):
    words = set(text.lower().replace("?","").replace(".","").replace(",","").split())
    return words - STOP_WORDS


def _similarity(a, b):
    """Jaccard-like word overlap between two strings (0–1)."""
    wa = _word_set(a)
    wb = _word_set(b)
    if not wa or not wb:
        return 0.0
    return len(wa & wb) / max(len(wa), len(wb))


def _get_polymarket():
    now = time.time()
    c = _CACHE["polymarket"]
    if now - c["ts"] < CACHE_TTL and c["data"]:
        return c["data"]
    raw = polymarket.get_active_markets(limit=500)
    parsed = [m for m in (polymarket.parse_market(r) for r in raw) if m]
    c["data"] = parsed
    c["ts"] = now
    print(f"  [Cross] Polymarket: {len(parsed)} markets loaded")
    return parsed


def _get_odds():
    now = time.time()
    c = _CACHE["odds"]
    if now - c["ts"] < CACHE_TTL and c["data"]:
        return c["data"]
    events = odds_api.get_all_odds()
    c["data"] = events
    c["ts"] = now
    return events


# ── Core Match + Score ────────────────────────────────────────────────────────

def _match_polymarket(kalshi_title, pm_markets, threshold=0.30):
    best, best_sim = None, 0.0
    for pm in pm_markets:
        sim = _similarity(kalshi_title, pm["question"])
        if sim > best_sim:
            best_sim = sim
            best = pm
    if best_sim >= threshold:
        return best, best_sim
    return None, 0.0


def _match_odds(kalshi_title, events, threshold=0.28):
    best, best_sim = None, 0.0
    for ev in events:
        sim = _similarity(kalshi_title, ev["title"])
        if sim > best_sim:
            best_sim = sim
            best = ev
    if best_sim >= threshold:
        return best, best_sim
    return None, 0.0


def _bonus_from_gap(gap_pct):
    """
    Convert probability gap (0–1) into a score bonus (0–30).
    5%  gap → +10 pts
    10% gap → +20 pts
    15%+ gap → +30 pts
    """
    if gap_pct < 0.05:
        return 0
    return min(30, int(gap_pct * 200))


def compute_cross_edge(kalshi_market, pm_markets, odds_events):
    """
    Main function. Returns a dict describing the cross-market signal.

    kalshi_yes_prob = yes_ask / 100  (what Kalshi thinks YES is worth)
    external_yes_prob = what Polymarket / sportsbooks think

    If external > kalshi → YES is underpriced on Kalshi → BUY YES
    If external < kalshi → NO is underpriced on Kalshi → BUY NO
    """
    title    = kalshi_market.get("title") or kalshi_market.get("ticker", "")
    yes_ask  = float(kalshi_market.get("yes_ask", 50))
    kalshi_p = yes_ask / 100.0

    result = {
        "bonus":           0,
        "signal":          None,      # "YES" | "NO" | None
        "sources":         [],
        "polymarket_q":    None,
        "polymarket_prob": None,
        "polymarket_sim":  0.0,
        "odds_title":      None,
        "odds_prob":       None,      # home team prob (matched to YES side)
        "odds_books":      0,
        "odds_sim":        0.0,
        "prob_gaps":       [],        # list of (source, gap) tuples
    }

    external_probs = []

    # ── Polymarket ──────────────────────────────────────────────────────────
    pm, pm_sim = _match_polymarket(title, pm_markets)
    if pm and pm.get("yes_prob") is not None:
        pm_p = pm["yes_prob"]
        gap  = pm_p - kalshi_p
        result["polymarket_q"]    = pm["question"]
        result["polymarket_prob"] = round(pm_p, 4)
        result["polymarket_sim"]  = round(pm_sim, 3)
        result["sources"].append("polymarket")
        result["prob_gaps"].append(("polymarket", gap))
        external_probs.append(pm_p)

    # ── Sportsbooks (The Odds API) ──────────────────────────────────────────
    ev, ev_sim = _match_odds(title, odds_events)
    if ev:
        # Heuristic: if Kalshi YES price < 50, we're betting on the underdog.
        # Map home_prob to YES side (best effort — keyword overlap tells us which team is YES)
        home_words = _word_set(ev.get("home", ""))
        title_words = _word_set(title)
        home_in_title = len(home_words & title_words)
        # If more home-team words appear in Kalshi title, map home_prob → YES
        ext_prob = ev["home_prob"] if home_in_title > 0 else ev["away_prob"]
        gap = ext_prob - kalshi_p
        result["odds_title"] = ev["title"]
        result["odds_prob"]  = round(ext_prob, 4)
        result["odds_books"] = ev["bookmakers"]
        result["odds_sim"]   = round(ev_sim, 3)
        result["sources"].append(f"sportsbooks({ev['bookmakers']})")
        result["prob_gaps"].append(("sportsbooks", gap))
        external_probs.append(ext_prob)

    # ── Compute final bonus ─────────────────────────────────────────────────
    if external_probs:
        avg_external = sum(external_probs) / len(external_probs)
        avg_gap      = avg_external - kalshi_p
        bonus        = _bonus_from_gap(abs(avg_gap))

        # Only apply bonus when gap is directionally consistent across sources
        gaps = [g for _, g in result["prob_gaps"]]
        consistent = all(g > 0 for g in gaps) or all(g < 0 for g in gaps)
        if not consistent:
            bonus = bonus // 2   # halve bonus when sources disagree with each other

        result["bonus"]  = bonus
        result["signal"] = "YES" if avg_gap > 0 else "NO"

    return result


# ── Public API ────────────────────────────────────────────────────────────────

def enrich_markets(markets):
    """
    Called once per scan cycle. Adds cross_edge dict to each market
    and boosts the score where external data confirms an edge.
    """
    pm_markets  = _get_polymarket()
    odds_events = _get_odds()

    if not pm_markets and not odds_events:
        return markets   # no external data, pass through unchanged

    enriched = 0
    for m in markets:
        ce = compute_cross_edge(m, pm_markets, odds_events)
        m["cross_edge"] = ce

        if ce["bonus"] > 0:
            base  = float(m.get("score", 0))
            boost = ce["bonus"]
            m["score"] = base + boost
            m["score_boosted"] = True
            enriched += 1

    if enriched:
        print(f"  [Cross] {enriched}/{len(markets)} markets boosted by cross-market edge")
    return markets

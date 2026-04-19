"""
Closing Line Value (CLV) Tracker

CLV is the single most reliable measure of whether a trading strategy has
real edge. If you consistently enter at better prices than where the market
closes, you're the sharp money. If worse, you're the fish.

How it works:
  1. record_entry() called the moment a position opens — stores entry data
  2. Background thread watches pending entries as they approach close time
  3. ~5 minutes before market close: fetch last active price, compute CLV
  4. Results written to data/clv.json for analysis

CLV calculation:
  YES position:  CLV = closing_mid - entry_price
                 Positive = market moved in our direction before close = real edge
  NO  position:  CLV = (100 - closing_mid) - entry_price
                 Positive = NO price moved in our direction before close

signal_type tags let you compare edge sources:
  "cross_market"    — Polymarket/FedWatch/CoinGecko/NOAA/etc signal
"""

import os
import json
import time
import threading
from datetime import datetime, timezone

import kalshi_api as kapi

CLV_FILE = "data/clv.json"

_lock    = threading.Lock()
_pending = {}   # ticker → entry dict


# ── Public API ────────────────────────────────────────────────────────────────

def record_entry(ticker, side, entry_price, close_time,
                 title="", signal_type="cross_market"):
    """
    Call immediately when a position opens.
    close_time: ISO string from Kalshi market data.
    signal_type: tag for later analysis (e.g. 'cross_market').
    """
    with _lock:
        _pending[ticker] = {
            "ticker":      ticker,
            "side":        side,
            "entry_price": entry_price,
            "close_time":  close_time or "",
            "entry_time":  datetime.now().isoformat(),
            "title":       title,
            "signal_type": signal_type,
        }


def get_summary():
    """
    Return CLV stats for the dashboard and strategy review.
    CLV rate > 50% + positive avg_clv_cents = strategy has real edge.
    """
    records = _load_records()
    if not records:
        return {
            "total": 0, "clv_rate_pct": 0,
            "avg_clv_cents": 0, "pending": len(_pending),
        }

    total = len(records)
    pos   = sum(1 for r in records if r.get("clv_cents", 0) > 0)
    clvs  = [r["clv_cents"] for r in records if "clv_cents" in r]
    avg   = sum(clvs) / len(clvs) if clvs else 0

    # Break down by signal type — the key insight table
    by_sig = {}
    for r in records:
        sig = r.get("signal_type", "unknown")
        if sig not in by_sig:
            by_sig[sig] = {"n": 0, "wins": 0, "clvs": []}
        by_sig[sig]["n"] += 1
        if r.get("clv_cents", 0) > 0:
            by_sig[sig]["wins"] += 1
        if "clv_cents" in r:
            by_sig[sig]["clvs"].append(r["clv_cents"])

    breakdown = {}
    for sig, d in by_sig.items():
        breakdown[sig] = {
            "n":       d["n"],
            "clv_pct": round(d["wins"] / d["n"] * 100, 1) if d["n"] else 0,
            "avg_clv": round(sum(d["clvs"]) / len(d["clvs"]), 2) if d["clvs"] else 0,
        }

    return {
        "total":         total,
        "clv_rate_pct":  round(pos / total * 100, 1),
        "avg_clv_cents": round(avg, 2),
        "pending":       len(_pending),
        "by_signal":     breakdown,
    }


def start():
    """Start background CLV resolver thread. Call once at engine startup."""
    threading.Thread(target=_bg_loop, daemon=True, name="clv-bg").start()
    print("  📊 CLV tracker started")


# ── Internal ──────────────────────────────────────────────────────────────────

def _load_records():
    try:
        os.makedirs("data", exist_ok=True)
        if os.path.exists(CLV_FILE):
            with open(CLV_FILE) as f:
                return json.load(f)
    except Exception:
        pass
    return []


def _append_record(record):
    records = _load_records()
    records.append(record)
    try:
        os.makedirs("data", exist_ok=True)
        with open(CLV_FILE, "w") as f:
            json.dump(records, f, indent=2)
    except Exception as e:
        print(f"  [CLV] save error: {e}")


def _compute_and_record(entry, yes_bid, yes_ask):
    side = entry["side"]
    ep   = float(entry["entry_price"])

    closing_mid = (yes_bid + yes_ask) / 2.0

    if side == "yes":
        # Bought YES at ep. Positive CLV = market closed higher = we were right early.
        clv = closing_mid - ep
    else:
        # Bought NO at ep (= 100 - yes_bid at entry).
        # NO closing mid = 100 - yes_mid.
        closing_no_mid = 100.0 - closing_mid
        clv = closing_no_mid - ep

    clv_pct = round((clv / ep) * 100, 2) if ep > 0 else 0

    record = {
        **entry,
        "closing_yes_bid": yes_bid,
        "closing_yes_ask": yes_ask,
        "closing_mid":     round(closing_mid, 1),
        "clv_cents":       round(clv, 2),
        "clv_pct":         clv_pct,
        "positive":        clv > 0,
        "recorded_at":     datetime.now().isoformat(),
    }
    _append_record(record)

    sign = "✅" if clv > 0 else "❌"
    print(
        f"  {sign} CLV [{entry.get('signal_type','?')}] {entry['ticker']} | "
        f"{side.upper()} entry={ep:.0f}¢  close_mid={closing_mid:.1f}¢  "
        f"CLV={clv:+.1f}¢ ({clv_pct:+.1f}%)"
    )
    return record


def _check_pending():
    """Scan pending entries. Record CLV when market is within 5 min of close."""
    now_dt = datetime.now(timezone.utc)

    with _lock:
        snap = dict(_pending)

    resolved = []
    for ticker, entry in snap.items():
        close_str = entry.get("close_time", "")
        if not close_str:
            resolved.append(ticker)
            continue

        try:
            exp       = datetime.fromisoformat(close_str.replace("Z", "+00:00"))
            secs_left = (exp - now_dt).total_seconds()
        except Exception:
            resolved.append(ticker)
            continue

        # Capture closing line in the ±5 min window around close time
        if -300 <= secs_left <= 300:
            try:
                m = kapi.get_market(ticker)
                yb = int(m.get("yes_bid", 0)) if m else 0
                ya = int(m.get("yes_ask", 0)) if m else 0
                if yb and ya:
                    _compute_and_record(entry, yb, ya)
                    resolved.append(ticker)
            except Exception as e:
                print(f"  [CLV] price fetch failed {ticker}: {e}")

        # Market closed > 3h ago with no price — give up, log as unresolved
        elif secs_left < -10800:
            print(f"  [CLV] ⚠ expired unresolved: {ticker}")
            resolved.append(ticker)

    if resolved:
        with _lock:
            for t in resolved:
                _pending.pop(t, None)


def _bg_loop():
    while True:
        try:
            _check_pending()
        except Exception as e:
            print(f"  [CLV] loop error: {e}")
        time.sleep(60)   # check every minute — close window is ±5 min so this is fine

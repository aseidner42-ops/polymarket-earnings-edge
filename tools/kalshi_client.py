"""Kalshi reader — read-only market data, no auth needed.

HONEST SCOPE (as of 2026-08): Kalshi's earnings markets are "what will the company SAY on the
call?" keyword/MENTION markets (series KXEARNINGSMENTION...), NOT EPS beat/miss markets. Our
engine predicts beat/miss, so it can't price mention markets — those are explicitly filtered
OUT. This reader returns only genuine BEAT/MISS / revenue markets. Today that's an empty list;
the moment Kalshi lists such a market it flows into the scanner automatically, same edge logic
as Polymarket. No fake reach — it finds what actually exists.
"""
from __future__ import annotations

import json
import urllib.request
from typing import Any, Dict, List, Optional

BASE = "https://api.elections.kalshi.com/trade-api/v2"
_BEAT_HINTS = ("beat earnings", "beat eps", "eps beat", "exceed eps", "earnings per share",
               "beat revenue", "revenue beat", "miss earnings", "beat estimates")
_MENTION_HINTS = ("say during", "mention", "say on", "earnings call", "what will")


def _get(path: str, params: Dict[str, Any]) -> Optional[dict]:
    q = "&".join(f"{k}={v}" for k, v in params.items() if v not in (None, ""))
    url = f"{BASE}{path}" + (f"?{q}" if q else "")
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
        return json.loads(urllib.request.urlopen(req, timeout=20).read())
    except Exception:
        return None


def _ticker_from(title: str) -> str:
    import re
    m = re.search(r"\(([A-Z]{1,5})\)", title or "")
    return m.group(1) if m else ""


def find_beat_markets(max_pages: int = 4) -> List[Dict[str, Any]]:
    """Return open Kalshi markets that are genuine EPS/revenue BEAT-MISS bets (mention markets
    filtered out). Each: {ticker, question, yes_price, url, source='kalshi'}. Usually [] today."""
    out: List[Dict[str, Any]] = []
    cur = None
    for _ in range(max_pages):
        d = _get("/markets", {"limit": 200, "status": "open", "cursor": cur})
        if not d:
            break
        for m in d.get("markets", []) or []:
            t = f"{m.get('title','')} {m.get('subtitle','') or ''}".lower()
            if any(h in t for h in _MENTION_HINTS):
                continue  # skip 'what will they say' mention markets — not beat/miss
            if not any(h in t for h in _BEAT_HINTS):
                continue
            bid, ask = m.get("yes_bid"), m.get("yes_ask")
            yes = ((bid + ask) / 2.0 / 100.0) if (bid is not None and ask is not None) else None
            out.append({"ticker": _ticker_from(m.get("title", "")),
                        "question": m.get("title"), "yes_price": yes,
                        "url": f"https://kalshi.com/markets/{m.get('ticker','')}",
                        "slug": m.get("ticker"), "source": "kalshi"})
        cur = d.get("cursor")
        if not cur:
            break
    return out

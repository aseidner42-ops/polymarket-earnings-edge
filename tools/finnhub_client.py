"""Finnhub data access (free tier).

Provides: symbol search (name -> ticker), upcoming earnings date with consensus
EPS + revenue estimates, historical earnings surprises, and recommendation
trends (used as a free proxy for analyst revisions).

Free tier limits: ~60 req/min. Some endpoints (eps-estimate, price-target,
revenue-estimate detail) are premium and return 403; callers must degrade
gracefully when a field is None.
"""
from __future__ import annotations

import time
from datetime import date, timedelta
from typing import Any, Dict, List, Optional

import requests

from tools.config import get_finnhub_key

BASE = "https://finnhub.io/api/v1"
_session = requests.Session()


def _get(path: str, params: Dict[str, Any], retries: int = 6) -> Any:
    """GET a Finnhub endpoint with token + retry. Returns parsed JSON.

    Returns None on 403 (premium-gated) so callers can degrade instead of crash.
    On the free tier (60 req/min) bursts get 429'd; we back off harder and longer so a
    transient rate-limit RECOVERS instead of dropping the name — this is what makes a
    multi-name run produce a CONSISTENT event count instead of a different N each time.
    """
    params = dict(params)
    params["token"] = get_finnhub_key()
    url = f"{BASE}{path}"
    last_err: Any = None
    fivexx = 0
    for attempt in range(retries):
        try:
            resp = _session.get(url, params=params, timeout=12)
        except (requests.Timeout, requests.ConnectionError) as e:
            last_err = e
            time.sleep(min(2.0 * (attempt + 1), 12))  # transient network; back off + retry
            continue
        if resp.status_code == 403:
            return None  # premium-gated; degrade gracefully
        if resp.status_code == 429:
            time.sleep(min(3 * (attempt + 1), 20))  # rate limited; back off harder, keep trying
            continue
        if resp.status_code >= 500:  # upstream down (502/503/504) — won't recover in-request;
            fivexx += 1                # retry only briefly, then degrade so the UI doesn't hang.
            if fivexx >= 2:
                return None
            time.sleep(2.0)
            continue
        resp.raise_for_status()
        return resp.json()
    if last_err:
        return None  # exhausted retries on network error; let caller degrade
    raise RuntimeError(f"Finnhub rate-limited repeatedly on {path}")


def resolve_symbol(query: str) -> Optional[Dict[str, str]]:
    """Resolve a company name or ticker to a primary US symbol.

    Returns {"symbol", "description"} or None. If the query already looks like a
    clean ticker we still verify it via search to get the display name.
    """
    data = _get("/search", {"q": query})
    if not data or not data.get("result"):
        return None
    results = data["result"]
    q_upper = query.strip().upper()
    # Prefer an exact ticker match, then a plain common-stock symbol.
    for r in results:
        if r.get("symbol", "").upper() == q_upper:
            return {"symbol": r["symbol"], "description": r.get("description", "")}
    for r in results:
        sym = r.get("symbol", "")
        if sym and "." not in sym and r.get("type") in ("Common Stock", ""):
            return {"symbol": sym, "description": r.get("description", "")}
    r = results[0]
    return {"symbol": r["symbol"], "description": r.get("description", "")}


def get_next_earnings(symbol: str) -> Optional[Dict[str, Any]]:
    """Next scheduled earnings event with consensus EPS + revenue estimates.

    Pulls a +90-day window from the earnings calendar and returns the soonest
    future event. Fields: date, epsEstimate, revenueEstimate, hour, quarter, year.
    Returns None if nothing scheduled.
    """
    today = date.today()
    data = _get(
        "/calendar/earnings",
        {
            "from": today.isoformat(),
            "to": (today + timedelta(days=90)).isoformat(),
            "symbol": symbol,
        },
    )
    if not data:
        return None
    events = data.get("earningsCalendar", [])
    future = [e for e in events if e.get("date", "") >= today.isoformat()]
    if not future:
        return None
    future.sort(key=lambda e: e["date"])
    e = future[0]
    return {
        "date": e.get("date"),
        "epsEstimate": e.get("epsEstimate"),
        "revenueEstimate": e.get("revenueEstimate"),
        "hour": e.get("hour"),
        "quarter": e.get("quarter"),
        "year": e.get("year"),
    }


def get_earnings_surprises(symbol: str, limit: int = 8) -> List[Dict[str, Any]]:
    """Historical EPS surprises, most recent first.

    Each item: period, actual, estimate, surprise, surprisePercent.
    """
    data = _get("/stock/earnings", {"symbol": symbol, "limit": limit})
    if not data:
        return []
    out = []
    for e in data:
        out.append(
            {
                "period": e.get("period"),
                "actual": e.get("actual"),
                "estimate": e.get("estimate"),
                "surprise": e.get("surprise"),
                "surprisePercent": e.get("surprisePercent"),
            }
        )
    # Sort newest-first EXPLICITLY. The recency-decay in surprise_model assumes index 0 = newest;
    # Finnhub happens to return that order but doesn't guarantee it (fix 2026-09-21).
    out.sort(key=lambda r: r.get("period") or "", reverse=True)
    return out


def get_company_news(symbol: str, days: int = 14) -> List[Dict[str, Any]]:
    """Recent company headlines (free tier). Each item: headline, summary, datetime.

    Used for a lightweight news-sentiment signal. Returns [] if none / gated.
    """
    today = date.today()
    data = _get(
        "/company-news",
        {
            "symbol": symbol,
            "from": (today - timedelta(days=days)).isoformat(),
            "to": today.isoformat(),
        },
    )
    if not data:
        return []
    return [
        {"headline": n.get("headline", ""), "summary": n.get("summary", ""),
         "datetime": n.get("datetime")}
        for n in data
    ]


def get_peers(symbol: str) -> List[str]:
    """Sector/industry peer tickers (free). First element is usually the symbol itself."""
    data = _get("/stock/peers", {"symbol": symbol})
    return data if isinstance(data, list) else []


def peer_beat_rate(symbol: str, max_peers: int = 6) -> Optional[Dict[str, Any]]:
    """Fraction of peers whose most recent EPS surprise was positive.

    Sector tailwind/headwind context: if peers are broadly beating, the base rate
    for this name tilts up. None if no peer data."""
    peers = [p for p in get_peers(symbol) if p and p.upper() != symbol.upper()][:max_peers]
    if not peers:
        return None
    beats = total = 0
    for p in peers:
        surp = get_earnings_surprises(p, limit=1)
        if surp and surp[0].get("surprise") is not None:
            total += 1
            beats += 1 if surp[0]["surprise"] > 0 else 0
    if total == 0:
        return None
    return {"rate": beats / total, "n": total, "peers": peers}


def get_recommendation_trends(symbol: str, limit: int = 4) -> List[Dict[str, Any]]:
    """Analyst recommendation buckets by month, most recent first.

    Free-tier proxy for analyst-revision direction (Stickel 1991). Each item:
    period, strongBuy, buy, hold, sell, strongSell.
    """
    data = _get("/stock/recommendation", {"symbol": symbol})
    if not data:
        return []
    data.sort(key=lambda d: d.get("period", ""), reverse=True)
    return data[:limit]

"""Polymarket access via the public Gamma + CLOB APIs (no key required).

The user pastes a market or event URL/slug. We resolve it to a single binary
market and return its implied probability, spread, liquidity, and the
resolution text (criteria) so the agent never ignores how the contract settles.

Gamma:  https://gamma-api.polymarket.com   (market metadata, outcomes, prices)
CLOB:   https://clob.polymarket.com        (live midpoint / book for a token)
"""
from __future__ import annotations

import json
import re
from typing import Any, Dict, List, Optional

import requests

GAMMA = "https://gamma-api.polymarket.com"
CLOB = "https://clob.polymarket.com"
_session = requests.Session()


def parse_slug(url_or_slug: str) -> str:
    """Extract the trailing slug from a Polymarket URL, or pass a bare slug through."""
    s = url_or_slug.strip()
    if "polymarket.com" not in s:
        return s.strip("/")
    # .../event/<event-slug>[/<market-slug>][?query]
    path = re.sub(r"https?://", "", s).split("?")[0]
    parts = [p for p in path.split("/") if p]
    # drop domain and the 'event'/'market' segment
    parts = [p for p in parts if p not in ("polymarket.com", "event", "market", "markets")]
    return parts[-1] if parts else s


def _get(base: str, path: str, params: Optional[Dict[str, Any]] = None) -> Any:
    resp = _session.get(f"{base}{path}", params=params or {}, timeout=20)
    resp.raise_for_status()
    return resp.json()


def _loads(val: Any) -> Any:
    """Gamma returns some array fields as JSON strings; normalize to Python lists."""
    if isinstance(val, str):
        try:
            return json.loads(val)
        except Exception:
            return val
    return val


def _fetch_markets_by_slug(slug: str) -> List[Dict[str, Any]]:
    """Resolve a slug to one or more markets, trying market then event lookup."""
    data = _get(GAMMA, "/markets", {"slug": slug})
    if data:
        return data if isinstance(data, list) else [data]
    events = _get(GAMMA, "/events", {"slug": slug})
    if events:
        ev = events[0] if isinstance(events, list) else events
        return ev.get("markets", []) or []
    return []


def _clob_midpoint(token_id: str) -> Optional[float]:
    try:
        data = _get(CLOB, "/midpoint", {"token_id": token_id})
        return float(data["mid"]) if data and "mid" in data else None
    except Exception:
        return None


def _clob_book(token_id: str) -> Dict[str, Any]:
    """Live order book for the YES token: best bid/ask, true spread, top depth ($).

    Gives a real net-of-cost EV instead of the Gamma spread estimate, and a real
    liquidity read. Returns {} if the book is unavailable."""
    try:
        data = _get(CLOB, "/book", {"token_id": token_id})
        if not data:
            return {}
        bids = data.get("bids") or []
        asks = data.get("asks") or []
        best_bid = max((float(b["price"]) for b in bids), default=None)
        best_ask = min((float(a["price"]) for a in asks), default=None)
        # Dollar depth within ~3c of the best on each side.
        def _depth(levels, ref, side):
            tot = 0.0
            for lv in levels:
                p, s = float(lv["price"]), float(lv["size"])
                if ref is not None and abs(p - ref) <= 0.03:
                    tot += p * s
            return tot
        depth = _depth(bids, best_bid, "b") + _depth(asks, best_ask, "a")
        spread = (best_ask - best_bid) if (best_bid is not None and best_ask is not None) else None
        # Top levels (price, size) for executable-edge / slippage walking.
        ask_levels = sorted(([float(a["price"]), float(a["size"])] for a in asks),
                            key=lambda x: x[0])[:15]
        bid_levels = sorted(([float(b["price"]), float(b["size"])] for b in bids),
                            key=lambda x: -x[0])[:15]
        return {"best_bid": best_bid, "best_ask": best_ask,
                "book_spread": spread, "depth_usd": depth or None,
                "ask_levels": ask_levels, "bid_levels": bid_levels}
    except Exception:
        return {}


_EARNINGS_HINTS = ("beat", "miss", "earnings", "eps", "revenue", "guidance",
                   "report", "up or down", "above", "below", "exceed")


def search_markets(query: str, limit: int = 8) -> List[Dict[str, Any]]:
    """Search Polymarket for candidate markets via the public-search endpoint.

    Returns OPEN markets first (closed last), earnings-flavored questions ranked
    above generic ones, then by volume. Each candidate: question, event_title,
    slug (pass to get_market), url, yes_price, volume, closed.
    """
    try:
        data = _get(GAMMA, "/public-search", {"q": query, "limit_per_type": limit})
    except Exception:
        return []
    events = (data or {}).get("events", []) or []
    cands: List[Dict[str, Any]] = []
    for ev in events:
        ev_slug = ev.get("slug", "")
        for m in ev.get("markets", []) or []:
            prices = _loads(m.get("outcomePrices")) or []
            yes = None
            if prices:
                try:
                    yes = float(prices[0])
                except (TypeError, ValueError):
                    yes = None
            q = (m.get("question") or "")
            cands.append({
                "question": q,
                "event_title": ev.get("title"),
                "slug": m.get("slug") or ev_slug,
                "url": f"https://polymarket.com/event/{ev_slug}",
                "yes_price": yes,
                "volume": float(m.get("volumeNum") or 0),
                "closed": bool(m.get("closed")),
                "earnings_like": any(h in q.lower() for h in _EARNINGS_HINTS),
                "inferred": infer_outcome(q),
            })
    cands.sort(key=lambda c: (c["closed"], not c["earnings_like"], -c["volume"]))
    return cands[:limit]


def get_price_history(token_id: str, interval: str = "1w", fidelity: int = 180
                      ) -> Dict[str, Any]:
    """Recent YES-price history + momentum (is the crowd drifting toward our view?).

    Returns {points, first, last, momentum} where momentum = last - first over the
    window. {} if unavailable."""
    try:
        data = _get(CLOB, "/prices-history",
                    {"market": token_id, "interval": interval, "fidelity": fidelity})
        hist = (data or {}).get("history") or []
        if len(hist) < 2:
            return {}
        first, last = float(hist[0]["p"]), float(hist[-1]["p"])
        return {"points": len(hist), "first": first, "last": last,
                "momentum": last - first}
    except Exception:
        return {}


# Heuristic: infer the outcome type + threshold a market resolves on, from its text.
_NUM = r"(\d[\d,\.]*)"


def infer_outcome(question: str) -> Dict[str, Any]:
    """Best-effort map a Polymarket question to our outcome + threshold, so the
    model tests the SAME thing the market resolves on."""
    import re
    q = (question or "").lower()
    def num(pat):
        m = re.search(pat, q)
        if not m:
            return None
        try:
            return float(m.group(1).replace(",", ""))
        except ValueError:
            return None
    if "revenue" in q or "sales" in q:
        if "$" in q or "billion" in q or "trillion" in q:
            v = num(r"\$?\s*" + _NUM)
            mult = 1e9 if "billion" in q else (1e12 if "trillion" in q else 1)
            return {"outcome": "revenue_over", "threshold": (v * mult) if v else None,
                    "label": "Revenue over threshold"}
        return {"outcome": "revenue_beat", "threshold": None, "label": "Revenue beat"}
    if "eps" in q or "earnings per share" in q:
        pct = num(_NUM + r"\s*%")
        if pct is not None and ("beat" in q or "above" in q or "exceed" in q):
            return {"outcome": "eps_surprise_over", "threshold": pct, "label": "EPS surprise over %"}
        return {"outcome": "eps_beat", "threshold": None, "label": "EPS beat"}
    if "beat" in q and ("earnings" in q or "profit" in q):
        return {"outcome": "eps_beat", "threshold": None, "label": "Earnings beat"}
    if any(w in q for w in ("above", "up ", "higher", "close above", "finish", "rise", "increase")):
        return {"outcome": "stock_up", "threshold": None, "label": "Stock up / above level"}
    return {"outcome": None, "threshold": None, "label": "could not infer"}


def parse_eps_strike(text: str) -> Dict[str, Any]:
    """Extract the FIXED EPS strike + resolution mechanics from a contract's question +
    resolution rules (critique #3: the contract resolves on 'reported EPS <op> fixed strike',
    NOT 'beat current consensus' — different events). Returns
    {strike, operator, equality_no, non_gaap, rounding} or {strike: None} when no explicit
    EPS-adjacent dollar strike is stated (fall back to consensus-beat).

    Rewritten 2026-09-21 against REAL Polymarket resolution text, which states the strike explicitly
    as the consensus FROZEN at market creation, e.g. "The Street consensus estimate for ... non-GAAP
    EPS ... is $2.39 as of market creation. This market will resolve to 'Yes' if ... reports non-GAAP
    EPS greater than $2.39." So the strike is nearly universal (not rare). Key robustness rule: a
    strike must be a DOLLAR figure WITH CENTS ($X.XX) — this alone rejects the date/year noise that
    broke the old parser (it grabbed 2025 from '2025-09-25', or a stray int). Also rejects any
    year-like value (1900-2100) as a final guard, and reads the comparison operator."""
    import re
    t = (text or "").lower()

    op = ">"
    if re.search(r"at least|greater than or equal|>=|no less than|or more|or higher|or above", t):
        op = ">="
    elif re.search(r"less than|below|under", t) and not re.search(r"greater|above|exceed|beat", t):
        op = "<"

    # A strike is always a $X.XX (cents mandatory). Try, in priority order, the resolution clause,
    # the stated consensus, a generic 'greater than $X.XX', then any EPS-context $X.XX.
    DOLLAR = r"\$?\s*(\d{1,3}\.\d{2})"
    pats = [
        r"resolve[^.]{0,90}?(?:greater than|above|exceeds?|at least|no less than|>=?|less than|below)\s*" + DOLLAR,
        r"consensus[^.]{0,70}?(?:is|of|:)\s*" + DOLLAR,
        r"(?:greater than|above|exceeds?|at least|no less than|less than|below)\s*" + DOLLAR,
        r"(?:non-?gaap eps|adjusted eps|diluted eps|eps|earnings per share)[^.$%]{0,40}?" + DOLLAR,
    ]
    strike = None
    for pat in pats:
        m = re.search(pat, t)
        if m:
            try:
                v = float(m.group(1))
            except ValueError:
                continue
            if not (1900 <= v <= 2100):        # never a year
                strike = v
                break

    # EPS BASIS (added 2026-11-03). Three states, not a boolean — "not non-GAAP" is NOT the
    # same as "explicitly GAAP". The distinction decides whether our consensus is even the
    # right unit: analyst consensus (yfinance/Finnhub) is ADJUSTED / non-GAAP, so a contract
    # resolving on GAAP EPS is a DIFFERENT measure. UBER 2026-11-04 is the worked example:
    # the contract pays on GAAP EPS > $0.98 (which IS the GAAP consensus), while our
    # consensus was the $0.90 adjusted figure — so the "strike gap" read as -8.5% when it
    # was really ~0, and the surprise distribution came from adjusted history. That produced
    # a 68% model probability against a 19% market, i.e. a unit error dressed as an edge.
    is_non_gaap = ("non-gaap" in t or "non gaap" in t or "adjusted" in t)
    is_gaap = (not is_non_gaap) and re.search(r"\bgaap\b", t) is not None
    basis = "non_gaap" if is_non_gaap else ("gaap" if is_gaap else None)

    return {"strike": strike, "operator": op,
            "equality_no": (op == ">"),
            "non_gaap": is_non_gaap,
            "eps_basis": basis,
            "rounding": 0.01 if ("round" in t or "nearest cent" in t) else None}


def get_market(url_or_slug: str) -> Optional[Dict[str, Any]]:
    """Return a normalized binary market, or None if it can't be resolved.

    Keys:
      question, slug, resolution_text, end_date, closed,
      outcomes (list, e.g. ["Yes","No"]),
      yes_price (implied prob of YES in [0,1]),
      spread, liquidity, volume, token_ids
    """
    slug = parse_slug(url_or_slug)
    markets = _fetch_markets_by_slug(slug)
    if not markets:
        return None
    m = markets[0]  # user pasted a specific market; take the first resolved

    outcomes = _loads(m.get("outcomes")) or ["Yes", "No"]
    prices = _loads(m.get("outcomePrices")) or []
    token_ids = _loads(m.get("clobTokenIds")) or []

    # Prefer a live CLOB midpoint for the YES token; fall back to Gamma price.
    yes_price: Optional[float] = None
    book: Dict[str, Any] = {}
    hist: Dict[str, Any] = {}
    if token_ids:
        yes_price = _clob_midpoint(str(token_ids[0]))
        book = _clob_book(str(token_ids[0]))
        hist = get_price_history(str(token_ids[0]))
    if yes_price is None and book.get("best_bid") is not None and book.get("best_ask") is not None:
        yes_price = (book["best_bid"] + book["best_ask"]) / 2.0
    if yes_price is None and prices:
        try:
            yes_price = float(prices[0])
        except (TypeError, ValueError):
            yes_price = None

    def _num(*keys: str) -> Optional[float]:
        for k in keys:
            v = m.get(k)
            if v is not None:
                try:
                    return float(v)
                except (TypeError, ValueError):
                    pass
        return None

    return {
        "question": m.get("question"),
        "slug": m.get("slug", slug),
        "resolution_text": m.get("description") or m.get("resolutionSource"),
        "end_date": m.get("endDate"),
        "closed": bool(m.get("closed")),
        "outcomes": outcomes,
        "yes_price": yes_price,
        "spread": _num("spread"),
        "liquidity": _num("liquidityNum", "liquidity"),
        "volume": _num("volumeNum", "volume"),
        "token_ids": [str(t) for t in token_ids],
        "best_bid": book.get("best_bid"),
        "best_ask": book.get("best_ask"),
        "book_spread": book.get("book_spread"),
        "depth_usd": book.get("depth_usd"),
        "price_momentum": hist.get("momentum"),
        "ask_levels": book.get("ask_levels"),
        "bid_levels": book.get("bid_levels"),
        "inferred": infer_outcome(m.get("question") or ""),
    }

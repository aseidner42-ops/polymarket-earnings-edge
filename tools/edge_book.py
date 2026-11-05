"""Edge Book — persistent store of every real + potential edge the scanner has seen, with
only the STILL-ACTIVE ones surfaced (an edge is active until its earnings date passes).

Unlike the EDGE_FOUND.txt / POTENTIAL_EDGE.txt flags (overwritten each scan), this
ACCUMULATES across scans: upserts by market, tracks first_seen / last_seen, keeps the best
tier ('real' beats 'potential'), and drops entries once the print has happened. Written by
both the daily job and on-demand app scans, so it's always current.
"""
from __future__ import annotations

import json
from datetime import date, datetime
from typing import Any, Dict, List

from tools.config import OUTPUT_DIR

_BOOK = OUTPUT_DIR / "edge_book.json"


def _load() -> Dict[str, Any]:
    try:
        return json.loads(_BOOK.read_text())
    except Exception:
        return {}


def _save(book: Dict[str, Any]) -> None:
    _BOOK.write_text(json.dumps(book, indent=2))


def _key(r: Dict[str, Any]) -> str:
    return (r.get("slug")
            or f"{r.get('ticker')}|{r.get('outcome')}|{r.get('earnings_date')}")


def _fields(r: Dict[str, Any], tier: str, stamp: str) -> Dict[str, Any]:
    return {
        "ticker": r.get("ticker"), "outcome": r.get("outcome"),
        "earnings_date": r.get("earnings_date"), "side": r.get("side"),
        "model_prob": r.get("model_prob"), "market_prob": r.get("market_prob"),
        "net_edge": r.get("net_edge"), "executable_edge": r.get("executable_edge"),
        "max_usd": r.get("max_usd"), "scar": r.get("predictability"),
        "edge_quality": r.get("edge_quality"), "url": r.get("url"),
        "slug": r.get("slug"), "threshold": r.get("threshold"),
        "fade_beat": bool(r.get("fade_beat")),
        "tier": tier, "last_seen": stamp,
    }


def upsert(believable: List[Dict[str, Any]], potential: List[Dict[str, Any]],
           stamp: str) -> Dict[str, Any]:
    """Add/update this scan's real + potential edges. 'real' never downgraded to 'potential'.
    Returns the pruned active view."""
    book = _load()
    for r in potential:
        k = _key(r)
        prior = book.get(k, {})
        if prior.get("tier") == "real":
            continue  # keep the stronger tier
        rec = _fields(r, "potential", stamp)
        rec["first_seen"] = prior.get("first_seen", stamp)
        book[k] = rec
    for r in believable:  # real overrides potential
        k = _key(r)
        prior = book.get(k, {})
        rec = _fields(r, "real", stamp)
        rec["first_seen"] = prior.get("first_seen", stamp)
        book[k] = rec
    _save(book)
    return active()


def _is_active(rec: Dict[str, Any]) -> bool:
    """Active until the earnings print happens. Unknown date -> keep 14 days from last_seen."""
    ed = rec.get("earnings_date")
    today = date.today()
    if ed:
        try:
            return datetime.strptime(str(ed)[:10], "%Y-%m-%d").date() >= today
        except ValueError:
            pass
    try:
        ls = datetime.strptime(str(rec.get("last_seen", ""))[:10], "%Y-%m-%d").date()
        return (today - ls).days <= 14
    except ValueError:
        return True


def active(prune: bool = True) -> Dict[str, Any]:
    """Return {real:[...], potential:[...]} of still-active edges, newest-quality first.
    Prunes expired entries from the stored book by default."""
    book = _load()
    keep = {k: v for k, v in book.items() if _is_active(v)}
    if prune and len(keep) != len(book):
        _save(keep)
    real = sorted([v for v in keep.values() if v.get("tier") == "real"],
                  key=lambda x: -(x.get("edge_quality") or 0))
    pot = sorted([v for v in keep.values() if v.get("tier") == "potential"],
                 key=lambda x: -abs((x.get("model_prob") or 0) - (x.get("market_prob") or 0)))
    return {"real": real, "potential": pot,
            "n_real": len(real), "n_potential": len(pot)}

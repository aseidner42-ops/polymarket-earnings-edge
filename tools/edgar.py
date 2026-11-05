"""SEC EDGAR — recent filing dates for earnings-timing cross-check (free).

Maps a ticker to its CIK, then reads the company's recent submissions to surface
the latest 8-K (often the earnings press release) and 10-Q/10-K dates. Used as a
timing sanity check on the approximate earnings date — not a trading signal.
SEC requires a descriptive User-Agent.
"""
from __future__ import annotations

from datetime import datetime
from functools import lru_cache
from typing import Any, Dict, Optional

import requests

UA = {"User-Agent": "earnings-edge research seidner.ab@northeastern.edu"}
_session = requests.Session()


@lru_cache(maxsize=1)
def _ticker_cik_map() -> Dict[str, str]:
    try:
        data = _session.get("https://www.sec.gov/files/company_tickers.json",
                            headers=UA, timeout=20).json()
        return {row["ticker"].upper(): str(row["cik_str"]).zfill(10)
                for row in data.values()}
    except Exception:
        return {}


# 8-K item codes that signal trouble (free "event risk" — what paid terminals charge for).
BEARISH_8K_ITEMS = {
    "1.03": (1.00, "bankruptcy"),
    "4.02": (0.90, "restatement / non-reliance on prior financials"),
    "2.06": (0.60, "material impairment"),
    "2.04": (0.50, "debt acceleration / triggering event"),
    "2.05": (0.45, "restructuring / exit & disposal costs"),
    "5.02": (0.30, "officer/director departure"),
    "8.01": (0.12, "other event (often a guidance update/warning)"),
}


@lru_cache(maxsize=256)
def get_8k_items(ticker: str) -> tuple:
    """All 8-K filings as (filingDate, [item codes]) — cached. Item codes come straight
    from EDGAR (no document download needed), so this is fast + point-in-time."""
    cik = _ticker_cik_map().get(ticker.upper())
    if not cik:
        return tuple()
    try:
        data = _session.get(f"https://data.sec.gov/submissions/CIK{cik}.json",
                            headers=UA, timeout=20).json()
    except Exception:
        return tuple()
    rec = (data.get("filings") or {}).get("recent") or {}
    forms, dates = rec.get("form", []), rec.get("filingDate", [])
    items = rec.get("items", [""] * len(forms))
    out = []
    for f, d, it in zip(forms, dates, items):
        if f == "8-K":
            out.append((d, [x.strip() for x in (it or "").split(",") if x.strip()]))
    return tuple(out)


def filing_risk(ticker: str, asof: Optional[str] = None, lookback_days: int = 150) -> Dict[str, Any]:
    """Event-risk score from 8-K item codes filed in the `lookback_days` before `asof`
    (ISO date, or None = now). Higher = more trouble signals (restructuring, departures,
    impairments, restatements). Point-in-time, so it's backtestable. {} if no data."""
    from datetime import date as _date, timedelta as _td
    items = get_8k_items(ticker)
    if not items:
        return {}
    end = (datetime.strptime(asof, "%Y-%m-%d").date() if asof else _date.today())
    start = end - _td(days=lookback_days)
    score, flags = 0.0, []
    for d, codes in items:
        try:
            fd = datetime.strptime(d, "%Y-%m-%d").date()
        except ValueError:
            continue
        if not (start <= fd < end):
            continue
        for c in codes:
            if c in BEARISH_8K_ITEMS:
                wt, label = BEARISH_8K_ITEMS[c]
                score += wt
                flags.append(f"{d}: {label}")
    return {"risk": min(1.0, score), "flags": flags[:6]}


@lru_cache(maxsize=256)
def get_recent_filings(ticker: str) -> Dict[str, Any]:
    """Latest 8-K and 10-Q/10-K filing dates for the ticker. {} if unavailable."""
    cik = _ticker_cik_map().get(ticker.upper())
    if not cik:
        return {}
    try:
        data = _session.get(f"https://data.sec.gov/submissions/CIK{cik}.json",
                            headers=UA, timeout=20).json()
    except Exception:
        return {}
    recent = (data.get("filings") or {}).get("recent") or {}
    forms = recent.get("form") or []
    dates = recent.get("filingDate") or []
    out: Dict[str, Any] = {"cik": cik}
    for form, date in zip(forms, dates):
        if form == "8-K" and "last_8k" not in out:
            out["last_8k"] = date
        if form in ("10-Q", "10-K") and "last_periodic" not in out:
            out["last_periodic"] = date
        if "last_8k" in out and "last_periodic" in out:
            break
    return out

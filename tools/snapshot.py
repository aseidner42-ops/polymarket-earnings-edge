"""TradingView market-snapshot ingestion + Kelly (2016) over-extrapolation measure.

CENTRAL THEORY — Ertan, Karolyi, Kelly & Stoumbos (2016),
"Pre-Earnings Announcement Over-Extrapolation":
  Investors over-extrapolate from a firm's PAST EARNINGS-ANNOUNCEMENT returns.
  When recent announcement returns are high, investors become overly optimistic,
  over-buy in the ~5 days before the next print (run-up), and the stock then
  REVERSES after the announcement. Their predictor is the "extrapolated return
  measure" (ExtRet): a recency-weighted average of the 2-day announcement-window
  returns ([0,+1]) over the firm's last 8 earnings announcements (quarters -8..-1).

This module turns a multi-year price snapshot (exported from TradingView, or
pulled via the TradingView MCP and saved to CSV) into ExtRet. A high ExtRet =>
over-extrapolation => beat likely already priced in (Q2) and downside reversal
risk after the print (Q3). Only ~2 years of DAILY bars are needed for the 8
windows; longer snapshots (5-10y) add distributional context.
"""
from __future__ import annotations

import csv
import re
from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional, Tuple


# --- timeframe parsing ------------------------------------------------------
def parse_timeframe(text: str) -> Dict[str, Any]:
    """Parse a user snapshot timeframe in days/weeks/years.

    Accepts e.g. "5y", "10 years", "180d", "52w", "3 yr". Returns the recommended
    TradingView resolution + approx bar count for pulling, plus a normalized label.
    Kelly's ExtRet needs ~8 quarters (~2y) of DAILY bars, so daily is preferred for
    accuracy; long horizons fall back to weekly to stay within the 500-bar pull cap.
    """
    m = re.search(r"(\d+(?:\.\d+)?)\s*([a-zA-Z]+)", text.strip())
    if not m:
        raise ValueError(f"could not parse timeframe '{text}' (try '5y', '180d', '52w')")
    n = float(m.group(1))
    unit = m.group(2).lower()
    if unit.startswith("d"):
        days = n
    elif unit.startswith("w"):
        days = n * 7
    elif unit.startswith("y"):
        days = n * 365
    else:
        raise ValueError(f"unknown timeframe unit '{unit}' (use d / w / y)")

    # Choose resolution so the pull fits in ~500 bars.
    trading_days = days * (252.0 / 365.0)
    if trading_days <= 500:
        resolution, bars = "D", int(round(trading_days))
    elif trading_days / 5 <= 500:        # weekly
        resolution, bars = "W", int(round(days / 7))
    else:                                 # monthly
        resolution, bars = "M", int(round(days / 30))
    return {"days": days, "resolution": resolution,
            "bars": min(bars, 500), "label": f"{m.group(1)}{unit[0]}"}


# --- snapshot loading -------------------------------------------------------
def _to_date(val: str) -> Optional[datetime]:
    val = str(val).strip()
    if not val:
        return None
    if re.fullmatch(r"\d{10,}", val):              # unix seconds (TV MCP bars)
        return datetime.utcfromtimestamp(int(val))
    for fmt in ("%Y-%m-%dT%H:%M:%S", "%Y-%m-%d %H:%M:%S", "%Y-%m-%d", "%m/%d/%Y"):
        try:
            return datetime.strptime(val[:len(fmt) + 2].split("+")[0].strip(), fmt)
        except ValueError:
            continue
    try:
        return datetime.fromisoformat(val.replace("Z", "").split("+")[0])
    except ValueError:
        return None


def load_snapshot(path: str) -> List[Dict[str, Any]]:
    """Load an OHLCV snapshot CSV (TradingView export or MCP-saved) into sorted
    bars: [{date, close, volume}, ...]. Tolerant of column-name casing/aliases."""
    rows: List[Dict[str, Any]] = []
    with open(path, newline="") as fh:
        reader = csv.DictReader(fh)
        cols = {c.lower().strip(): c for c in (reader.fieldnames or [])}
        time_col = next((cols[k] for k in ("time", "date", "datetime") if k in cols), None)
        close_col = next((cols[k] for k in ("close", "close ", "last") if k in cols), None)
        vol_col = next((cols[k] for k in ("volume", "vol") if k in cols), None)
        if not time_col or not close_col:
            raise ValueError(f"snapshot {path} needs time + close columns; got {reader.fieldnames}")
        for r in reader:
            d = _to_date(r.get(time_col, ""))
            try:
                close = float(r.get(close_col, ""))
            except (TypeError, ValueError):
                close = None
            if d is None or close is None:
                continue
            try:
                vol = float(r.get(vol_col, "")) if vol_col else None
            except (TypeError, ValueError):
                vol = None
            rows.append({"date": d, "close": close, "volume": vol})
    rows.sort(key=lambda x: x["date"])
    return rows


# --- Kelly extrapolated-return measure -------------------------------------
def _bar_index_on_or_after(bars: List[Dict[str, Any]], when: datetime) -> Optional[int]:
    for i, b in enumerate(bars):
        if b["date"] >= when:
            return i
    return None


def announcement_return(bars: List[Dict[str, Any]], announce: datetime) -> Optional[float]:
    """2-day [0,+1] buy-and-hold return around an announcement date.

    Approximated as close[i+1]/close[i-1]-1 where i is the first bar on/after the
    date (≈ paper's [0,+1] for daily bars; coarser for weekly/monthly snapshots)."""
    i = _bar_index_on_or_after(bars, announce)
    if i is None or i - 1 < 0 or i + 1 >= len(bars):
        return None
    base = bars[i - 1]["close"]
    end = bars[i + 1]["close"]
    if base <= 0:
        return None
    return end / base - 1.0


def extrapolated_return_measure(bars: List[Dict[str, Any]],
                                announce_dates: List[datetime],
                                n_quarters: int = 8) -> Dict[str, Any]:
    """Kelly ExtRet: recency-weighted average of the last `n_quarters` announcement
    returns. announce_dates must be most-recent-first. Weights decay linearly so the
    most recent quarter is heaviest. Also returns the positive-return streak.

    Positive ExtRet => high past announcement returns => over-extrapolation =>
    over-buying pre-print and reversal after.
    """
    ars: List[float] = []
    for d in announce_dates[:n_quarters]:
        ar = announcement_return(bars, d)
        if ar is not None:
            ars.append(ar)
    if not ars:
        return {"ext_ret": None, "n_quarters": 0, "streak": None, "recent_ars": []}
    # Linear recency weights: most recent (index 0) heaviest.
    weights = [len(ars) - k for k in range(len(ars))]
    wsum = sum(weights)
    ext_ret = sum(w * ar for w, ar in zip(weights, ars)) / wsum
    streak = all(ar > 0 for ar in ars[:4]) if len(ars) >= 4 else None
    return {"ext_ret": ext_ret, "n_quarters": len(ars), "streak": streak,
            "recent_ars": [round(a, 4) for a in ars]}


def kelly_from_snapshot(path: str, announce_dates: List[datetime],
                        n_quarters: int = 8) -> Dict[str, Any]:
    """Convenience: load a snapshot CSV and compute ExtRet against announce dates."""
    bars = load_snapshot(path)
    out = extrapolated_return_measure(bars, announce_dates, n_quarters)
    out["bars"] = len(bars)
    out["span"] = (f"{bars[0]['date'].date()}..{bars[-1]['date'].date()}" if bars else None)
    return out

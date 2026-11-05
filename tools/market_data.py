"""Market data via yfinance: prices, volume, relative strength, options.

All functions degrade gracefully — return None / empty when data is missing —
because the scoring layer treats absent signals as "no information" rather than
crashing. Options data is only available live (no history), so options-based
signals are simply absent in the backtester.
"""
from __future__ import annotations

from datetime import datetime
from functools import lru_cache
from typing import Any, Dict, Optional

import csv as _csv
import io as _io

import numpy as np
import pandas as pd
import requests
import yfinance as yf

# GICS sector -> representative SPDR sector ETF, for relative-strength comparison.
SECTOR_ETF = {
    "Technology": "XLK",
    "Financial Services": "XLF",
    "Financial": "XLF",
    "Healthcare": "XLV",
    "Consumer Cyclical": "XLY",
    "Consumer Defensive": "XLP",
    "Energy": "XLE",
    "Industrials": "XLI",
    "Basic Materials": "XLB",
    "Real Estate": "XLRE",
    "Utilities": "XLU",
    "Communication Services": "XLC",
}
INDEX_ETF = "SPY"


@lru_cache(maxsize=256)
def _history(symbol: str, period: str = "2y") -> Optional[pd.DataFrame]:
    """Price history, cached per (symbol, period) so a basket backtest fetches
    each symbol once instead of once per quarter."""
    try:
        df = yf.Ticker(symbol).history(period=period, auto_adjust=True)
        return df if df is not None and not df.empty else None
    except Exception:
        return None


def _cumret(df: pd.DataFrame, lookback: int, asof: Optional[datetime]) -> Optional[float]:
    """Cumulative close-to-close return over the last `lookback` bars up to asof."""
    if df is None or df.empty:
        return None
    closes = df["Close"]
    if asof is not None:
        closes = closes[closes.index <= pd.Timestamp(asof, tz=closes.index.tz)]
    closes = closes.dropna()
    if len(closes) < lookback + 1:
        return None
    window = closes.iloc[-(lookback + 1):]
    return float(window.iloc[-1] / window.iloc[0] - 1.0)


def expected_move(symbol: str, window: int = 5, baseline: int = 60,
                  asof: Optional[datetime] = None) -> Optional[float]:
    """The stock's own expected `window`-day move = trailing daily-return std (over
    `baseline` days ENDING just before the window, so the run-up itself doesn't inflate
    it) × sqrt(window). Pure price, point-in-time. Used by PUMP to normalize the
    pre-earnings run-up into 'how many expected moves has it already travelled'."""
    df = _history(symbol)
    if df is None or "Close" not in df:
        return None
    closes = df["Close"]
    if asof is not None:
        closes = closes[closes.index <= pd.Timestamp(asof, tz=closes.index.tz)]
    closes = closes.dropna()
    if len(closes) < baseline + window + 1:
        return None
    rets = closes.pct_change().dropna()
    base = rets.iloc[-(baseline + window):-window]  # exclude the run-up window
    sd = float(base.std())
    if sd <= 0:
        return None
    return sd * (window ** 0.5)


@lru_cache(maxsize=256)
def get_sector_etf(symbol: str) -> Optional[str]:
    try:
        sector = yf.Ticker(symbol).info.get("sector")
    except Exception:
        return None
    return SECTOR_ETF.get(sector)


def pre_earnings_return(symbol: str, lookback: int = 20,
                        asof: Optional[datetime] = None) -> Optional[float]:
    """Raw cumulative stock return over the `lookback` trading days before asof."""
    return _cumret(_history(symbol), lookback, asof)


def relative_strength(symbol: str, lookback: int = 20,
                      asof: Optional[datetime] = None) -> Dict[str, Optional[float]]:
    """Stock return minus index return, and minus sector return, over lookback.

    Positive = outperforming. Used for the relative-strength / momentum signal
    (Jegadeesh-Titman 1993).
    """
    stock = _cumret(_history(symbol), lookback, asof)
    idx = _cumret(_history(INDEX_ETF), lookback, asof)
    etf = get_sector_etf(symbol)
    sect = _cumret(_history(etf), lookback, asof) if etf else None
    return {
        "vs_index": (stock - idx) if (stock is not None and idx is not None) else None,
        "vs_sector": (stock - sect) if (stock is not None and sect is not None) else None,
    }


def volume_buildup(symbol: str, lookback: int = 5, baseline: int = 60,
                   asof: Optional[datetime] = None) -> Optional[float]:
    """Recent avg volume vs longer baseline, minus 1.

    >0 means volume is building ahead of earnings (Beaver 1968: volume reflects
    information arrival). Returns e.g. 0.4 = 40% above baseline.
    """
    df = _history(symbol)
    if df is None or "Volume" not in df:
        return None
    vol = df["Volume"]
    if asof is not None:
        vol = vol[vol.index <= pd.Timestamp(asof, tz=vol.index.tz)]
    vol = vol.dropna()
    if len(vol) < baseline + lookback:
        return None
    recent = vol.iloc[-lookback:].mean()
    base = vol.iloc[-(baseline + lookback):-lookback].mean()
    if base <= 0:
        return None
    return float(recent / base - 1.0)


@lru_cache(maxsize=256)
def get_earnings_history(symbol: str, limit: int = 24) -> tuple:
    """Past earnings with REAL announcement dates (yfinance get_earnings_dates).

    Returns a tuple (so it's lru-cacheable) of dicts, most-recent-first:
      {date, eps_estimate, eps_actual, surprise_pct}
    This replaces the Finnhub fiscal-period-end + 40d approximation and the
    ~4-quarter free cap — giving Kelly precise dates and a full 8 windows, and
    deepening the surprise-streak / SUE signals. Empty tuple on failure."""
    try:
        df = yf.Ticker(symbol).get_earnings_dates(limit=limit)
    except Exception:
        return tuple()
    if df is None or df.empty:
        return tuple()
    out = []
    for idx, row in df.iterrows():
        rep = row.get("Reported EPS")
        if rep is None or pd.isna(rep):
            continue  # future / unreported
        est = row.get("EPS Estimate")
        sp = row.get("Surprise(%)")
        try:
            d = idx.to_pydatetime().replace(tzinfo=None)
        except Exception:
            continue
        out.append({
            "date": d,
            "eps_estimate": None if (est is None or pd.isna(est)) else float(est),
            "eps_actual": float(rep),
            "surprise_pct": None if (sp is None or pd.isna(sp)) else float(sp),
        })
    out.sort(key=lambda x: x["date"], reverse=True)
    return tuple(out)


def _bars_from_stooq(symbol: str) -> list:
    """Free Stooq daily CSV fallback (no key). US tickers use the .us suffix."""
    try:
        url = f"https://stooq.com/q/d/l/?s={symbol.lower()}.us&i=d"
        text = requests.get(url, timeout=20).text
        rows = list(_csv.DictReader(_io.StringIO(text)))
        out = []
        for r in rows:
            try:
                out.append({"date": datetime.strptime(r["Date"], "%Y-%m-%d"),
                            "close": float(r["Close"]),
                            "volume": float(r.get("Volume") or 0)})
            except (ValueError, KeyError):
                continue
        return out
    except Exception:
        return []


def get_price_bars(symbol: str, period: str = "2y") -> list:
    """Auto-fetched OHLCV bars [{date, close, volume}], yfinance with Stooq fallback.

    Lets the engine compute the Kelly signal with NO user upload. Dates are tz-naive
    to align with the (naive) approximate announcement dates."""
    df = _history(symbol, period)
    if df is not None and not df.empty:
        out = []
        for ts, row in df.iterrows():
            d = ts.to_pydatetime().replace(tzinfo=None)
            try:
                out.append({"date": d, "close": float(row["Close"]),
                            "volume": float(row.get("Volume") or 0)})
            except (TypeError, ValueError):
                continue
        if out:
            return out
    return _bars_from_stooq(symbol)  # fallback


@lru_cache(maxsize=256)
def get_forward_eps_consensus(symbol: str) -> Dict[str, Any]:
    """FRESH forward EPS consensus for the NEXT (unreported) quarter, from yfinance's LIVE analyst
    estimates — overrides Finnhub's free calendar, which carries a PRIOR quarter's estimate forward
    (the GME $0.3737 stale-carry, critique #2). Returns {eps, n_analysts, source, revised_recently,
    calendar_eps} or {}.

    yfinance '0q' = the current/upcoming quarter's average estimate; eps_trend gives the same figure
    30/60/90 days ago, so we can tell it's a LIVE mark (moved recently) vs a stale stub."""
    out: Dict[str, Any] = {}
    try:
        df = yf.Ticker(symbol).earnings_estimate
        if df is not None and not df.empty and "0q" in df.index:
            r = df.loc["0q"]
            avg, n = r.get("avg"), r.get("numberOfAnalysts")
            if avg is not None and avg == avg:                 # not NaN
                out["eps"] = float(avg)
                out["n_analysts"] = int(n) if (n is not None and n == n) else None
                out["source"] = "yfinance_estimate"
    except Exception:
        pass
    try:
        tr = yf.Ticker(symbol).eps_trend
        if tr is not None and not tr.empty and "0q" in tr.index:
            row = tr.loc["0q"]
            cur, old = row.get("current"), row.get("90daysAgo")
            if cur is not None and old is not None and cur == cur and old == old:
                out["revised_recently"] = abs(float(cur) - float(old)) > 1e-9
    except Exception:
        pass
    try:
        cal = yf.Ticker(symbol).calendar or {}
        ea = cal.get("Earnings Average")
        if ea is not None and ea == ea:
            out["calendar_eps"] = float(ea)
    except Exception:
        pass
    return out


def get_forecast_dispersion(symbol: str) -> Dict[str, Optional[float]]:
    """Analyst forecast dispersion + count for the current quarter (yfinance, free).

    Dispersion = (high - low) / |avg| of EPS estimates. High dispersion predicts
    LOWER, noisier outcomes (Diether-Malloy-Scherbina 2002, Johnson 2004); a tight
    consensus with many analysts is more predictable. {} if unavailable."""
    try:
        df = yf.Ticker(symbol).earnings_estimate
        if df is None or df.empty or "0q" not in df.index:
            return {}
        r = df.loc["0q"]
        avg, lo, hi = r.get("avg"), r.get("low"), r.get("high")
        n = r.get("numberOfAnalysts")
        if avg in (None, 0) or lo is None or hi is None:
            return {"n_analysts": int(n) if n is not None else None}
        return {"dispersion": float(hi - lo) / abs(float(avg)),
                "n_analysts": int(n) if n is not None else None}
    except Exception:
        return {}


@lru_cache(maxsize=256)
def get_accruals(symbol: str) -> Optional[float]:
    """Accruals ratio (Sloan 1996): HIGH accruals = earnings not backed by cash =
    lower quality / mean-reverting; LOW/negative = persistent.

    Preferred: balance-sheet working-capital accruals scaled by total assets,
      accruals = (ΔCurrentAssets − ΔCash) − (ΔCurrentLiabilities − ΔShortTermDebt)
                 − Depreciation,  all / Total Assets,  quarter-over-quarter.
    Fallback: the (Net Income − Operating Cash Flow) / Total Assets proxy.
    None if neither is computable."""
    try:
        t = yf.Ticker(symbol)
        ci, cf, bs = t.quarterly_income_stmt, t.quarterly_cashflow, t.quarterly_balance_sheet

        def col(df, key, i):
            try:
                if df is not None and key in df.index and len(df.columns) > i:
                    v = df.loc[key].iloc[i]
                    return None if pd.isna(v) else float(v)
            except Exception:
                return None
            return None

        def first(df, keys, i):
            for k in keys:
                v = col(df, k, i)
                if v is not None:
                    return v
            return None

        ta = first(bs, ["Total Assets"], 0)
        # Full working-capital accruals (needs two quarters of balance sheet).
        if ta and bs is not None and len(bs.columns) >= 2:
            d_ca = (first(bs, ["Current Assets"], 0) or 0) - (first(bs, ["Current Assets"], 1) or 0)
            d_cash = (first(bs, ["Cash And Cash Equivalents", "Cash Cash Equivalents And Short Term Investments"], 0) or 0) \
                     - (first(bs, ["Cash And Cash Equivalents", "Cash Cash Equivalents And Short Term Investments"], 1) or 0)
            d_cl = (first(bs, ["Current Liabilities"], 0) or 0) - (first(bs, ["Current Liabilities"], 1) or 0)
            d_std = (first(bs, ["Current Debt", "Current Debt And Capital Lease Obligation"], 0) or 0) \
                    - (first(bs, ["Current Debt", "Current Debt And Capital Lease Obligation"], 1) or 0)
            dep = first(cf, ["Depreciation And Amortization", "Depreciation"], 0) or 0
            if first(bs, ["Current Assets"], 0) is not None and first(bs, ["Current Liabilities"], 0) is not None:
                accr = ((d_ca - d_cash) - (d_cl - d_std) - dep) / ta
                return accr
        # Fallback proxy.
        ni = first(ci, ["Net Income"], 0)
        cfo = first(cf, ["Operating Cash Flow", "Total Cash From Operating Activities"], 0)
        if ni is not None and cfo is not None and ta:
            return (ni - cfo) / ta
        return None
    except Exception:
        return None


@lru_cache(maxsize=256)
def _quarterly_income(symbol: str):
    try:
        return yf.Ticker(symbol).quarterly_income_stmt
    except Exception:
        return None


@lru_cache(maxsize=256)
def _quarterly_statements(symbol: str):
    """(income, balance_sheet, cashflow) quarterly frames, cached. Any may be None."""
    try:
        t = yf.Ticker(symbol)
        return (t.quarterly_income_stmt, t.quarterly_balance_sheet, t.quarterly_cashflow)
    except Exception:
        return (None, None, None)


def get_piotroski(symbol: str, asof: Optional[datetime] = None) -> Dict[str, Optional[float]]:
    """Piotroski (2000) F-Score — up to 9 binary fundamental-health tests, compared
    YoY (same quarter prior year) to avoid seasonality. Free from yfinance quarterly
    financials. Returns {f_score, n_components, score_norm in [-1,1]} or {} when too
    few components are computable (shallow free history often lacks the year-ago quarter).
    Honest: yfinance quarterly frames are a CURRENT snapshot, so for old backtest events
    the needed columns may be absent -> signal simply absent, never fabricated."""
    try:
        ci, bs, cf = _quarterly_statements(symbol)
        if ci is None or bs is None or cf is None:
            return {}

        def _cols(df):
            cols = list(df.columns)
            if asof is not None:
                cols = [c for c in cols if pd.Timestamp(c).to_pydatetime().replace(tzinfo=None) <= asof]
            return sorted(cols, reverse=True)  # newest first

        def v(df, keys, i):
            if df is None:
                return None
            cols = _cols(df)
            if i >= len(cols):
                return None
            for k in keys:
                if k in df.index:
                    try:
                        x = df.loc[k][cols[i]]
                        if not pd.isna(x):
                            return float(x)
                    except Exception:
                        pass
            return None

        # YoY index: prefer 4 quarters back (same fiscal quarter); else fall back to oldest.
        ici, ibs, icf = len(_cols(ci)), len(_cols(bs)), len(_cols(cf))
        yi = 4 if min(ici, ibs) >= 5 else (max(min(ici, ibs) - 1, 1))

        ni0 = v(ci, ["Net Income", "Net Income Common Stockholders"], 0)
        cfo0 = v(cf, ["Operating Cash Flow", "Total Cash From Operating Activities"], 0)
        ta0 = v(bs, ["Total Assets"], 0)
        ta_y = v(bs, ["Total Assets"], yi)
        ni_y = v(ci, ["Net Income", "Net Income Common Stockholders"], yi)
        tests: Dict[str, int] = {}
        # Profitability
        if ni0 is not None:
            tests["roa_pos"] = int(ni0 > 0)
        if cfo0 is not None:
            tests["cfo_pos"] = int(cfo0 > 0)
        if None not in (ni0, ta0, ni_y, ta_y) and ta0 and ta_y:
            tests["d_roa"] = int((ni0 / ta0) > (ni_y / ta_y))
        if None not in (cfo0, ni0):
            tests["accrual"] = int(cfo0 > ni0)  # cash-backed earnings
        # Leverage / liquidity
        ltd0 = v(bs, ["Long Term Debt", "Long Term Debt And Capital Lease Obligation"], 0)
        ltd_y = v(bs, ["Long Term Debt", "Long Term Debt And Capital Lease Obligation"], yi)
        if None not in (ltd0, ta0, ltd_y, ta_y) and ta0 and ta_y:
            tests["d_lever"] = int((ltd0 / ta0) <= (ltd_y / ta_y))  # leverage fell/flat
        ca0, cl0 = v(bs, ["Current Assets"], 0), v(bs, ["Current Liabilities"], 0)
        ca_y, cl_y = v(bs, ["Current Assets"], yi), v(bs, ["Current Liabilities"], yi)
        if None not in (ca0, cl0, ca_y, cl_y) and cl0 and cl_y:
            tests["d_liquid"] = int((ca0 / cl0) > (ca_y / cl_y))
        sh0 = v(bs, ["Share Issued", "Ordinary Shares Number", "Common Stock"], 0)
        sh_y = v(bs, ["Share Issued", "Ordinary Shares Number", "Common Stock"], yi)
        if None not in (sh0, sh_y):
            tests["no_dilution"] = int(sh0 <= sh_y * 1.01)  # no meaningful new issuance
        # Efficiency
        rev0, rev_y = v(ci, ["Total Revenue"], 0), v(ci, ["Total Revenue"], yi)
        gp0, gp_y = v(ci, ["Gross Profit"], 0), v(ci, ["Gross Profit"], yi)
        if None not in (gp0, rev0, gp_y, rev_y) and rev0 and rev_y:
            tests["d_margin"] = int((gp0 / rev0) > (gp_y / rev_y))
        if None not in (rev0, ta0, rev_y, ta_y) and ta0 and ta_y:
            tests["d_turn"] = int((rev0 / ta0) > (rev_y / ta_y))

        n = len(tests)
        if n < 6:  # too sparse to be meaningful -> absent, not guessed
            return {}
        f = sum(tests.values())
        return {"f_score": f, "n_components": n,
                "score_norm": max(-1.0, min(1.0, (f / n - 0.5) * 2.0)),
                "tests": tests}
    except Exception:
        return {}


def get_revenue_series(symbol: str, asof: Optional[datetime] = None) -> list:
    """Quarterly revenue, newest-first, only quarters on/before asof (point-in-time)."""
    ci = _quarterly_income(symbol)
    if ci is None or ci.empty or "Total Revenue" not in ci.index:
        return []
    cols = list(ci.columns)
    if asof is not None:
        cols = [c for c in cols if pd.Timestamp(c).to_pydatetime().replace(tzinfo=None) <= asof]
    cols = sorted(cols, reverse=True)
    out = []
    for c in cols:
        try:
            v = ci.loc["Total Revenue"][c]
            out.append(None if pd.isna(v) else float(v))
        except Exception:
            out.append(None)
    return out


def get_fundamental_trend(symbol: str, asof: Optional[datetime] = None) -> Dict[str, Optional[float]]:
    """Revenue deceleration + operating-margin compression from quarterly financials
    (yfinance). Deteriorating fundamentals precede misses (the UAA pattern). Uses only
    quarters reported before `asof` (point-in-time). Returns {rev_growth, margin_delta,
    op_margin} or {}."""
    try:
        ci = _quarterly_income(symbol)
        if ci is None or ci.empty:
            return {}
        cols = list(ci.columns)
        if asof is not None:
            cols = [c for c in cols if pd.Timestamp(c).to_pydatetime().replace(tzinfo=None) <= asof]
        cols = sorted(cols, reverse=True)  # newest first
        if len(cols) < 3:
            return {}

        def row(name):
            return ci.loc[name] if name in ci.index else None
        rev, op = row("Total Revenue"), row("Operating Income")
        if rev is None:
            return {}

        def val(series, c):
            try:
                v = series[c]
                return None if pd.isna(v) else float(v)
            except Exception:
                return None
        revs = [val(rev, c) for c in cols]
        # Revenue growth: YoY (vs 4 quarters ago) if available, else QoQ.
        rev_growth = None
        if len(revs) >= 5 and revs[0] and revs[4]:
            rev_growth = revs[0] / revs[4] - 1
        elif revs[0] and revs[1]:
            rev_growth = revs[0] / revs[1] - 1
        # Operating margin trend: latest vs avg of prior 3.
        margin_delta = op_margin = None
        if op is not None:
            ops = [val(op, c) for c in cols]
            m = [(ops[i] / revs[i]) for i in range(min(4, len(cols)))
                 if ops[i] is not None and revs[i]]
            if m:
                op_margin = m[0]
                if len(m) >= 2:
                    margin_delta = m[0] - sum(m[1:]) / len(m[1:])
        return {"rev_growth": rev_growth, "margin_delta": margin_delta, "op_margin": op_margin}
    except Exception:
        return {}


@lru_cache(maxsize=256)
def get_sector(symbol: str) -> Optional[str]:
    """GICS sector string (yfinance). Used for per-sector base rates."""
    try:
        return yf.Ticker(symbol).info.get("sector")
    except Exception:
        return None


@lru_cache(maxsize=256)
def get_estimate_revisions(symbol: str) -> Dict[str, Optional[float]]:
    """REAL analyst EPS-estimate revisions for the current quarter (yfinance, free).

    eps_revisions gives up/down analyst counts (last 30d); eps_trend gives the
    estimate now vs 30/60/90d ago (magnitude of the revision). Net upward revision
    is one of the strongest pre-earnings predictors (Stickel 1991, Chan 1996).
    Returns {up, down, trend_pct} or {} if unavailable."""
    out: Dict[str, Optional[float]] = {}
    try:
        rev = yf.Ticker(symbol).eps_revisions
        if rev is not None and not rev.empty and "0q" in rev.index:
            r = rev.loc["0q"]
            up = float(r.get("upLast30days") or 0)
            dn = float(r.get("downLast30days") or 0)
            out["up"], out["down"] = up, dn
    except Exception:
        pass
    try:
        tr = yf.Ticker(symbol).eps_trend
        if tr is not None and not tr.empty and "0q" in tr.index:
            t = tr.loc["0q"]
            cur = float(t.get("current") or 0)
            past = next((float(t.get(k)) for k in ("30daysAgo", "60daysAgo", "90daysAgo")
                         if t.get(k) and float(t.get(k)) != 0), 0.0)
            if past:
                out["trend_pct"] = (cur - past) / abs(past) * 100.0
    except Exception:
        pass
    return out


@lru_cache(maxsize=256)
def get_short_interest(symbol: str) -> Optional[float]:
    """Short interest as % of float (yfinance). High % => squeeze potential on a
    beat / crowded bearish positioning. None if unavailable."""
    try:
        v = yf.Ticker(symbol).info.get("shortPercentOfFloat")
        return float(v) if v is not None else None
    except Exception:
        return None


def find_next_earnings_date(symbol: str) -> Dict[str, Optional[str]]:
    """Web-scan the next earnings date from free sources (yfinance calendar, then
    the future row of the earnings-dates table). Returns {date, source} or {}."""
    from datetime import date as _date
    # 1) yfinance calendar (dict in new yfinance, DataFrame in old)
    try:
        cal = yf.Ticker(symbol).calendar
        ed = None
        if isinstance(cal, dict):
            v = cal.get("Earnings Date")
            ed = (v[0] if isinstance(v, (list, tuple)) and v else v)
        elif cal is not None and hasattr(cal, "index") and "Earnings Date" in cal.index:
            ed = cal.loc["Earnings Date"][0]
        if isinstance(ed, (datetime, _date)):
            return {"date": str(ed)[:10], "source": "yfinance calendar"}
    except Exception:
        pass
    # 2) future (unreported) row in the earnings-dates table
    try:
        df = yf.Ticker(symbol).get_earnings_dates(limit=8)
        if df is not None and not df.empty:
            now = datetime.now()
            fut = [idx.to_pydatetime().replace(tzinfo=None) for idx, row in df.iterrows()
                   if pd.isna(row.get("Reported EPS")) and idx.to_pydatetime().replace(tzinfo=None) > now]
            if fut:
                return {"date": min(fut).date().isoformat(), "source": "yfinance earnings dates"}
    except Exception:
        pass
    return {}


@lru_cache(maxsize=1)
def get_vix() -> Dict[str, Optional[float]]:
    """VIX level + 1y percentile = market-volatility regime. High percentile =>
    more uncertainty => bigger EV haircut. {} on failure."""
    try:
        h = yf.Ticker("^VIX").history(period="1y")["Close"].dropna()
        if h.empty:
            return {}
        last = float(h.iloc[-1])
        pct = float((h < last).mean())
        return {"value": round(last, 2), "percentile": round(pct, 2)}
    except Exception:
        return {}


def post_earnings_return(symbol: str, announce: datetime,
                         bars: Optional[list] = None) -> Optional[float]:
    """Realized post-earnings stock move: close[day-1] -> close[day+2] around the
    announcement (captures the reaction + first drift days). Lets us BACKTEST the
    stock_up / Kelly-reversal model on free price data. None if not enough bars."""
    if bars is None:
        bars = get_price_bars(symbol, "5y")
    if not bars:
        return None
    i = next((k for k, b in enumerate(bars) if b["date"] >= announce), None)
    if i is None or i - 1 < 0 or i + 2 >= len(bars):
        return None
    base = bars[i - 1]["close"]
    end = bars[i + 2]["close"]
    return (end / base - 1.0) if base else None


def options_implied_move(symbol: str) -> Dict[str, Optional[float]]:
    """ATM implied move and put/call IV skew from the nearest expiration.

    Live only (yfinance has no options history). Returns:
      implied_move_pct: ATM straddle price / spot (approx expected move)
      skew: OTM put IV - OTM call IV (positive = downside fear priced in)
    Any field may be None if the chain is unavailable.
    """
    out: Dict[str, Optional[float]] = {"implied_move_pct": None, "skew": None}
    try:
        tk = yf.Ticker(symbol)
        exps = tk.options
        if not exps:
            return out
        chain = tk.option_chain(exps[0])
        calls, puts = chain.calls, chain.puts
        spot = tk.history(period="1d")["Close"].iloc[-1]
        if calls.empty or puts.empty or spot <= 0:
            return out
        # ATM straddle ~ implied move
        atm_call = calls.iloc[(calls["strike"] - spot).abs().argmin()]
        atm_put = puts.iloc[(puts["strike"] - spot).abs().argmin()]
        straddle = float(atm_call["lastPrice"]) + float(atm_put["lastPrice"])
        if straddle > 0:
            out["implied_move_pct"] = straddle / float(spot)
        # Skew: ~10% OTM put IV minus ~10% OTM call IV
        otm_put = puts.iloc[(puts["strike"] - spot * 0.9).abs().argmin()]
        otm_call = calls.iloc[(calls["strike"] - spot * 1.1).abs().argmin()]
        pv, cv = otm_put.get("impliedVolatility"), otm_call.get("impliedVolatility")
        if pv is not None and cv is not None and not (np.isnan(pv) or np.isnan(cv)):
            out["skew"] = float(pv) - float(cv)
    except Exception:
        return out
    return out

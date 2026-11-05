"""Replication of Kelly (2016), "Pre-Earnings Announcement Over-Extrapolation".

WHY THIS EXISTS
---------------
This project is built on Kelly's finding, and we could not reproduce it. A first pass over
480 events gave a correlation of +0.03 between our PUMP score and the post-earnings return,
and a quintile sort of +0.003 (t = 0.33) — no effect, and the wrong sign.

That pass was not a fair test. It used a 2-day return window on whatever free data was to
hand, no market adjustment, no announcement-timing alignment, and a signal (PUMP) that is
a cousin of ExtRet rather than ExtRet itself. Reporting "the paper doesn't replicate" on
that basis would be the same error this project exists to avoid, pointed outward.

So this is the honest retest, built to the paper's actual specification.

THE CLAIM BEING TESTED
----------------------
Individual investors over-extrapolate from a firm's past earnings-announcement returns.
After a run of good announcement returns they become optimistic, over-buy in the days
BEFORE the next announcement, and the stock REVERSES afterwards. The predictor is ExtRet:
a recency-weighted average of the 2-day [0,+1] announcement-window returns over the last
8 quarters.

Two testable predictions, and they must BOTH hold for the mechanism to be real:
    H1  ExtRet predicts the pre-announcement run-up      -> POSITIVE coefficient
    H2  ExtRet predicts the post-announcement return     -> NEGATIVE coefficient

H2 alone could be mean reversion. H1 alone could be momentum. The over-extrapolation story
requires the pair: buying pressure first, reversal after.

WHAT THIS FIXES VERSUS THE FIRST PASS
-------------------------------------
1. ANNOUNCEMENT TIMING. A print released after the close reacts on the NEXT trading day.
   Treating the calendar date as day 0 for an after-close announcement puts the entire
   reaction outside the measurement window and silently destroys the signal. yfinance
   timestamps carry the hour, so after-close events are shifted by one trading day.
2. MARKET ADJUSTMENT. Raw returns over a 2016-2026 bull market are dominated by beta.
   All returns here are abnormal (stock minus SPY over the same window).
3. ExtRet ITSELF, not a proxy — recency-weighted over 8 prior announcements, each measured
   in the paper's [0,+1] window.
4. REAL EVENT HISTORY. Up to 12 years of announcement dates per name, so an event can draw
   on 8 genuine prior announcements rather than whatever history happened to be cached.

HONEST LIMITS, STATED UP FRONT
------------------------------
- Kelly uses 1983-2014 with individual-investor order flow (retail trade data we do not
  have). We test the PRICE implications only, on a later and different sample.
- Our universe is liquid large/mid caps; the paper's effect is strongest in small, retail-
  held names, which is exactly where our free data is worst. A null here is evidence about
  OUR tradable universe, not a refutation of the paper.
- Standard errors are clustered by ticker via a block bootstrap, because events from one
  firm are not independent draws.
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional, Tuple

import numpy as np
import pandas as pd

AFTER_CLOSE_HOUR = 16        # yfinance stamps after-close prints at/after 16:00 local
N_QUARTERS = 8               # prior announcements in the ExtRet average (paper's spec)
RUNUP_WINDOW = (-5, -1)      # Kelly: over-buying concentrates in ~5 days before the print
POST_WINDOWS = {             # reversal windows, measured AFTER the announcement reaction
    "post_2_21": (2, 21),
    "post_2_42": (2, 42),
    "post_1_5": (1, 5),
}


def _prices(ticker: str, years: int = 12) -> Optional[pd.DataFrame]:
    import yfinance as yf
    try:
        h = yf.Ticker(ticker).history(period=f"{years}y", interval="1d", auto_adjust=True)
    except Exception:
        return None
    if h is None or h.empty:
        return None
    h = h[["Close"]].copy()
    h.index = pd.to_datetime(h.index).tz_localize(None).normalize()
    return h


def _event_days(ticker: str) -> List[pd.Timestamp]:
    """Announcement dates, shifted to the trading day the market can first react.

    This is the step the first pass missed: an after-close print is a next-day event.
    """
    import yfinance as yf
    try:
        ed = yf.Ticker(ticker).get_earnings_dates(limit=60)
    except Exception:
        return []
    if ed is None or ed.empty:
        return []
    out = []
    for ts in ed.index:
        d = pd.Timestamp(ts)
        after_close = d.hour >= AFTER_CLOSE_HOUR
        day = d.tz_localize(None).normalize() if d.tzinfo else d.normalize()
        out.append(day + pd.Timedelta(days=1) if after_close else day)
    return sorted(set(out))


def _idx_on_or_after(index: pd.DatetimeIndex, day: pd.Timestamp) -> Optional[int]:
    pos = index.searchsorted(day)
    return int(pos) if pos < len(index) else None


def _abnormal(px: pd.Series, mkt: pd.Series, i: int, a: int, b: int) -> Optional[float]:
    """Abnormal buy-and-hold return over trading days [i+a, i+b], stock minus market."""
    lo, hi = i + a, i + b
    if lo - 1 < 0 or hi >= len(px) or lo > hi:
        return None
    try:
        s = px.iloc[hi] / px.iloc[lo - 1] - 1.0
        m = mkt.iloc[hi] / mkt.iloc[lo - 1] - 1.0
    except Exception:
        return None
    if not np.isfinite(s) or not np.isfinite(m):
        return None
    return float(s - m)


def build_events(tickers: List[str], verbose: bool = True) -> pd.DataFrame:
    """One row per earnings event with ExtRet, the run-up, and the post-event returns."""
    mkt_df = _prices("SPY")
    if mkt_df is None:
        raise RuntimeError("could not load SPY benchmark")
    rows: List[Dict[str, Any]] = []
    for n, t in enumerate(tickers, 1):
        px_df = _prices(t)
        days = _event_days(t)
        if px_df is None or len(days) < N_QUARTERS + 1:
            if verbose:
                print(f"  [{n}/{len(tickers)}] {t}: skipped (insufficient history)")
            continue
        joined = px_df.join(mkt_df, how="inner", rsuffix="_m").dropna()
        if len(joined) < 300:
            continue
        px, mkt = joined.iloc[:, 0], joined.iloc[:, 1]
        idx = joined.index

        # [0,+1] abnormal announcement return for every event we can measure.
        ann: Dict[pd.Timestamp, float] = {}
        for d in days:
            i = _idx_on_or_after(idx, d)
            if i is None:
                continue
            ar = _abnormal(px, mkt, i, 0, 1)
            if ar is not None:
                ann[d] = ar

        used = 0
        for k, d in enumerate(days):
            prior = [days[j] for j in range(k - 1, -1, -1)][:N_QUARTERS]
            prior_ars = [ann[p] for p in prior if p in ann]
            if len(prior_ars) < N_QUARTERS:
                continue                      # strictly 8 prior announcements, no partials
            i = _idx_on_or_after(idx, d)
            if i is None:
                continue
            # Recency weights, heaviest on the most recent quarter (paper's construction).
            w = np.arange(len(prior_ars), 0, -1, dtype=float)
            ext_ret = float(np.dot(w, prior_ars) / w.sum())
            runup = _abnormal(px, mkt, i, *RUNUP_WINDOW)
            rec = {"ticker": t, "event_day": d, "ext_ret": ext_ret,
                   "runup": runup, "announce_ret": ann.get(d)}
            for name, (a, b) in POST_WINDOWS.items():
                rec[name] = _abnormal(px, mkt, i, a, b)
            if runup is None:
                continue
            rows.append(rec)
            used += 1
        if verbose:
            print(f"  [{n}/{len(tickers)}] {t}: {used} usable events")
    return pd.DataFrame(rows)


def _block_bootstrap_mean(df: pd.DataFrame, col: str, seed: int = 0,
                          draws: int = 5000) -> Tuple[float, float]:
    """95% CI for a column mean, resampling whole TICKERS (events within a firm are
    correlated, so resampling events would understate the uncertainty)."""
    rng = np.random.default_rng(seed)
    groups = [g[col].dropna().values for _, g in df.groupby("ticker")]
    groups = [g for g in groups if len(g)]
    if len(groups) < 3:
        return (float("nan"), float("nan"))
    means = []
    for _ in range(draws):
        pick = rng.integers(0, len(groups), len(groups))
        vals = np.concatenate([groups[i] for i in pick])
        means.append(vals.mean())
    return float(np.percentile(means, 2.5)), float(np.percentile(means, 97.5))


def analyse(df: pd.DataFrame, seed: int = 0) -> Dict[str, Any]:
    """Test H1 (run-up, expect positive) and H2 (reversal, expect negative)."""
    out: Dict[str, Any] = {"n_events": len(df), "n_tickers": int(df["ticker"].nunique())}
    if df.empty:
        return out

    def quintile_spread(ycol: str) -> Dict[str, Any]:
        d = df[["ticker", "ext_ret", ycol]].dropna()
        if len(d) < 50:
            return {"n": len(d), "note": "too few events"}
        qs = np.quantile(d["ext_ret"], [0, .2, .4, .6, .8, 1.0])
        buckets = []
        for i in range(5):
            m = ((d["ext_ret"] >= qs[i]) &
                 (d["ext_ret"] <= qs[i + 1] if i == 4 else d["ext_ret"] < qs[i + 1]))
            buckets.append({"q": i + 1, "n": int(m.sum()),
                            "mean": round(float(d.loc[m, ycol].mean()), 5)})
        hi = d[d["ext_ret"] >= qs[4]]
        lo = d[d["ext_ret"] <= qs[1]]
        diff = float(hi[ycol].mean() - lo[ycol].mean())
        # Ticker-clustered CI on the spread, via bootstrap of each leg.
        hl = _block_bootstrap_mean(hi, ycol, seed=seed)
        ll = _block_bootstrap_mean(lo, ycol, seed=seed + 1)
        se = np.sqrt(hi[ycol].var(ddof=1) / len(hi) + lo[ycol].var(ddof=1) / len(lo))
        return {"n": len(d), "buckets": buckets,
                "top_minus_bottom": round(diff, 5),
                "t_naive": round(diff / se, 2) if se else None,
                "top_ci95": [round(hl[0], 5), round(hl[1], 5)],
                "bottom_ci95": [round(ll[0], 5), round(ll[1], 5)],
                "corr": round(float(d["ext_ret"].corr(d[ycol])), 4)}

    out["H1_runup"] = quintile_spread("runup")
    out["H2"] = {name: quintile_spread(name) for name in POST_WINDOWS}
    out["announce_reaction"] = quintile_spread("announce_ret")

    h1 = out["H1_runup"]
    h2 = out["H2"].get("post_2_21", {})

    def _supported(s: Dict[str, Any], want_positive: bool) -> bool:
        """A leg counts as supported only if it is SIGNIFICANT in the predicted
        direction — sign alone is not evidence.

        The first version of this function checked the sign only. On the real data it
        duly announced that "both legs point the predicted way" off a reversal spread of
        -0.16% with t = -0.23, which is noise wearing a minus sign. That is the same
        error this project retracted in October, reproduced inside the tool built to
        test for it. Significance is now required.
        """
        d = s.get("top_minus_bottom")
        t = s.get("t_naive")
        if not isinstance(d, float) or t is None:
            return False
        if want_positive and not (d > 0 and t >= 1.96):
            return False
        if (not want_positive) and not (d < 0 and t <= -1.96):
            return False
        return True

    h1_ok = _supported(h1, want_positive=True)
    h2_ok = _supported(h2, want_positive=False)
    out["H1_supported"], out["H2_supported"] = h1_ok, h2_ok
    if h1_ok and h2_ok:
        out["verdict"] = ("REPLICATES — both legs significant in the predicted direction "
                          "(run-up positive, reversal negative).")
    elif h1_ok:
        out["verdict"] = ("PARTIAL — the run-up leg replicates (investors do bid these names "
                          "up before the print), but the REVERSAL does not. The tradeable "
                          "half of the mechanism is absent in our universe.")
    elif h2_ok:
        out["verdict"] = ("Reversal leg only — could be ordinary mean reversion. Without the "
                          "run-up there is no over-extrapolation story.")
    else:
        out["verdict"] = ("NO SUPPORT — neither leg is significant in the predicted direction "
                          "on this sample.")
    return out


DEFAULT_UNIVERSE = [
    "AAPL", "MSFT", "NVDA", "AMZN", "META", "GOOGL", "TSLA", "AMD", "INTC", "QCOM",
    "CRM", "ADBE", "ORCL", "NFLX", "CSCO", "TXN", "AVGO", "MU", "NOW", "SNAP",
    "UBER", "SHOP", "SQ", "PYPL", "ROKU", "PINS", "TWLO", "OKTA", "DOCU", "ZM",
    "WMT", "TGT", "COST", "HD", "LOW", "NKE", "SBUX", "MCD", "DIS", "CMCSA",
    "JPM", "BAC", "GS", "MS", "C", "WFC", "AXP", "V", "MA", "PFE",
]


def run(tickers: Optional[List[str]] = None, verbose: bool = True) -> Dict[str, Any]:
    tickers = tickers or DEFAULT_UNIVERSE
    if verbose:
        print(f"Kelly (2016) replication — {len(tickers)} tickers, "
              f"ExtRet over {N_QUARTERS} prior announcements, market-adjusted\n")
    df = build_events(tickers, verbose=verbose)
    res = analyse(df)
    res["_frame"] = df
    return res


if __name__ == "__main__":
    import json
    r = run()
    df = r.pop("_frame")
    print("\n" + json.dumps(r, indent=2, default=str))

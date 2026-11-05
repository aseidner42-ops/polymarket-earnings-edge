"""Backtest + calibrate the fundamental beat model (v1.1).

Reconstructs point-in-time signals before each past EPS print, scores the
fundamental beat probability, and compares predictions to what actually
happened across a diverse basket (beaters, missers, volatile names, multiple
sectors). Then it derives data-driven weight recommendations and calibrates the
logistic (intercept/gain) to minimize the Brier score.

Honest limitations (surfaced in the report, not hidden):
  * Free data has NO historical revenue consensus -> revenue-beat accuracy
    cannot be validated. Only EPS-beat is testable.
  * Finnhub gives fiscal-period-end, not announcement date -> announcement
    approximated as period-end + 40 days for the price-window as-of.
  * analyst_revision, news_sentiment, and options signals have no free history
    -> absent in backtest; their weights are left untested (kept as-is).
"""
from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional, Tuple

import numpy as np

from tools import edgar, finnhub_client as fh, market_data as md, signals
from tools.config import OUTPUT_DIR, load_weights
from tools.scoring import FUND_SPREAD, FUNDAMENTAL, _logistic, _weighted_norm

# Signals that actually have free history available in the backtest.
BACKTEST_SIGNALS = ["sue_momentum", "surprise_streak", "pre_earnings_drift", "rel_strength"]

# Diverse basket: beaters, missers, volatile names, across sectors.
DEFAULT_BASKET: Dict[str, List[str]] = {
    "large_cap_tech": ["AAPL", "MSFT", "GOOGL", "AMZN", "NVDA", "META"],
    "consumer": ["KO", "PG", "MCD", "NKE", "SBUX"],
    "financials": ["JPM", "BAC", "GS", "WFC"],
    "industrials": ["CAT", "BA", "GE", "HON"],
    "healthcare": ["JNJ", "PFE", "UNH", "MRK"],
    "high_momentum_growth": ["TSLA", "NFLX", "SHOP", "UBER", "PLTR"],
    "volatile_missers": ["SNAP", "RIVN", "ROKU", "BYND", "PINS"],
}


def all_basket_tickers() -> List[str]:
    return [t for group in DEFAULT_BASKET.values() for t in group]


def _category_of(symbol: str) -> str:
    for cat, tks in DEFAULT_BASKET.items():
        if symbol in tks:
            return cat
    return "other"


def _asof_for_period(period: str) -> datetime:
    return datetime.strptime(period, "%Y-%m-%d") + timedelta(days=40)


def _run_one_real(symbol: str, eh: List[Dict[str, Any]], max_quarters: int) -> List[Dict[str, Any]]:
    """Backtest using yfinance REAL announcement dates + surprise% (precise as-of)."""
    w = load_weights()["weights"]
    out: List[Dict[str, Any]] = []
    for i, ev in enumerate(eh[:max_quarters]):
        asof = ev["date"]
        if asof is None or asof > datetime.now():
            continue
        prior = [{"surprisePercent": e["surprise_pct"],
                  "surprise": (None if e["eps_estimate"] is None else e["eps_actual"] - e["eps_estimate"]),
                  "period": e["date"].date().isoformat()} for e in eh[i + 1:]]  # older only
        from tools import pump as _pump
        _pp = _pump.pump_pressure(symbol, asof=asof)
        data = {
            "surprises": prior, "recommendations": [],
            "pre_earnings_return": md.pre_earnings_return(symbol, asof=asof),
            "rel_strength": md.relative_strength(symbol, asof=asof),
            "volume_buildup": md.volume_buildup(symbol, asof=asof),
            "options": {}, "news": [],
            "fundamental_trend": md.get_fundamental_trend(symbol, asof=asof),
            "piotroski": md.get_piotroski(symbol, asof=asof),
            "filing_risk": edgar.filing_risk(symbol, asof=asof.date().isoformat()),
            "pump_score": _pp.get("pump"),  # PUMP as a beat signal (point-in-time)
        }
        sigs = signals.compute_all(data)
        scores = {n: sigs[n].score for n in FUNDAMENTAL if n in sigs and sigs[n].present}
        norm, n_present = _weighted_norm(sigs, FUNDAMENTAL, w)
        # SCAR predictability per event (point-in-time) -> lets us isolate confident calls.
        from tools.scar import scar_rating
        from tools import surprise_model
        _spp = [e["surprisePercent"] for e in prior if e.get("surprisePercent") is not None]
        _fp = surprise_model.fingerprint(_spp)
        _log = load_weights()["logistic"]
        _base = _logistic(_log["intercept"] + _log["gain"] * FUND_SPREAD * norm)
        _scar = scar_rating(sigs, md.get_revenue_series(symbol, asof=asof),
                            _fp["mu"] if _fp else None, w, FUNDAMENTAL,
                            fingerprint_consistency=_fp["consistency"] if _fp else None,
                            model_prob=_base,
                            fingerprint_sigma=_fp["sigma"] if _fp else None)
        sp = ev.get("surprise_pct")
        actual_beat = (sp if sp is not None else
                       ((ev["eps_actual"] - (ev["eps_estimate"] or 0)))) > 0
        # _pp (PUMP) already computed above (fed into the pump signal); reuse it. Also grab the
        # realized post-earnings move so PUMP's predictive value stays measurable on history.
        _post = md.post_earnings_return(symbol, ev["date"])
        out.append({"symbol": symbol, "category": _category_of(symbol),
                    "period": ev["date"].date().isoformat(), "norm": norm,
                    "n_signals": n_present, "scores": scores, "actual_beat": bool(actual_beat),
                    "eps_estimate": ev.get("eps_estimate"), "eps_actual": ev.get("eps_actual"),
                    "surprise_pct": ev.get("surprise_pct"),
                    "predictability": _scar.get("predictability"),
                    "fp_mu": (_fp["mu"] if _fp else None),
                    "fp_sigma": (_fp["sigma"] if _fp else None),
                    "fp_consistency": (_fp["consistency"] if _fp else None),
                    "coherence": _scar.get("coherence"),
                    "pump": _pp.get("pump"), "post_earnings_return": _post})
    return out


def _run_one(symbol: str, max_quarters: int) -> List[Dict[str, Any]]:
    """Reconstruct predictions for one ticker's past quarters (real dates if available)."""
    eh = md.get_earnings_history(symbol)
    if eh:
        return _run_one_real(symbol, list(eh), max_quarters)
    surprises = fh.get_earnings_surprises(symbol, limit=max_quarters + 6)
    w = load_weights()["weights"]
    out: List[Dict[str, Any]] = []
    for i, ev in enumerate(surprises[:max_quarters]):
        if ev.get("actual") is None or ev.get("estimate") is None or not ev.get("period"):
            continue
        try:
            asof = _asof_for_period(ev["period"])
        except ValueError:
            continue
        if asof > datetime.now():
            continue
        prior = surprises[i + 1:]
        data = {
            "surprises": prior,
            "recommendations": [],
            "pre_earnings_return": md.pre_earnings_return(symbol, asof=asof),
            "rel_strength": md.relative_strength(symbol, asof=asof),
            "volume_buildup": md.volume_buildup(symbol, asof=asof),
            "options": {},
            "news": [],
        }
        sigs = signals.compute_all(data)
        scores = {n: sigs[n].score for n in FUNDAMENTAL if n in sigs and sigs[n].present}
        norm, n_present = _weighted_norm(sigs, FUNDAMENTAL, w)
        actual_beat = (ev.get("surprise") or (ev["actual"] - ev["estimate"])) > 0
        out.append({
            "symbol": symbol, "category": _category_of(symbol), "period": ev["period"],
            "norm": norm, "n_signals": n_present, "scores": scores,
            "actual_beat": bool(actual_beat),
            "eps_estimate": ev.get("estimate"), "eps_actual": ev.get("actual"),
            "surprise_pct": ev.get("surprisePercent"),
        })
    return out


# --- probability helpers ----------------------------------------------------
def _norm_from(scores: Dict[str, float], weights: Dict[str, float]) -> float:
    # De-correlate: damp each signal by 1/sqrt(#present in its group), matching live scoring.
    from tools.scoring import SIGNAL_GROUPS
    gcount: Dict[str, int] = {}
    for name in scores:
        g = SIGNAL_GROUPS.get(name, name)
        gcount[g] = gcount.get(g, 0) + 1
    num = den = 0.0
    for name, s in scores.items():
        wt = abs(weights.get(name, 0.0)) / np.sqrt(gcount.get(SIGNAL_GROUPS.get(name, name), 1))
        num += wt * s
        den += wt
    return num / den if den > 0 else 0.0


def _prob(scores: Dict[str, float], weights: Dict[str, float],
          intercept: float, gain: float) -> float:
    return _logistic(intercept + gain * FUND_SPREAD * _norm_from(scores, weights))


def _pearson(xs: List[float], ys: List[float]) -> float:
    if len(xs) < 3:
        return 0.0
    a, b = np.array(xs, dtype=float), np.array(ys, dtype=float)
    if a.std() == 0 or b.std() == 0:
        return 0.0
    return float(np.corrcoef(a, b)[0, 1])


# --- analysis ---------------------------------------------------------------
def _metrics(probs: List[float], rows: List[Dict[str, Any]]) -> Dict[str, Any]:
    n = len(rows)
    if n == 0:
        return {"n": 0, "note": "no usable historical quarters"}
    actual = [r["actual_beat"] for r in rows]
    pred = [p >= 0.5 for p in probs]
    tp = sum(1 for p, a in zip(pred, actual) if p and a)
    fp = sum(1 for p, a in zip(pred, actual) if p and not a)
    tn = sum(1 for p, a in zip(pred, actual) if not p and not a)
    fn = sum(1 for p, a in zip(pred, actual) if not p and a)
    acc = (tp + tn) / n
    prec = tp / (tp + fp) if (tp + fp) else 0.0
    rec = tp / (tp + fn) if (tp + fn) else 0.0
    f1 = 2 * prec * rec / (prec + rec) if (prec + rec) else 0.0
    base_rate = sum(actual) / n
    brier = float(np.mean([(p - a) ** 2 for p, a in zip(probs, actual)]))
    brier_baseline = float(np.mean([(base_rate - a) ** 2 for a in actual]))
    return {
        "n": n, "accuracy": round(acc, 3), "precision": round(prec, 3),
        "recall": round(rec, 3), "f1": round(f1, 3),
        "false_positives": fp, "false_negatives": fn,
        "confusion": {"tp": tp, "fp": fp, "tn": tn, "fn": fn},
        "brier": round(brier, 4), "brier_baseline": round(brier_baseline, 4),
        "baseline_always_beat_accuracy": round(base_rate, 3),
        "beats_baseline_accuracy": acc > base_rate,
        "beats_baseline_brier": brier < brier_baseline,
    }


def _signal_analysis(rows: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Per-signal correlation with the actual beat outcome + directional hit rate."""
    out = []
    for name in FUNDAMENTAL:
        xs, ys, hits, used = [], [], 0, 0
        for r in rows:
            if name in r["scores"]:
                s = r["scores"][name]
                xs.append(s)
                ys.append(1.0 if r["actual_beat"] else 0.0)
                if s != 0:
                    used += 1
                    hits += 1 if (s > 0) == r["actual_beat"] else 0
        if not xs:
            out.append({"signal": name, "n_present": 0, "corr": None,
                        "hit_rate": None, "verdict": "absent (no free history)"})
            continue
        corr = round(_pearson(xs, ys), 3)
        hit = round(hits / used, 3) if used else None
        verdict = ("helps" if corr > 0.05 else
                   "noise" if -0.05 <= corr <= 0.05 else "hurts")
        out.append({"signal": name, "n_present": len(xs), "corr": corr,
                    "hit_rate": hit, "verdict": verdict})
    return out


def _recommend_weights(sig_analysis: List[Dict[str, Any]],
                       current: Dict[str, float]) -> Dict[str, float]:
    """Data-driven weights for tested signals: magnitude ∝ positive correlation,
    scaled so the strongest tested signal keeps the current top weight. Signals
    with corr<=0 are dropped to 0 (noise/hurts). Untested signals keep current."""
    tested = {a["signal"]: a["corr"] for a in sig_analysis
              if a["corr"] is not None and a["signal"] in BACKTEST_SIGNALS}
    pos = {k: v for k, v in tested.items() if v > 0}
    rec = dict(current)  # start from current; only overwrite tested signals
    top_scale = max(current.get(k, 0.0) for k in BACKTEST_SIGNALS) or 1.2
    max_corr = max(pos.values()) if pos else 1.0
    for name in BACKTEST_SIGNALS:
        corr = tested.get(name)
        if corr is None:
            continue
        rec[name] = round((corr / max_corr) * top_scale, 2) if corr > 0 else 0.0
    return rec


def _calibrate_logistic(rows: List[Dict[str, Any]], rec_weights: Dict[str, float]
                        ) -> Dict[str, Any]:
    """Grid-search intercept/gain to minimize Brier under the recommended weights."""
    actual = [1.0 if r["actual_beat"] else 0.0 for r in rows]
    best = None
    for intercept in np.linspace(-0.5, 1.0, 16):
        for gain in np.linspace(0.3, 2.5, 12):
            probs = [_prob(r["scores"], rec_weights, intercept, gain) for r in rows]
            brier = float(np.mean([(p - a) ** 2 for p, a in zip(probs, actual)]))
            if best is None or brier < best["brier"]:
                best = {"intercept": round(float(intercept), 3),
                        "gain": round(float(gain), 3), "brier": round(brier, 4)}
    return best or {"intercept": 0.3, "gain": 0.9, "brier": None}


def _pava(ys: List[float]) -> List[float]:
    """Pool-Adjacent-Violators: fit a monotonic non-decreasing curve to ys (already
    ordered by ascending predicted prob). Returns fitted values, same length/order."""
    vals: List[float] = []
    wts: List[float] = []
    cnts: List[int] = []
    for y in ys:
        v, w, c = float(y), 1.0, 1
        while vals and vals[-1] > v:
            pv, pw, pc = vals.pop(), wts.pop(), cnts.pop()
            v = (v * w + pv * pw) / (w + pw)
            w += pw
            c += pc
        vals.append(v); wts.append(w); cnts.append(c)
    out: List[float] = []
    for v, c in zip(vals, cnts):
        out.extend([v] * c)
    return out


def _reliability(probs: List[float], actuals: List[float], nbins: int = 10) -> List[Dict[str, Any]]:
    """Reliability diagram bins: predicted-prob bucket -> mean predicted vs realized."""
    bins = []
    for b in range(nbins):
        lo, hi = b / nbins, (b + 1) / nbins
        idx = [i for i, p in enumerate(probs) if (p >= lo and (p < hi or (b == nbins - 1 and p <= hi)))]
        if not idx:
            continue
        bins.append({
            "bin": f"{int(lo*100)}-{int(hi*100)}%",
            "predicted": round(sum(probs[i] for i in idx) / len(idx), 3),
            "realized": round(sum(actuals[i] for i in idx) / len(idx), 3),
            "n": len(idx),
        })
    return bins


def _isotonic_map(train_x: List[float], train_y: List[float]):
    """Fit isotonic calibration on train, return a function mapping prob -> calibrated prob."""
    if len(train_x) < 3:
        return lambda p: p
    order = np.argsort(train_x)
    xs = np.array(train_x)[order]
    fitted = np.array(_pava([train_y[i] for i in order]))

    def cal(p: float) -> float:
        j = int(np.searchsorted(xs, p, side="right")) - 1
        j = max(0, min(len(fitted) - 1, j))
        return float(fitted[j])
    return cal


def _walk_forward(rows: List[Dict[str, Any]]) -> Dict[str, Any]:
    """Chronological out-of-sample test: fit isotonic calibration on the earliest
    70% of events, evaluate on the most recent 30%. Exposes overfit (in-sample vs OOS)."""
    ev = [(r.get("period") or "", r.get("prob"), 1.0 if r.get("actual_beat") else 0.0)
          for r in rows if r.get("prob") is not None]
    ev.sort(key=lambda e: e[0])
    if len(ev) < 12:
        return {"note": f"need >=12 events for walk-forward (have {len(ev)})"}
    split = int(len(ev) * 0.7)
    train, test = ev[:split], ev[split:]
    cal = _isotonic_map([e[1] for e in train], [e[2] for e in train])
    is_brier = float(np.mean([(e[1] - e[2]) ** 2 for e in train]))
    oos_raw = float(np.mean([(e[1] - e[2]) ** 2 for e in test]))
    oos_cal = float(np.mean([(cal(e[1]) - e[2]) ** 2 for e in test]))
    oos_acc = float(np.mean([(e[1] >= 0.5) == (e[2] == 1) for e in test]))
    return {
        "train_n": len(train), "test_n": len(test),
        "in_sample_brier": round(is_brier, 4),
        "oos_brier_raw": round(oos_raw, 4),
        "oos_brier_calibrated": round(oos_cal, 4),
        "oos_accuracy": round(oos_acc, 3),
        "calibration_helps_oos": oos_cal < oos_raw,
    }


def _fit_logistic(rows: List[Dict[str, Any]], feature_names: List[str],
                  l2: float = 1.0, iters: int = 600, lr: float = 0.3) -> Optional[Dict[str, Any]]:
    """#3/#4: L2-regularized logistic regression on z-scored signal features
    (absent signal -> 0). Trains on resolved historical events. Returns the fitted
    coefficients + standardization, or None if too few events."""
    X, y = [], []
    for r in rows:
        sc = r.get("scores") or {}
        X.append([sc.get(f, 0.0) for f in feature_names])
        y.append(1.0 if r.get("actual_beat") else 0.0)
    X, y = np.array(X, float), np.array(y, float)
    if len(y) < 20 or X.shape[1] == 0:
        return None
    # Defense in depth: a signal can come back None/NaN/inf (missing free data), and
    # np.array() turns those into NaN, which poisons mu/sd and then every matmul in the
    # gradient loop — the fitted weights would silently become NaN. Treat a non-finite
    # signal as "absent" (0.0), the same convention as a missing key.
    # NOTE (2026-11-03): this is NOT the cause of the matmul RuntimeWarnings in the daily
    # log. Those are spurious — numpy 2.0 on Apple Accelerate leaks FP status flags, so
    # even np.ones((500,9)) @ np.zeros(9) raises divide-by-zero/overflow/invalid. They are
    # suppressed at the loop below; the data here was already clean.
    X = np.nan_to_num(X, nan=0.0, posinf=0.0, neginf=0.0)
    mu, sd = X.mean(0), X.std(0)
    sd[~np.isfinite(sd) | (sd == 0)] = 1.0
    mu = np.nan_to_num(mu, nan=0.0, posinf=0.0, neginf=0.0)
    Xs = np.hstack([np.ones((len(X), 1)), (X - mu) / sd])
    w = np.zeros(Xs.shape[1])
    # errstate: silence the spurious Accelerate matmul flags (see note above) so a real
    # numerical problem is visible instead of being buried in thousands of fake warnings.
    with np.errstate(divide="ignore", over="ignore", invalid="ignore"):
        for _ in range(iters):
            p = 1.0 / (1.0 + np.exp(-np.clip(Xs @ w, -30, 30)))
            g = Xs.T @ (p - y) / len(y)
            g[1:] += l2 * w[1:] / len(y)
            w -= lr * g
    if not np.all(np.isfinite(w)):      # refuse to return a NaN model rather than ship one
        return None
    return {"features": feature_names, "mu": mu.tolist(), "sd": sd.tolist(),
            "coef": w[1:].tolist(), "intercept": float(w[0])}


def _logistic_prob(model: Dict[str, Any], scores: Dict[str, float]) -> float:
    mu, sd, coef = np.array(model["mu"]), np.array(model["sd"]), np.array(model["coef"])
    x = np.array([scores.get(f, 0.0) for f in model["features"]], float)
    z = (x - mu) / sd
    return float(1.0 / (1.0 + np.exp(-np.clip(model["intercept"] + z @ coef, -30, 30))))


def _fitted_evaluation(rows: List[Dict[str, Any]], feature_names: List[str]) -> Dict[str, Any]:
    """Fit on earliest 70%, test on most recent 30% (walk-forward) + report coefficients."""
    full = _fit_logistic(rows, feature_names)
    if not full:
        return {"note": f"need >=20 events to fit a model (have {len(rows)})"}
    ev = sorted([r for r in rows if r.get("scores")], key=lambda r: r.get("period") or "")
    split = int(len(ev) * 0.7)
    train, test = ev[:split], ev[split:]
    m = _fit_logistic(train, feature_names)
    out = {"coefficients": dict(zip(feature_names, [round(c, 3) for c in full["coef"]])),
           "intercept": round(full["intercept"], 3)}
    if m and test:
        y = np.array([1.0 if r["actual_beat"] else 0.0 for r in test])
        p = np.array([_logistic_prob(m, r["scores"]) for r in test])
        out["oos_accuracy"] = round(float(np.mean((p >= 0.5) == (y == 1))), 3)
        out["oos_brier"] = round(float(np.mean((p - y) ** 2)), 4)
        out["test_n"] = len(test)
    return out


def diagnose(row: Dict[str, Any]) -> str:
    """For a mismatch, name the signal that most misled the model + a concrete fix.
    Empty string when the prediction matched."""
    if row.get("pred_beat") == row.get("actual_beat"):
        return ""
    scores = row.get("scores") or {}
    if not scores:
        return "No signals had data — too little information to predict; needs more coverage."
    if row["pred_beat"] and not row["actual_beat"]:
        # Predicted beat, actually missed -> a bullish signal over-extrapolated.
        name, val = max(scores.items(), key=lambda kv: kv[1])
        return (f"Predicted BEAT, missed. Most bullish lean: {name} (+{val:.2f}). "
                f"Likely over-extrapolated recent strength (Kelly) — consider trimming "
                f"{name}'s weight or strengthening the over-extrapolation reversal.")
    # Predicted miss, actually beat -> a bearish signal dominated.
    name, val = min(scores.items(), key=lambda kv: kv[1])
    return (f"Predicted MISS, beat. Most bearish lean: {name} ({val:.2f}). "
            f"Model under-weighted the beat — consider down-weighting {name} or "
            f"raising the base-rate intercept (companies beat ~65% of the time).")


def backtest(tickers: Optional[List[str]] = None, max_quarters: int = 8,
             since: Optional[str] = None, until: Optional[str] = None) -> Dict[str, Any]:
    """Run the full validation + calibration. tickers=None -> diverse default basket.

    since/until (ISO 'YYYY-MM-DD') restrict to earnings events in [since, until] —
    used by the per-event window to evaluate the model over a chosen date range."""
    tickers = tickers or all_basket_tickers()
    cfg = load_weights()
    cur_w, cur_log = cfg["weights"], cfg["logistic"]

    # Tickers in a backtest are already clean symbols; skip the search call.
    mq = 24 if (since or until) else max_quarters  # go deep when a date window is set
    rows: List[Dict[str, Any]] = []
    for t in tickers:
        rows.extend(_run_one(t.strip().upper(), mq))
    if since:
        rows = [r for r in rows if (r.get("period") or "") >= since]
    if until:
        rows = [r for r in rows if (r.get("period") or "") <= until]

    # Current-model probabilities and metrics; attach per-event prob for display.
    cur_probs = [_prob(r["scores"], cur_w, cur_log["intercept"], cur_log["gain"]) for r in rows]
    for r, p in zip(rows, cur_probs):
        r["prob"] = round(p, 3)
        r["pred_beat"] = p >= 0.5
    metrics_current = _metrics(cur_probs, rows)

    sig_analysis = _signal_analysis(rows)
    rec_weights = _recommend_weights(sig_analysis, cur_w)
    calib = _calibrate_logistic(rows, rec_weights) if rows else {}

    # Calibrated-model metrics.
    if rows and calib:
        new_probs = [_prob(r["scores"], rec_weights, calib["intercept"], calib["gain"]) for r in rows]
        metrics_calibrated = _metrics(new_probs, rows)
    else:
        metrics_calibrated = {"n": 0}

    coverage: Dict[str, int] = {}
    for r in rows:
        coverage[r["category"]] = coverage.get(r["category"], 0) + 1

    # Calibration: reliability diagram + in-sample isotonic Brier (best achievable).
    actuals = [1.0 if r["actual_beat"] else 0.0 for r in rows]
    reliability = _reliability(cur_probs, actuals) if rows else []
    iso_brier = None
    if len(rows) >= 3:
        cal = _isotonic_map(cur_probs, actuals)
        iso_brier = round(float(np.mean([(cal(p) - a) ** 2 for p, a in zip(cur_probs, actuals)])), 4)

    return {
        "tickers": tickers,
        "coverage_by_category": coverage,
        "metrics_current": metrics_current,
        "metrics_calibrated": metrics_calibrated,
        "signal_analysis": sig_analysis,
        "recommended_weights": rec_weights,
        "recommended_logistic": {"intercept": calib.get("intercept"),
                                 "gain": calib.get("gain")},
        "reliability": reliability,
        "isotonic_brier_in_sample": iso_brier,
        "walk_forward": _walk_forward(rows),
        "fitted_logistic": _fitted_evaluation(rows, FUNDAMENTAL),
        "rows": rows,
    }


def reaction_backtest(tickers: Optional[List[str]] = None, max_quarters: int = 16,
                      since: Optional[str] = None) -> Dict[str, Any]:
    """Validate the post-earnings MOVE model (Q3 / Kelly reversal) on realized returns.

    For each past earnings event we rebuild the as-of stock_up probability (fundamental
    beat odds, priced-in from Kelly over-extrapolation) and compare to the realized
    close[-1]->close[+2] move. Crucially reports the Kelly test: correlation between the
    extrapolated-return measure and the realized post-earnings move (should be NEGATIVE
    if over-extrapolation -> reversal holds)."""
    from tools.snapshot import extrapolated_return_measure
    tickers = tickers or all_basket_tickers()
    cfg = load_weights()
    w, log, rcfg = cfg["weights"], cfg["logistic"], cfg["reaction"]
    rows: List[Dict[str, Any]] = []
    for t in tickers:
        sym = t.strip().upper()
        eh = list(md.get_earnings_history(sym))
        if not eh:
            continue
        bars = md.get_price_bars(sym, "5y")
        if not bars:
            continue
        for i, ev in enumerate(eh[:max_quarters]):
            asof = ev["date"]
            if asof is None or asof > datetime.now():
                continue
            if since and ev["date"].date().isoformat() < since:
                continue
            post = md.post_earnings_return(sym, asof, bars)
            if post is None:
                continue
            prior_dates = [e["date"] for e in eh[i + 1:]]
            kelly = extrapolated_return_measure(bars, prior_dates)
            ext = kelly.get("ext_ret")
            prior_surprises = [{"surprisePercent": e["surprise_pct"],
                                "surprise": (None if e["eps_estimate"] is None else e["eps_actual"] - e["eps_estimate"]),
                                "period": e["date"].date().isoformat()} for e in eh[i + 1:]]
            data = {"surprises": prior_surprises, "recommendations": [],
                    "pre_earnings_return": md.pre_earnings_return(sym, asof=asof),
                    "rel_strength": md.relative_strength(sym, asof=asof),
                    "volume_buildup": md.volume_buildup(sym, asof=asof),
                    "options": {}, "news": [],
                    "kelly": {"ext_ret": ext, "n_quarters": kelly.get("n_quarters")} if ext is not None else None}
            sigs = signals.compute_all(data)
            scores = {n: sigs[n].score for n in FUNDAMENTAL if n in sigs and sigs[n].present}
            beat = _prob(scores, w, log["intercept"], log["gain"])
            oe = sigs.get("over_extrapolation")
            oe_score = oe.score if (oe and oe.present) else 0.0
            pi = 0.5 + 0.5 * abs(oe_score)
            su = beat - (pi - 0.5) * rcfg["priced_in_penalty"] - rcfg["kelly_reversal"] * oe_score \
                + rcfg.get("announcement_premium", 0.0)
            su = max(0.03, min(0.97, su))
            rows.append({"symbol": sym, "period": ev["date"].date().isoformat(),
                         "ext_ret": ext, "stock_up_prob": su,
                         "actual_up": bool(post > 0), "post_move": round(post, 4)})
    n = len(rows)
    if n == 0:
        return {"n": 0, "note": "no usable events"}
    y = np.array([1.0 if r["actual_up"] else 0.0 for r in rows])
    p = np.array([r["stock_up_prob"] for r in rows])
    up_rate = float(np.mean(y))
    out = {
        "n": n,
        "accuracy": round(float(np.mean((p >= 0.5) == (y == 1))), 3),
        "brier": round(float(np.mean((p - y) ** 2)), 4),
        "baseline_up_rate": round(up_rate, 3),
        "baseline_brier": round(float(np.mean((up_rate - y) ** 2)), 4),
    }
    out["beats_baseline_brier"] = out["brier"] < out["baseline_brier"]
    # Kelly test: ExtRet vs realized post-earnings move (expect NEGATIVE).
    exts = [(r["ext_ret"], r["post_move"]) for r in rows if r["ext_ret"] is not None]
    if len(exts) >= 5:
        out["kelly_extret_vs_move_corr"] = round(_pearson([e[0] for e in exts], [e[1] for e in exts]), 3)
        out["kelly_reversal_holds"] = out["kelly_extret_vs_move_corr"] < 0
        out["kelly_n"] = len(exts)
    rows.sort(key=lambda r: r["period"], reverse=True)
    out["events"] = rows[:200]
    return out


def write_report(result: Dict[str, Any], max_quarters: int) -> str:
    """Write the markdown validation report (the user's 8 required points)."""
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    path = OUTPUT_DIR / f"backtest_report_{stamp}.md"
    mc, mk = result["metrics_current"], result["metrics_calibrated"]
    periods = sorted(r["period"] for r in result["rows"])
    span = f"{periods[0]} … {periods[-1]}" if periods else "n/a"

    L: List[str] = []
    L.append(f"# Backtest & Calibration Report — v1.1 ({stamp})\n")
    L.append("Validates the rules-based fundamental EPS-beat model against a naive "
             "always-beat baseline, then calibrates weights + logistic from the data. "
             "**Research only — not financial advice.**\n")

    L.append("## 1. Tickers tested")
    for cat, tks in DEFAULT_BASKET.items():
        L.append(f"- **{cat}**: {', '.join(tks)}")
    L.append(f"\nUsable events by category: `{result['coverage_by_category']}`")
    L.append(f"Total earnings events tested: **{mc.get('n', 0)}** "
             f"(up to {max_quarters} quarters/ticker)\n")

    L.append("## 2. Time period")
    L.append(f"Fiscal-period span of tested quarters: **{span}** "
             "(bounded by 2y of free price history; announcement ≈ period-end + 40d).\n")

    L.append("## 3. Data available vs missing")
    L.append("- **Available (tested):** EPS consensus + actual (Finnhub), prior surprises, "
             "pre-earnings price/volume + relative strength (yfinance).")
    L.append("- **Missing / untested:** historical **revenue consensus** (premium → "
             "revenue-beat accuracy NOT measurable on free data); **analyst_revision**, "
             "**news_sentiment**, **options** signals have no free history → absent here, "
             "weights left unchanged.\n")

    L.append("## 4. Did the model beat the baseline?")
    L.append("| metric | current model | calibrated model | always-beat baseline |")
    L.append("|---|---|---|---|")
    L.append(f"| EPS-beat accuracy | {mc.get('accuracy')} | {mk.get('accuracy')} | "
             f"{mc.get('baseline_always_beat_accuracy')} |")
    L.append(f"| precision | {mc.get('precision')} | {mk.get('precision')} | — |")
    L.append(f"| recall | {mc.get('recall')} | {mk.get('recall')} | — |")
    L.append(f"| false positives | {mc.get('false_positives')} | {mk.get('false_positives')} | — |")
    L.append(f"| false negatives | {mc.get('false_negatives')} | {mk.get('false_negatives')} | — |")
    L.append(f"| Brier (lower=better) | {mc.get('brier')} | {mk.get('brier')} | {mc.get('brier_baseline')} |")
    verdict = ("**YES** — calibrated Brier beats the baseline Brier."
               if mk.get("beats_baseline_brier") else
               "**NO / marginal** — model does not clearly beat the always-beat baseline.")
    L.append(f"\nVerdict: {verdict}")
    L.append("Revenue-beat accuracy: **not available** (no free historical revenue consensus).\n")

    L.append("## 5. Strongest signals")
    L.append("| signal | n | corr w/ beat | dir. hit-rate | verdict |")
    L.append("|---|---|---|---|---|")
    for a in result["signal_analysis"]:
        L.append(f"| {a['signal']} | {a['n_present']} | {a['corr']} | "
                 f"{a['hit_rate']} | {a['verdict']} |")
    helps = [a["signal"] for a in result["signal_analysis"] if a["verdict"] == "helps"]
    L.append(f"\nHelped most: **{', '.join(helps) or 'none clearly'}**.\n")

    L.append("## 6. Signals to reduce or remove")
    weak = [a["signal"] for a in result["signal_analysis"] if a["verdict"] in ("noise", "hurts")]
    L.append(f"Down-weighted to ~0 (noise/hurts): **{', '.join(weak) or 'none'}**. "
             "Untested signals are kept pending free history.\n")

    L.append("## 7. New calibrated weights")
    L.append("```yaml")
    L.append("weights:")
    for k, v in result["recommended_weights"].items():
        L.append(f"  {k}: {v}")
    rl = result["recommended_logistic"]
    L.append("logistic:")
    L.append(f"  intercept: {rl['intercept']}")
    L.append(f"  gain: {rl['gain']}")
    L.append("```\n")

    L.append("## 8. Still to improve before serious Polymarket use")
    L.append("- Validate **revenue-beat** (needs a historical revenue-consensus source).")
    L.append("- Backtest **analyst_revision / news_sentiment / options** (no free history yet).")
    L.append("- Use real **announcement dates** instead of period-end + 40d.")
    L.append("- Larger sample + walk-forward (out-of-sample) calibration to avoid overfitting "
             "these 2 scalars to this basket.")
    L.append("- Validate the **priced-in (Q2)** and **edge (Q3)** layers against realized "
             "post-earnings moves and logged Polymarket outcomes.\n")

    path.write_text("\n".join(L))
    return str(path)

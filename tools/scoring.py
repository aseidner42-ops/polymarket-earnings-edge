"""Combine signals into probabilities. Transparent, rules-based, no ML (v1).

Two distinct outputs, kept separate on purpose (the user's questions 1 vs 2):
  * fundamental beat probability  -> will the company beat official consensus?
  * priced-in index               -> how much is a move already expected?

A logistic blend turns a weight-normalized signal score into a probability.
Intercept/gain live in weights.yaml so the backtester's calibration step can
tune them without code changes.
"""
from __future__ import annotations

import math
from typing import Any, Dict, List, Optional, Tuple

from tools.config import load_weights
from tools.signals import Signal

# Which signals inform the fundamental beat vs the priced-in index.
# CRITIQUE #4/#6 (2026-09-21): price/positioning signals are NOT fundamentals and were letting
# "the stock went up" masquerade as an earnings read (GME 62%→92%). They are split into their own
# family (MARKET_SIGNALS) whose net influence on the beat probability is HARD-CAPPED (see
# score_fundamental). FUNDAMENTAL_CORE is the honest "ignoring the stock/options market, what do
# the fundamentals say?" set. FUNDAMENTAL is kept = CORE + MARKET for the callers that want the
# full signal list (SCAR coherence, bear_signals, backtest feature set).
FUNDAMENTAL_CORE = ["sue_momentum", "surprise_streak", "analyst_revision", "news_sentiment",
                    "peer_beats", "dispersion", "accruals", "fundamental_trend",
                    "filing_risk", "piotroski"]
MARKET_SIGNALS = ["pre_earnings_drift", "rel_strength", "pump"]   # price/technical family (capped)
FUNDAMENTAL = FUNDAMENTAL_CORE + MARKET_SIGNALS
# Critique #6: a pre-earnings run-up is a PRICED-IN / over-extrapolation signal (Kelly), not
# evidence of a fundamental beat — so pre_earnings_drift lives here (and is capped, ~0-coef, in
# the beat model). It is NOT reversed to "bearish for the beat" (Kelly is about post-print returns,
# not P(beat)); it simply belongs in the priced-in layer.
PRICED_IN = ["over_extrapolation", "options_move", "volume_buildup", "rel_strength",
             "pre_earnings_drift"]

FUND_SPREAD = 2.5  # maps weight-normalized score in [-1,1] into a meaningful logit range

# Collinearity groups: signals reading the SAME underlying information. When several
# in a group are present they double-count, so each is damped by 1/sqrt(group size)
# (variance-inflation style) — prevents one theme from dominating the probability.
SIGNAL_GROUPS = {
    "sue_momentum": "earnings_history", "surprise_streak": "earnings_history",
    "peer_beats": "earnings_history",
    "analyst_revision": "estimates", "dispersion": "estimates",
    "pre_earnings_drift": "price_momentum", "rel_strength": "price_momentum",
    "pump": "price_momentum",
    "news_sentiment": "sentiment", "accruals": "quality", "fundamental_trend": "quality",
    "piotroski": "quality",
    "filing_risk": "event_risk",
}

# Structural beat rates by sector — firms manage earnings to meet/beat
# (Burgstahler-Dichev 1997); the base rate differs by sector. Sets the prior.
SECTOR_BEAT_RATE = {
    "Technology": 0.72, "Healthcare": 0.68, "Financial Services": 0.62,
    "Consumer Cyclical": 0.65, "Consumer Defensive": 0.66, "Energy": 0.55,
    "Industrials": 0.66, "Communication Services": 0.66, "Utilities": 0.60,
    "Basic Materials": 0.60, "Real Estate": 0.60,
}
DEFAULT_BEAT_RATE = 0.65  # Burgstahler structural tendency to beat


def _logistic(x: float) -> float:
    import math
    return 1.0 / (1.0 + math.exp(-x))


def _weighted_norm(signals: Dict[str, Signal], names: List[str],
                   weights: Dict[str, float], use_abs: bool = False,
                   decorrelate: bool = False) -> Tuple[float, int]:
    """Weight-normalized score over present signals in `names`. Returns (norm, n_present).

    decorrelate=True damps each signal by 1/sqrt(#present in its collinearity group),
    so redundant signals reading the same information don't double-count."""
    group_counts: Dict[str, int] = {}
    if decorrelate:
        for name in names:
            s = signals.get(name)
            if s and s.present:
                g = SIGNAL_GROUPS.get(name, name)
                group_counts[g] = group_counts.get(g, 0) + 1
    num = den = 0.0
    n = 0
    for name in names:
        s = signals.get(name)
        if s is None or not s.present:
            continue
        w = abs(weights.get(name, 0.0))
        if decorrelate:
            g = SIGNAL_GROUPS.get(name, name)
            w /= math.sqrt(group_counts.get(g, 1))
        val = abs(s.score) if use_abs else s.score
        num += w * val
        den += w
        n += 1
    return (num / den if den > 0 else 0.0), n


def sector_base_rate(sector: Optional[str]) -> float:
    return SECTOR_BEAT_RATE.get(sector or "", DEFAULT_BEAT_RATE)


_FITTED_CACHE = {"mtime": None, "model": None}


def _fitted_model():
    """Load an AUTO-FITTED logistic model if autofit.py has adopted one (walk-forward,
    OOS-validated). Returns None normally — so default behavior is unchanged unless the
    fitter proved an improvement on held-out data. Cached by file mtime."""
    import os
    from tools.config import OUTPUT_DIR
    p = OUTPUT_DIR / "fitted_model.json"
    try:
        mt = os.path.getmtime(p)
    except OSError:
        _FITTED_CACHE["model"] = None
        return None
    if _FITTED_CACHE["mtime"] != mt:
        try:
            import json
            _FITTED_CACHE["model"] = json.loads(open(p).read())
        except Exception:
            _FITTED_CACHE["model"] = None
        _FITTED_CACHE["mtime"] = mt
    return _FITTED_CACHE["model"]


def score_fundamental(signals: Dict[str, Signal],
                      sector: Optional[str] = None) -> Dict[str, Any]:
    """Probability the company beats official consensus.

    Two-part, kept honest (critique #4/#6, 2026-09-21):
      * fundamental_only_prob  — from FUNDAMENTAL_CORE alone (no price/positioning). This is
        "ignoring the stock and options market, what do the fundamentals predict?"
      * price/positioning family adds a CAPPED adjustment (±market_cap_pp, default 5pp). Price
        signals may only NUDGE the beat probability until forward evidence earns them more — they
        can no longer turn 62% into 92% by voting "the stock went up" three times.

    Works for BOTH paths via a counterfactual: the probability is computed with the market signals
    at their real values and again with them neutralized (fitted: z=0 for those features; hand
    blend: dropped from the norm); the difference is the price family's raw influence, which is
    then clamped. The prior (logit intercept) uses the sector structural beat rate
    (Burgstahler-Dichev 1997). An OOS-validated auto-fitted model, if adopted, drives the base."""
    cfg = load_weights()
    w = cfg["weights"]
    log = cfg["logistic"]
    cap = log.get("market_cap_pp", 5) / 100.0
    fm = _fitted_model()
    br = sector_base_rate(sector)
    intercept = math.log(br / (1 - br)) if sector else log["intercept"]

    def _prob(zero_market: bool) -> float:
        if fm and fm.get("features"):
            import numpy as _np
            mu, sd, coef = _np.array(fm["mu"]), _np.array(fm["sd"]), _np.array(fm["coef"])
            x = _np.array([signals[f].score if (f in signals and signals[f].present) else 0.0
                           for f in fm["features"]], float)
            z = (x - mu) / sd
            if zero_market:                       # neutralize price features (z=0 -> no contribution)
                for i, f in enumerate(fm["features"]):
                    if f in MARKET_SIGNALS:
                        z[i] = 0.0
            return float(1.0 / (1.0 + _np.exp(-_np.clip(fm["intercept"] + z @ coef, -30, 30))))
        # hand blend: compute over the SAME full name set both ways; neutralize the price family by
        # zeroing its SCORES (keeps the denominator identical, mirroring the fitted z=0) so raw_adj
        # isolates the price signals and can't fold in a fundamentals re-scaling from the changing
        # denominator (fix 2026-09-21, agent review).
        sig = signals
        if zero_market:
            from dataclasses import replace
            sig = dict(signals)
            for m in MARKET_SIGNALS:
                if m in sig and sig[m] is not None and sig[m].present:
                    sig[m] = replace(sig[m], score=0.0)
        norm, _ = _weighted_norm(sig, FUNDAMENTAL, w, decorrelate=True)
        return _logistic(intercept + log["gain"] * FUND_SPREAD * norm)

    core_prob = _prob(zero_market=True)           # fundamentals only
    full_prob = _prob(zero_market=False)          # with price/positioning
    raw_adj = full_prob - core_prob
    capped_adj = max(-cap, min(cap, raw_adj))     # price family may move the prob by <= cap
    prob = max(0.02, min(0.98, core_prob + capped_adj))

    # Per-signal contribution to the logit under the LIVE model (fitted coef·z, else hand
    # weight·score with group damping) — for HONEST evidence ranking. Raw |score| is misleading:
    # a max-score signal whose fitted coefficient is ~0 (e.g. pre_earnings_drift) contributes
    # nothing, yet dominated the old evidence panel. This is what ACTUALLY moved the probability.
    # Market-signal contributions are shown NET OF THE CAP (scaled by capped/raw) so the panel
    # can't overstate a price signal whose real net effect was clamped to <=cap (fix 2026-09-21).
    contribs: Dict[str, float] = {}
    _ratio = (capped_adj / raw_adj) if abs(raw_adj) > 1e-9 else 1.0
    if fm and fm.get("features"):
        import numpy as _np
        mu, sd, coef = _np.array(fm["mu"]), _np.array(fm["sd"]), _np.array(fm["coef"])
        for i, f in enumerate(fm["features"]):
            if f in signals and signals[f].present:
                c = float(((signals[f].score - mu[i]) / sd[i]) * coef[i])
                contribs[f] = round(c * (_ratio if f in MARKET_SIGNALS else 1.0), 3)
    else:
        gc: Dict[str, int] = {}
        for f in FUNDAMENTAL:
            s = signals.get(f)
            if s and s.present:
                g = SIGNAL_GROUPS.get(f, f)
                gc[g] = gc.get(g, 0) + 1
        den = sum(abs(w.get(f, 0.0)) / math.sqrt(gc.get(SIGNAL_GROUPS.get(f, f), 1))
                  for f in FUNDAMENTAL if signals.get(f) and signals[f].present)
        scale = (log["gain"] * FUND_SPREAD / den) if den > 0 else 0.0   # -> true logit-delta units
        for f in FUNDAMENTAL:
            s = signals.get(f)
            if s and s.present:
                g = SIGNAL_GROUPS.get(f, f)
                c = (abs(w.get(f, 0.0)) / math.sqrt(gc.get(g, 1))) * s.score * scale
                contribs[f] = round(c * (_ratio if f in MARKET_SIGNALS else 1.0), 3)

    # Confidence + reporting reflect the CORE fundamentals, not the price signals.
    core_present = [signals[n] for n in FUNDAMENTAL_CORE if n in signals and signals[n].present]
    n_present = len(core_present)
    pos = sum(1 for s in core_present if s.score > 0)
    agreement = max(pos, len(core_present) - pos) / len(core_present) if core_present else 0.0
    norm_core, _ = _weighted_norm(signals, FUNDAMENTAL_CORE, w, decorrelate=True)
    all_present = [signals[n] for n in FUNDAMENTAL if n in signals and signals[n].present]
    return {
        "probability": prob,
        "fundamental_only_prob": round(core_prob, 3),
        "market_adjustment_pp": round(capped_adj * 100, 1),
        "market_adjustment_raw_pp": round(raw_adj * 100, 1),
        "market_capped": abs(raw_adj) > cap,
        "norm": norm_core,
        "n_present": n_present,
        "agreement": agreement,
        "contributions": {s.name: round(w.get(s.name, 0) * s.score, 3) for s in all_present},
        "logit_contributions": contribs,
    }


def priced_in_index(signals: Dict[str, Signal]) -> Dict[str, Any]:
    """0..1: how much a big move is already priced in (Kelly 2016 over-extrapolation,
    implied move size, volume surge, extreme relative strength). 0.5 ~ normal."""
    cfg = load_weights()
    w = cfg["weights"]
    norm, n_present = _weighted_norm(signals, PRICED_IN, w, use_abs=True)
    return {"index": norm, "n_present": n_present}


def predicted_surprises(signals: Dict[str, Signal],
                        surprises: List[Dict[str, Any]]) -> Dict[str, float]:
    """Rough predicted surprise % for EPS and revenue. Anchored on historical
    EPS-surprise average plus a tilt from the fundamental signal score. Crude by
    design — flagged as low-precision in the report."""
    fund = score_fundamental(signals)
    norm = fund["norm"]
    hist = [s.get("surprisePercent") for s in surprises[:4] if s.get("surprisePercent") is not None]
    hist_avg = sum(hist) / len(hist) if hist else 0.0
    eps_pct = 0.5 * hist_avg + 5.0 * norm          # percentage points
    rev_pct = 2.0 * norm                            # revenue surprises are smaller
    return {"eps_surprise_pct": eps_pct, "revenue_surprise_pct": rev_pct}


def edge_estimate(signals: Dict[str, Signal], market_prob: float,
                  model_prob: Optional[float] = None, scar: Optional[float] = None) -> Dict[str, Any]:
    """SCAR-SHRINKAGE edge (rebuilt 2026-09-21 per critique). The Polymarket price is a
    better-calibrated prior than our model (proven: on the real bets, market Brier 0.151 <
    model 0.173, and our biggest disagreements LOST −0.67/$1). So we ANCHOR on the market and
    only move toward our own probability in proportion to SCAR conviction:

        P_edge = P_market + λ(SCAR) · (P_model − P_market),   λ = (SCAR/100)·λ_max

    Low SCAR → λ≈0 → we defer to the market (tiny edge). High SCAR → λ up to λ_max (<1, so we
    NEVER fully override the market). This gives SCAR an explicit statistical role (a shrinkage
    weight, not an arbitrary gate) and structurally stops the over-extremizing that lost money.
    The orthogonal signals are kept only to EXPLAIN which factors justify any deviation.

    Falls back to the old orthogonal-tilt only if model_prob/scar are unavailable."""
    cfg = load_weights().get("edge", {})
    weights = cfg.get("orthogonal", {})
    drivers = []
    num = den = 0.0
    for name, w in weights.items():
        s = signals.get(name)
        if not s or not s.present:
            continue
        val = (-s.score) if name == "over_extrapolation" else s.score
        num += w * val
        den += w
        if abs(s.score) > 0.15:
            drivers.append({"signal": name, "contribution": round(w * val, 3),
                            "evidence": s.evidence})
    tilt = (num / den) if den > 0 else 0.0
    drivers.sort(key=lambda d: -abs(d["contribution"]))

    if model_prob is not None and scar is not None:
        lam_max = cfg.get("lambda_max", 0.40)
        lam = max(0.0, min(1.0, scar / 100.0)) * lam_max
        aware = max(0.03, min(0.97, market_prob + lam * (model_prob - market_prob)))
        return {"tilt": round(tilt, 3), "market_aware_prob": round(aware, 3),
                "edge_vs_market": round(aware - market_prob, 3), "drivers": drivers[:4],
                "lambda_scar": round(lam, 3), "shrinkage": True}
    # fallback (no model_prob/scar): legacy orthogonal tilt
    gain = cfg.get("gain", 0.15)
    aware = max(0.03, min(0.97, market_prob + gain * tilt))
    return {"tilt": round(tilt, 3), "market_aware_prob": round(aware, 3),
            "edge_vs_market": round(aware - market_prob, 3), "drivers": drivers[:4],
            "shrinkage": False}


def confidence_label(fund: Dict[str, Any]) -> str:
    """high / medium / low from #present fundamental signals and their agreement."""
    cfg = load_weights()["confidence"]
    n, agree = fund["n_present"], fund["agreement"]
    if n >= cfg["min_signals_for_high"] and agree >= cfg["agreement_high"]:
        return "high"
    if n >= cfg["min_signals_for_med"]:
        return "medium"
    return "low"

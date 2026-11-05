"""CWSF — Coverage-Weighted Surprise Fingerprint (original estimator).

Goal: predict the EXACT EPS surprise %, not just beat/miss — and be honestly more
precise on small / sparsely-covered companies.

Idea (novel synthesis, empirical-Bayes flavored):
  * Every firm has a SURPRISE FINGERPRINT — the recency-weighted mean (mu) and
    volatility (sigma) of its own past surprise %. Steady firms surprise in a tight,
    characteristic band; erratic firms don't.
  * Analyst consensus/revisions are informative ONLY when coverage is good. So we
    compute a COVERAGE factor kappa from (# analysts, forecast dispersion):
      - many analysts + tight consensus -> kappa -> 1  -> trust revision drift
      - few analysts / wide dispersion (small caps) -> kappa -> 0 -> trust the firm's
        own fingerprint, and WIDEN the uncertainty band (honest "we're less sure").
  * Event tilt (SEC 8-K risk, fundamental deterioration) shifts the mean down.

Output: a predicted surprise %, a firm-specific sigma, and P(surprise > threshold)
under a Normal — a calibrated beat probability that self-widens for small names.
"""
from __future__ import annotations

import math
import statistics
from typing import Any, Dict, List, Optional

try:
    from statistics import NormalDist
except ImportError:  # py<3.8
    NormalDist = None


# Surprise % blows up when EPS estimate ≈ 0 (breakeven names: BA, INTC). Winsorize
# so a single near-zero-denominator freak (+2000%) doesn't wreck the fingerprint.
WINSOR = 50.0


def _clip(p: float) -> float:
    return max(-WINSOR, min(WINSOR, float(p)))


def fingerprint(surprise_pcts: List[float], decay: float = 0.85) -> Optional[Dict[str, float]]:
    """Firm surprise fingerprint from past surprise % (most-recent-first), winsorized."""
    pcts = [_clip(p) for p in surprise_pcts if p is not None]
    if not pcts:
        return None
    w = [decay ** i for i in range(len(pcts))]
    mu = sum(wi * p for wi, p in zip(w, pcts)) / sum(w)
    sd = statistics.pstdev(pcts) if len(pcts) >= 2 else max(abs(mu), 3.0)
    beat_rate = sum(1 for p in pcts if p > 0) / len(pcts)
    # consistency = how tight the surprises cluster (low std relative to |mu|)
    consistency = abs(mu) / sd if sd > 0 else 0.0
    return {"mu": mu, "sigma": max(sd, 0.5), "beat_rate": beat_rate,
            "n": len(pcts), "consistency": round(consistency, 2)}


def coverage_kappa(n_analysts: Optional[float], dispersion: Optional[float]) -> float:
    """0 = sparse/uncertain coverage (trust the firm's fingerprint, widen band);
    1 = deep, tight coverage (trust analyst consensus/revisions)."""
    if n_analysts is None and dispersion is None:
        return 0.30
    cov = 1.0 / (1.0 + math.exp(-((n_analysts or 8) - 12) / 6.0))      # ~0.5 at 12 analysts
    tight = (1.0 / (1.0 + math.exp(((dispersion or 0.12) - 0.12) / 0.06))) if dispersion is not None else 0.6
    return round(0.5 * cov + 0.5 * tight, 3)


def predict_surprise(surprise_pcts: List[float], revision_drift_pct: float = 0.0,
                     n_analysts: Optional[float] = None, dispersion: Optional[float] = None,
                     event_tilt: float = 0.0) -> Optional[Dict[str, Any]]:
    """Predict the surprise % (point) + firm-specific sigma + coverage factor.

    revision_drift_pct: analyst estimate change % (live); event_tilt: small negative
    when SEC/fundamental deterioration signals fire (pulls the surprise down)."""
    fp = fingerprint(surprise_pcts)
    if fp is None:
        return None
    kappa = coverage_kappa(n_analysts, dispersion)
    SHRINK, REVW, EVW = 0.55, 0.6, 3.0
    pred = SHRINK * fp["mu"] + kappa * REVW * revision_drift_pct + EVW * event_tilt
    # sigma widens with dispersion and with LOW coverage (small caps least certain).
    sigma = fp["sigma"] * (1 + 0.8 * (dispersion or 0.0)) * (1 + 0.6 * (1 - kappa))
    sigma = max(sigma, 1.0)
    return {"pred_surprise_pct": round(pred, 2), "sigma": round(sigma, 2),
            "kappa": kappa, "fingerprint": fp}


def p_beat(pred_surprise_pct: float, sigma: float, threshold_pct: float = 0.0) -> float:
    """P(surprise % > threshold) under Normal(pred, sigma). Self-widens for small caps,
    pulling their probabilities honestly toward 0.5."""
    if NormalDist is None or sigma <= 0:
        return 1.0 if pred_surprise_pct > threshold_pct else 0.0
    return round(1.0 - NormalDist(pred_surprise_pct, sigma).cdf(threshold_pct), 4)


# ---------------------------------------------------------------------------
# CENTS / LEVEL model (critique #8, 2026-09-21). Percentage surprises are TOXIC when
# consensus EPS is small: $0.04 -> $0.17 is a +325% surprise that's really 13 cents, and a
# handful of tiny-denominator quarters wreck the fingerprint. The Polymarket contract itself
# resolves on the reported EPS LEVEL in dollars vs a fixed strike — so we model EPS in DOLLARS,
# using a ROBUST scale (MAD, not stdev, so one freak quarter can't dominate).
# ---------------------------------------------------------------------------
def _median(xs: List[float]) -> float:
    return statistics.median(xs)


def predict_eps_level(consensus_eps: Optional[float],
                      actual_estimate_pairs: List[tuple], revision_drift_pct: float = 0.0,
                      n_analysts: Optional[float] = None, dispersion: Optional[float] = None,
                      event_tilt: float = 0.0, decay: float = 0.85) -> Optional[Dict[str, Any]]:
    """Predict the reported EPS LEVEL (dollars) + a robust dollar sigma.

    actual_estimate_pairs: (actual, estimate) per already-reported quarter, most-recent-first.
    Models the firm's historical dollar forecast error e = actual - estimate; the next EPS =
    consensus + recency-weighted shrunk mean error + (coverage-weighted) revision drift. The
    scale is MAD-based (robust) so small-EPS names aren't blown up by percentage math."""
    if consensus_eps is None or not math.isfinite(float(consensus_eps)):
        return None
    errs = [float(a) - float(e) for (a, e) in actual_estimate_pairs
            if a is not None and e is not None
            and math.isfinite(float(a)) and math.isfinite(float(e))]   # reject NaN/inf pairs
    if not errs:
        return None
    w = [decay ** i for i in range(len(errs))]
    mu_err = sum(wi * x for wi, x in zip(w, errs)) / sum(w)
    med = _median(errs)
    mad = _median([abs(x - med) for x in errs])
    scale = 1.4826 * mad                          # MAD -> robust sigma estimate
    if scale <= 0:                                # all errors identical (or n=1) -> fall back
        scale = statistics.pstdev(errs) if len(errs) >= 2 else max(abs(mu_err), 0.02)
    kappa = coverage_kappa(n_analysts, dispersion)
    SHRINK, REVW, EVW = 0.55, 0.6, 1.0
    rev_drift_dollars = (revision_drift_pct / 100.0) * abs(consensus_eps)
    # event tilt (SEC/fundamental risk) as a modest dollar shift — EVW=1.0 (NOT the %-model's 3.0,
    # which in dollar space over-weights it ~100x; fix 2026-09-21). Caller event_tilt ~[-.02,.02].
    event_dollars = EVW * event_tilt * max(abs(consensus_eps), 0.05)
    pred_eps = consensus_eps + SHRINK * mu_err + kappa * REVW * rev_drift_dollars + event_dollars
    sigma = scale * (1 + 0.8 * (dispersion or 0.0)) * (1 + 0.6 * (1 - kappa))
    sigma = max(sigma, 0.01)                       # 1-cent floor
    return {"pred_eps": round(pred_eps, 4), "sigma_eps": round(sigma, 4), "kappa": kappa,
            "mu_err": round(mu_err, 4), "scale": round(scale, 4), "n": len(errs)}


_RECAL_CACHE = {"mtime": None, "params": None}


def _strike_recal():
    """Load the OOS-validated Platt recalibration for contract probabilities, if adopted.
    Returns {a, b} or None. The synthetic-strike backtest showed contract_prob systematically
    UNDER-predicts P(clear strike) (firms beat ~72%); this corrects it. Cached by file mtime."""
    import os
    from tools.config import OUTPUT_DIR
    p = OUTPUT_DIR / "strike_calibration.json"
    try:
        mt = os.path.getmtime(p)
    except OSError:
        _RECAL_CACHE["params"] = None
        return None
    if _RECAL_CACHE["mtime"] != mt:
        try:
            import json
            _RECAL_CACHE["params"] = json.loads(open(p).read())
        except Exception:
            _RECAL_CACHE["params"] = None
        _RECAL_CACHE["mtime"] = mt
    return _RECAL_CACHE["params"]


def recalibrate_contract_prob(p: float) -> float:
    """Apply the adopted Platt recal: corrected = sigmoid(a + b·logit(p)). No-op if none adopted."""
    r = _strike_recal()
    if not r or "a" not in r or "b" not in r:
        return p
    pc = min(1 - 1e-6, max(1e-6, float(p)))
    z = r["a"] + r["b"] * math.log(pc / (1 - pc))
    return round(1.0 / (1.0 + math.exp(-max(-30, min(30, z)))), 4)


def p_level_over(pred_eps: float, sigma_eps: float, level: float) -> float:
    """Plain P(reported EPS > level) under Normal — for consensus-beat (no rounding contract)."""
    if NormalDist is None or sigma_eps <= 0:
        return 1.0 if pred_eps > level else 0.0
    return round(1.0 - NormalDist(pred_eps, sigma_eps).cdf(level), 4)


def p_eps_over(pred_eps: float, sigma_eps: float, strike: float,
               equality_no: bool = True, operator: str = ">") -> float:
    """P( round(reported EPS, 2)  <operator>  strike ) under Normal(pred_eps, sigma_eps).

    Handles the contract's ROUNDING + equality rule (critique #3): a market that resolves YES iff
    reported EPS > $0.27 with equality=NO and cent rounding means round(EPS,2) must be >= $0.28,
    i.e. reported EPS >= strike + 0.005. So the effective dollar threshold is nudged half a cent."""
    if operator in (">", ">="):
        thr = strike + (0.005 if (operator == ">" and equality_no) else -0.005)
        if NormalDist is None or sigma_eps <= 0:
            return 1.0 if pred_eps > thr else 0.0
        return round(1.0 - NormalDist(pred_eps, sigma_eps).cdf(thr), 4)
    # operator "<" / "<=" (rare): P below the strike
    thr = strike - (0.005 if (operator == "<" and equality_no) else -0.005)
    if NormalDist is None or sigma_eps <= 0:
        return 1.0 if pred_eps < thr else 0.0
    return round(NormalDist(pred_eps, sigma_eps).cdf(thr), 4)

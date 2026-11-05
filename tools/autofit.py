"""Autonomous, walk-forward, OOS-GATED weight fitter — the honest core of "the program
evolves its own formula from the trends it sees."

Every run it:
  1. Backtests a broad basket -> events with signal scores + realized beat.
  2. Splits CHRONOLOGICALLY: fit on the older 70%, TEST on the newest 30% the fit never saw.
  3. Compares the fitted logistic's out-of-sample Brier to the CURRENT model's OOS Brier on
     the exact same held-out events.
  4. ADOPTS the fitted model (writes output/fitted_model.json, which scoring.py then uses)
     ONLY if the paired ΔBrier (current − candidate), block-bootstrapped by ticker, has a
     95% CI strictly above 0 — a statistically-meaningful improvement, not a point comparison.

HONEST CAVEAT (corrected 2026-09-21): this does NOT "physically prevent overfitting." Because
the daily job re-tests candidates against a rolling OOS split, the test set can leak into
selection over many runs (adaptive overfitting to the validation set). The block-bootstrap CI
raises the bar and the ticker-blocking respects clustering, but the DEFINITIVE validation is
the PROSPECTIVE forward vault — future Polymarket contracts that did not exist when anything
was designed. Treat autofit as calibration improvement on rolling data, not proof of edge.
Fully reversible: delete output/fitted_model.json to revert to the hand-set blend.
"""
from __future__ import annotations

import json
from datetime import datetime
from typing import Any, Dict, List, Optional

import numpy as np

from tools.backtest import backtest, _fit_logistic, _logistic_prob, _logistic, FUND_SPREAD
from tools.config import load_weights, OUTPUT_DIR
from tools.scoring import FUNDAMENTAL

_FITTED = OUTPUT_DIR / "fitted_model.json"
MIN_TEST = 40          # need a real held-out set before trusting an OOS comparison
MIN_BLOCKS = 6         # need >=this many distinct tickers in the test set for a valid block bootstrap

_UNIVERSE = (
    "AAPL MSFT NVDA META AMZN GOOGL TSLA NFLX AMD INTC QCOM AVGO MU TXN ORCL CRM ADBE NOW INTU "
    "PANW SNOW NET DDOG CRWD ZS MDB PLTR UBER ABNB COIN KO PEP MDLZ MNST KHC PG CL JPM BAC GS "
    "WFC C AXP V MA MS USB XOM CVX COP SLB JNJ PFE MRK ABBV LLY TMO ABT UNH CI ELV CAT DE BA GE "
    "HON MMM UNP UPS HD LOW COST WMT TGT NKE SBUX MCD DIS CMCSA T VZ"
).split()


def _current_oos_brier(test: List[Dict[str, Any]]) -> Optional[float]:
    """Brier of the CURRENT hand-set blend on the held-out events (from each row's norm)."""
    log = load_weights()["logistic"]
    if not test:
        return None
    p = np.array([_logistic(log["intercept"] + log["gain"] * FUND_SPREAD * r["norm"]) for r in test])
    y = np.array([1.0 if r["actual_beat"] else 0.0 for r in test])
    return float(np.mean((p - y) ** 2))


def run(stamp: Optional[str] = None, tickers: Optional[List[str]] = None,
        rows: Optional[List[Dict[str, Any]]] = None) -> Dict[str, Any]:
    """Walk-forward fit + OOS-gated adoption. Pass `rows` (from a backtest already run today)
    to avoid a second backtest — lets the daily job feed its own events straight in."""
    stamp = stamp or datetime.now().isoformat(timespec="seconds")
    if rows is None:
        rows = backtest(tickers or _UNIVERSE, max_quarters=8).get("rows", [])
    rows = [x for x in rows if x.get("scores")]
    ev = sorted(rows, key=lambda x: x.get("period") or "")
    if len(ev) < 20 + MIN_TEST:
        return {"adopted": False, "reason": f"too few events ({len(ev)}) to fit+validate", "n": len(ev)}
    split = int(len(ev) * 0.7)
    train, test = ev[:split], ev[split:]
    if len(test) < MIN_TEST:
        return {"adopted": False, "reason": f"held-out set too small ({len(test)})", "n": len(ev)}

    fitted = _fit_logistic(train, FUNDAMENTAL)
    if not fitted:
        return {"adopted": False, "reason": "fit failed", "n": len(ev)}
    y = np.array([1.0 if x["actual_beat"] else 0.0 for x in test])
    p_fit = np.array([_logistic_prob(fitted, x["scores"]) for x in test])
    log = load_weights()["logistic"]
    p_cur = np.array([_logistic(log["intercept"] + log["gain"] * FUND_SPREAD * x["norm"]) for x in test])
    se_fit = (p_fit - y) ** 2
    se_cur = (p_cur - y) ** 2
    fitted_oos, current_oos = float(se_fit.mean()), float(se_cur.mean())

    # PAIRED ΔBrier with a BLOCK bootstrap by ticker (observations aren't independent — same
    # company/quarter cluster). Adopt ONLY if the 95% CI lower bound of (current − candidate)
    # squared-error is > 0 — i.e. the improvement is statistically meaningful, not a point
    # comparison on a reused test set. This is a real (if imperfect) guard against adaptive
    # overfitting; the DEFINITIVE validation remains the prospective forward vault (untouched
    # future contracts), NOT this rolling OOS split.
    diff = se_cur - se_fit                       # >0 means candidate better on that event
    syms = [x.get("symbol") for x in test]
    from collections import defaultdict
    blocks = defaultdict(list)
    for d_, s_ in zip(diff, syms):
        blocks[s_].append(d_)
    block_keys = list(blocks.keys())
    if len(block_keys) < MIN_BLOCKS:
        return {"adopted": False, "n": len(ev), "test_n": len(test),
                "reason": f"only {len(block_keys)} tickers in test set (<{MIN_BLOCKS}) — "
                          "bootstrap CI would be degenerate; kept current", "at": stamp}
    rng = np.random.default_rng(0)
    means = []
    for _ in range(2000):
        pick = rng.choice(block_keys, size=len(block_keys), replace=True)
        vals = np.concatenate([blocks[k] for k in pick])
        means.append(vals.mean())
    ci_low, ci_high = float(np.percentile(means, 2.5)), float(np.percentile(means, 97.5))
    beats = ci_low > 0
    result = {"adopted": beats, "n": len(ev), "train_n": len(train), "test_n": len(test),
              "fitted_oos_brier": round(fitted_oos, 4),
              "current_oos_brier": round(current_oos, 4) if current_oos is not None else None,
              "delta_brier_mean": round(float(diff.mean()), 4),
              "delta_brier_ci95": [round(ci_low, 4), round(ci_high, 4)],
              "n_ticker_blocks": len(block_keys), "at": stamp}
    if beats:
        # refit on ALL events for the adopted model (more data), keep the OOS proof in the file
        full = _fit_logistic(ev, FUNDAMENTAL) or fitted
        full.update({"adopted_at": stamp, "fitted_oos_brier": round(fitted_oos, 4),
                     "current_oos_brier": round(current_oos, 4), "test_n": len(test)})
        _FITTED.write_text(json.dumps(full, indent=2))
        result["reason"] = (f"ΔBrier 95% CI {result['delta_brier_ci95']} > 0 (blocked by "
                            f"{result['n_ticker_blocks']} tickers) -> ADOPTED")
    else:
        result["reason"] = (f"ΔBrier 95% CI {result['delta_brier_ci95']} not clearly > 0 -> "
                            f"kept current (no statistically-meaningful improvement)")
    return result


if __name__ == "__main__":
    import warnings
    warnings.filterwarnings("ignore")
    res = run()
    print(json.dumps(res, indent=2))
    if res.get("adopted"):
        try:
            from tools.daily_improve import _notify
            _notify("Earnings Edge — model re-fitted (OOS-gated)",
                    f"Auto-fitter adopted a new weight model: OOS Brier "
                    f"{res['fitted_oos_brier']} < {res['current_oos_brier']} (beat current on "
                    f"held-out data, {res['test_n']} events).")
        except Exception:
            pass

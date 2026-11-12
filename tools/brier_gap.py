"""Is the model's Brier advantage over the market price real, or sampling noise?

The README headline compares two numbers: the model's Brier score and the market's, over
the same resolved bets. A point difference says nothing about whether it would survive a
different sample, and the gap has already moved in both directions as the vault grew
(0.0135 at n=298, 0.0096 at n=322, 0.0102 at n=326). This puts an interval on it.

METHOD. Per bet, d = (market - outcome)^2 - (model - outcome)^2, positive when the model is
closer. The statistic is mean(d). Bets on the same company are not independent (a company
appears in several contracts), so the bootstrap resamples whole TICKERS, the same block
scheme tools/autofit.py uses for its adoption gate. 10,000 resamples, fixed seed.

READ IT HONESTLY. The interval is on the Brier gap only. It says nothing about whether the
model can make money (it cannot; see the README) and a wide interval means "not
distinguishable from zero at this sample size", not "the model is worse".

Run: .venv/bin/python -m tools.brier_gap
"""
from __future__ import annotations

from typing import Any, Dict

import numpy as np

from tools import make_figures

N_BOOT = 10_000
SEED = 7


def paired_gap(y, m, q, tickers, n_boot: int = N_BOOT, seed: int = SEED) -> Dict[str, Any]:
    """Block-bootstrap (by ticker) interval for mean[(q-y)^2 - (m-y)^2]."""
    y, m, q, tickers = (np.asarray(a) for a in (y, m, q, tickers))
    if len(y) < 2:
        return {"n": int(len(y)), "status": "insufficient_data"}
    d = (q - y) ** 2 - (m - y) ** 2
    groups = {t: np.where(tickers == t)[0] for t in set(tickers.tolist())}
    keys = list(groups)
    rng = np.random.default_rng(seed)
    boots = np.empty(n_boot)
    for i in range(n_boot):
        pick = rng.integers(0, len(keys), len(keys))
        boots[i] = d[np.concatenate([groups[keys[j]] for j in pick])].mean()
    low, high = np.percentile(boots, [2.5, 97.5])
    return {
        "status": "ok",
        "n": int(len(y)),
        "tickers": len(keys),
        "model_brier": float(np.mean((m - y) ** 2)),
        "market_brier": float(np.mean((q - y) ** 2)),
        "base_rate_brier": float(np.mean((y.mean() - y) ** 2)),
        "gap": float(d.mean()),
        "ci_low": float(low),
        "ci_high": float(high),
        "p_gap_le_zero": float(np.mean(boots <= 0)),
        "significant": bool(low > 0),
        "n_boot": n_boot,
        "seed": seed,
    }


def main() -> int:
    a = make_figures.scored_arrays()
    r = paired_gap(a["y"], a["m"], a["q"], a["tickers"])
    if r["status"] != "ok":
        print(f"not enough resolved bets ({r['n']})")
        return 1
    print(f"n = {r['n']} bets across {r['tickers']} tickers")
    print(f"Brier: model {r['model_brier']:.4f}  market {r['market_brier']:.4f}  "
          f"base-rate-only {r['base_rate_brier']:.4f}")
    print(f"gap (market - model): {r['gap']:+.4f}   95% CI [{r['ci_low']:+.4f}, {r['ci_high']:+.4f}]")
    print(f"share of resamples where the model is no better: {r['p_gap_le_zero']:.1%}")
    print("verdict:", "advantage is statistically distinguishable from zero" if r["significant"]
          else "NOT distinguishable from zero at this sample size")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

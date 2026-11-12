"""Tests for the Brier-gap significance tool.

An interval is only worth publishing if the code producing it can be shown to behave: report
a real advantage when one exists, report none when there is none, and not be fooled by
repeated bets on the same company. Each test pins one way it could silently mislead.

Run: .venv/bin/python -m tools.test_brier_gap
"""
from __future__ import annotations

import numpy as np

from tools import brier_gap as B


def _check(name, cond, detail=""):
    print(f"  {'PASS' if cond else 'FAIL'}  {name}" + (f"  — {detail}" if detail else ""))
    return bool(cond)


def _world(n, skill, seed=1):
    """Outcomes drawn from a true probability; the model sees it with noise `skill`, the market with more."""
    rng = np.random.default_rng(seed)
    p = rng.uniform(0.15, 0.9, n)
    y = (rng.uniform(size=n) < p).astype(float)
    m = np.clip(p + rng.normal(0, 0.03 * skill, n), 0.02, 0.98)
    q = np.clip(p + rng.normal(0, 0.20, n), 0.02, 0.98)
    return y, m, q, np.array([f"T{i}" for i in range(n)])


def test_real_advantage_is_detected():
    """A model that is far closer to the truth than the market must come out significant."""
    y, m, q, t = _world(600, skill=1)
    r = B.paired_gap(y, m, q, t, n_boot=2000)
    return _check("gap positive and interval excludes zero", r["gap"] > 0 and r["significant"],
                  f"gap {r['gap']:+.4f} CI [{r['ci_low']:+.4f}, {r['ci_high']:+.4f}]")


def test_identical_forecasts_have_no_advantage():
    """Model == market must give a zero gap and must never be called significant."""
    y, _, q, t = _world(400, skill=1)
    r = B.paired_gap(y, q.copy(), q, t, n_boot=2000)
    ok = _check("gap is exactly zero", abs(r["gap"]) < 1e-12)
    ok &= _check("not significant", r["significant"] is False)
    return ok


def test_a_worse_model_is_not_reported_as_better():
    """Swapping model and market flips the sign; the lower bound can then never exceed zero."""
    y, m, q, t = _world(600, skill=1)
    r = B.paired_gap(y, q, m, t, n_boot=2000)
    return _check("worse model: negative gap, not significant", r["gap"] < 0 and not r["significant"],
                  f"gap {r['gap']:+.4f}")


def _clustered_world(seed, groups=12, per_group=50):
    """Bets grouped by company, where the MARKET is wrong in a shared way within a company
    (a company-level pricing error), so the per-bet score differences are correlated."""
    rng = np.random.default_rng(seed)
    n = groups * per_group
    gid = np.repeat(np.arange(groups), per_group)
    p = rng.uniform(0.15, 0.9, n)
    y = (rng.uniform(size=n) < p).astype(float)
    shared = rng.normal(0, 0.15, groups)[gid]
    m = np.clip(p + rng.normal(0, 0.03, n), 0.02, 0.98)
    q = np.clip(p + shared + rng.normal(0, 0.05, n), 0.02, 0.98)
    return y, m, q, gid


def test_correlated_bets_on_one_company_widen_the_interval():
    """When bets on a company share an error, treating them as independent understates the
    uncertainty; resampling whole tickers must give a wider interval than treating each bet alone."""
    wider = 0
    for seed in range(5):
        y, m, q, gid = _clustered_world(seed)
        naive = B.paired_gap(y, m, q, np.array([f"B{i}" for i in range(len(y))]), n_boot=2000)
        block = B.paired_gap(y, m, q, np.array([f"G{g}" for g in gid]), n_boot=2000)
        wider += (block["ci_high"] - block["ci_low"]) > (naive["ci_high"] - naive["ci_low"])
    return _check("block interval wider than naive in every one of 5 worlds", wider == 5, f"{wider}/5")


def test_deterministic_and_degenerate_inputs_do_not_crash():
    """Same seed, same answer; one bet returns insufficient_data instead of a fake interval."""
    y, m, q, t = _world(120, skill=1)
    a, b = B.paired_gap(y, m, q, t, n_boot=500), B.paired_gap(y, m, q, t, n_boot=500)
    ok = _check("reproducible", a["ci_low"] == b["ci_low"] and a["ci_high"] == b["ci_high"])
    ok &= _check("single bet is insufficient_data",
                 B.paired_gap([1.0], [0.5], [0.5], ["A"])["status"] == "insufficient_data")
    return ok


def main():
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    print(f"brier-gap significance — {len(tests)} regression tests\n")
    results = []
    for t in tests:
        print(f"{t.__name__}  ({(t.__doc__ or '').strip().splitlines()[0]})")
        results.append(bool(t()))
        print()
    passed = sum(results)
    print(f"{passed}/{len(results)} test groups passed")
    return 0 if passed == len(results) else 1


if __name__ == "__main__":
    raise SystemExit(main())

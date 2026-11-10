"""Tests for the validation harness.

Each test is a REGRESSION TEST FOR A REAL FAILURE. The names say which. If one of these
starts failing, the harness has lost the ability to catch the mistake that produced the
retracted "EDGE CONFIRMED" of 2026-10-30.

Run: .venv/bin/python -m tools.test_validation
"""
from __future__ import annotations

import tempfile
import numpy as np

from tools import validation as V


def _check(name, cond, detail=""):
    print(f"  {'PASS' if cond else 'FAIL'}  {name}" + (f"  — {detail}" if detail else ""))
    return bool(cond)


def test_contract_cost():
    """A NO bet on a hot market is a LONGSHOT PURCHASE, not a contrarian call."""
    ok = True
    ok &= _check("YES at 0.80 costs 0.80", abs(V.contract_cost("YES", 0.80) - 0.80) < 1e-9)
    ok &= _check("NO at 0.80 costs 0.20 (longshot!)",
                 abs(V.contract_cost("NO", 0.80) - 0.20) < 1e-9)
    ok &= _check("NO at 0.80 is below the favorite threshold",
                 V.contract_cost("NO", 0.80) < V.FAVORITE_COST)
    return ok


def test_fill_price_never_the_mid():
    """Scoring at the mid gifts half the spread on every bet."""
    ok = True
    ok &= _check("YES lifts the ask", V.fill_price("YES", 0.50, bid=0.40, ask=0.60) == 0.60)
    ok &= _check("NO pays 1-bid", abs(V.fill_price("NO", 0.50, bid=0.40, ask=0.60) - 0.60) < 1e-9)
    ok &= _check("falls back to mid when no book", V.fill_price("YES", 0.50) == 0.50)
    wide = V.fill_price("YES", 0.695, bid=0.58, ask=0.81)
    ok &= _check("23c spread: pays 0.81 not 0.695", wide == 0.81,
                 f"the EXPE case — mid flattered the edge by {0.81 - 0.695:.3f}")
    return ok


def test_executable_unknown_is_not_true():
    """Unknown must be neither evidence nor failure."""
    ok = True
    ok &= _check("no book data -> None", V.executable(None, None) is None)
    ok &= _check("zero size -> False", V.executable(0, 0) is False)
    ok &= _check("$250 of size -> True", V.executable(250, None) is True)
    ok &= _check("$9 book -> False", V.executable(9, 9) is False, "below MIN_BOOK_USD")
    return ok


def test_dedup_independence():
    """The SBUX failure: one print counted twice inflated mean AND faked sample size."""
    bets = [
        {"ticker": "SBUX", "earnings_date": "2026-10-29", "outcome": "eps_beat", "pnl": 28.39},
        {"ticker": "SBUX", "earnings_date": "2026-10-29", "outcome": "eps_strike", "pnl": 28.39},
        {"ticker": "AAPL", "earnings_date": "2026-10-30", "outcome": "eps_beat", "pnl": -1.0},
    ]
    order = {"eps_beat": 0, "eps_strike": 1}
    out = V.dedup_by_group(bets, key=lambda b: (b["ticker"], b["earnings_date"]),
                           prefer=lambda b: order.get(b["outcome"], 9))
    ok = _check("3 rows -> 2 independent events", len(out) == 2)
    ok &= _check("kept the preferred outcome",
                 any(b["outcome"] == "eps_beat" and b["ticker"] == "SBUX" for b in out))
    return ok


def test_power_requirement():
    """30 bets cannot resolve a 2.6% edge at any realistic variance."""
    ok = True
    # a range, not an exact integer: required_n depends on the sampled SD to 3 decimals,
    # so pinning the literal makes the test brittle rather than meaningful
    ok &= _check("favorites SD ~0.65 -> ~2,400", 2300 <= V.required_n(0.65, 0.026) <= 2500,
                 f"got {V.required_n(0.65, 0.026):,}")
    ok &= _check("longshots SD 3.74 -> unmeasurable", V.required_n(3.74, 0.026) > 70000,
                 f"{V.required_n(3.74, 0.026):,} bets")
    ok &= _check("required_n rises with variance", V.required_n(2.0) > V.required_n(0.5))
    return ok


def test_tail_driven_mean_is_rejected():
    """The exact 2026-10-30 shape: a few giant wins carrying everything."""
    pnls = [-1.0] * 95 + [30.0, 28.0, 28.0, 13.0, 13.0]
    st = V.robust_stats(pnls, seed=0)
    v = V.verdict(st)
    ok = _check("mean is positive", st["mean"] > 0, f"{st['mean']:+.3f}")
    ok &= _check("ex-top-5 is negative", st["mean_ex_top5"] < 0, f"{st['mean_ex_top5']:+.3f}")
    ok &= _check("top-5 share is enormous", (st["top5_share"] or 0) > 0.8,
                 f"{st['top5_share']:.0%} of all P&L")
    ok &= _check("verdict is NOT 'EDGE CONFIRMED'", not v.startswith("EDGE CONFIRMED"),
                 v[:60])
    return ok


def test_underpowered_beats_significant():
    """A tight CI on a small sample must NOT read as an edge."""
    rng = np.random.default_rng(7)
    pnls = list(rng.normal(0.30, 0.65, 60))       # "significant" but far under n_needed
    st = V.robust_stats(pnls, seed=1)
    v = V.verdict(st)
    ok = _check("CI excludes zero", (st["ci95"][0] or -9) > 0, f"CI {st['ci95']}")
    ok &= _check("still reported UNDERPOWERED", v.startswith("UNDERPOWERED"), v[:70])
    return ok


def test_genuine_edge_can_still_pass():
    """The harness must not be unfalsifiable in the other direction."""
    rng = np.random.default_rng(3)
    pnls = list(rng.normal(0.10, 0.30, 4000))     # real, powered, not tail-driven
    st = V.robust_stats(pnls, seed=2)
    v = V.verdict(st)
    ok = _check("powered", st["powered"] is True, f"n={st['n']} needed={st['n_needed']:,}")
    ok &= _check("verdict is EDGE CONFIRMED", v.startswith("EDGE CONFIRMED"), v[:60])
    return ok


def test_registry_blocks_backward_scoring():
    """A rule found by searching cannot be scored on the data that produced it."""
    with tempfile.NamedTemporaryFile(suffix=".json", delete=False) as fh:
        path = fh.name
    r = V.Registry(path)
    ok = _check("unregistered rule is not registered", not r.is_registered("dredged_v1"))
    r.register({"id": "real_v1", "claim": "c", "success_criterion": "s",
                "kill_criterion": "k", "scoring_starts_at": "2026-11-05"})
    ok &= _check("registered rule is found", r.is_registered("real_v1"))
    ok &= _check("scoring starts at registration",
                 r.scoring_starts_at("real_v1") == "2026-11-05")
    try:
        r.register({"id": "bad", "claim": "c"})
        ok &= _check("rejects unfalsifiable hypothesis", False)
    except ValueError:
        ok &= _check("rejects unfalsifiable hypothesis", True, "missing success criterion")
    try:
        r.register({"id": "real_v1", "claim": "c", "success_criterion": "s2",
                    "kill_criterion": "k", "scoring_starts_at": "2026-11-05"})
        ok &= _check("rejects amending a live hypothesis", False)
    except ValueError:
        ok &= _check("rejects amending a live hypothesis", True)
    total = r.log_trial("2026-11-03", 20, "subgroup sweep")
    ok &= _check("trial counter accumulates", total == 20, f"total={total}")
    ok &= _check("deflation note scales with trials", "20 search trials" in r.deflation_note())
    return ok


def test_assess_reports_exclusions():
    """A verdict whose exclusions are invisible is not auditable."""
    bets = [
        {"id": 1, "executable": True, "pnl": 0.5, "t": "A", "d": "x"},
        {"id": 2, "executable": None, "pnl": 9.0, "t": "B", "d": "y"},   # unknown book
        {"id": 3, "executable": True, "pnl": -1.0, "t": "A", "d": "x"},  # duplicate event
    ]
    out = V.assess(bets, key=lambda b: (b["t"], b["d"]))
    ok = _check("unknown-book bet excluded", out["excluded"]["not_executable_or_unknown"] == 1)
    ok &= _check("duplicate event excluded", out["excluded"]["duplicate_events"] == 1)
    ok &= _check("one bet scored", out["n_scored"] == 1, f"{out['n_scored']} of {out['n_input']}")
    return ok


def test_refusal_is_not_a_bet():
    """The system refused; the scorer used to bet anyway.

    Scorers fell back to `side = row["side"] or ("YES" if model_prob >= q else "NO")`,
    which invents the side the system explicitly declined to take. Three resolved rows
    reading "market resolved / no live order book — nothing tradeable" were being booked
    as real P&L (found 2026-11-10). A refusal must score as nothing at all, not as a
    coin-flip guess at what we would have done.
    """
    ok = _check("explicit YES is a bet", V.bet_side({"side": "YES"}) == "YES")
    ok &= _check("explicit NO is a bet", V.bet_side({"side": "NO"}) == "NO")
    ok &= _check("blank side is NOT a bet", V.bet_side({"side": ""}) is None)
    ok &= _check("missing side is NOT a bet", V.bet_side({}) is None)
    ok &= _check("None side is NOT a bet", V.bet_side({"side": None}) is None)
    ok &= _check("whitespace side is NOT a bet", V.bet_side({"side": "   "}) is None)
    ok &= _check("literal 'None' is NOT a bet", V.bet_side({"side": "None"}) is None)
    ok &= _check("lowercase is normalised", V.bet_side({"side": "yes"}) == "YES")
    # The refusal must not be rescued by a confident model_prob sitting beside it.
    refusal = {"side": "", "model_prob": 0.95, "market_prob": 0.40,
               "recommendation": "market resolved / no live order book — nothing tradeable"}
    ok &= _check("a confident model_prob does not resurrect a refusal",
                 V.bet_side(refusal) is None)
    return ok


def main():
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    print(f"validation harness — {len(tests)} regression tests\n")
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

"""Tests for the paper market maker's scorer.

`maker.score_quotes()` produces the single number the project now hangs on: whether a
resting quote gets filled BECAUSE we were wrong. Every other route to a money edge is
closed or capped, so this verdict decides the thing.

It had no test. That is the gap these close, and the reason is 2026-11-10: in one session
the fill detector read 0 fills when the truth was 0.40, and before that a verdict function
announced "both legs point the predicted way" off a reversal at t = -0.23. Instruments on
this project break in the direction the author expects. Writing these now, while there is
almost no real data to flatter, is the only honest time to write them.

Each test is a regression test for a specific way the scorer could silently lie.

Run: .venv/bin/python -m tools.test_maker
"""
from __future__ import annotations

import csv
import tempfile
from pathlib import Path
from typing import Any, Dict, List, Optional

from tools import maker as M
from tools import validation as V


def _check(name, cond, detail=""):
    print(f"  {'PASS' if cond else 'FAIL'}  {name}" + (f"  — {detail}" if detail else ""))
    return bool(cond)


def _quote(ticker: str, side: str, fill_price: float, date: str,
           filled: str = "yes", resolved: str = "", pnl: str = "",
           outcome_actual: str = "", spread_earned: float = 0.04) -> Dict[str, Any]:
    """One row shaped exactly like the real quote log."""
    return {
        "quoted_at": f"{date}T09:00:00", "ticker": ticker, "outcome": "eps_beat",
        "slug": f"{ticker.lower()}-q-eps-{date}", "url": "", "earnings_date": date,
        "model_prob": 0.8, "mid": round(fill_price + spread_earned, 4),
        "best_bid": fill_price, "best_ask": fill_price + 2 * spread_earned,
        "book_spread": 2 * spread_earned, "depth_usd": 500.0,
        "side": side, "contract_cost_if_filled": fill_price if side == "YES" else 1 - fill_price,
        "our_bid": fill_price if side == "YES" else "",
        "our_ask": "" if side == "YES" else fill_price,
        "edge_vs_fair": 0.03, "size_usd": 100.0, "scar": 50, "eps_basis": "non_gaap",
        "rationale": "test", "resolved": resolved, "filled": filled,
        "fill_price": fill_price, "spread_earned": spread_earned,
        "outcome_actual": outcome_actual, "pnl": pnl, "notes": "test",
        "fill_method": "price_path",
    }


def _with_log(rows: List[Dict[str, Any]], resolver):
    """Run score_quotes against a throwaway quote log, never the real vault."""
    tmp = Path(tempfile.mkdtemp()) / "quotes.csv"
    with open(tmp, "w", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=M.FIELDS)
        w.writeheader()
        for r in rows:
            w.writerow({k: r.get(k, "") for k in M.FIELDS})
    original = M.QUOTE_LOG
    M.QUOTE_LOG = tmp
    try:
        out = M.score_quotes(resolver)
        written = list(csv.DictReader(open(tmp)))
        return out, written
    finally:
        M.QUOTE_LOG = original


def test_cost_arithmetic_by_side():
    """A sign error here silently inverts every verdict the project produces.

    Buying YES at a fill of 0.60 costs 0.60. Selling into someone -- resting an ASK at 0.60
    and being lifted -- leaves us holding NO, which costs 1 - 0.60 = 0.40. If the scorer
    used the fill price for both, every NO fill would be priced as its own complement and
    the adverse-selection number would be garbage in a way no eyeball would catch.
    """
    yes_win, _ = _with_log([_quote("AAA", "YES", 0.60, "2026-10-01")], lambda q: "YES")
    yes_lose, _ = _with_log([_quote("BBB", "YES", 0.60, "2026-10-02")], lambda q: "NO")
    no_win, _ = _with_log([_quote("CCC", "NO", 0.60, "2026-10-03")], lambda q: "NO")

    exp_yes_win = V.payoff(0.60, True)      # cost 0.60
    exp_no_win = V.payoff(0.40, True)       # cost 1 - 0.60
    got_yes = yes_win["assessment"]["stats"]["mean"]
    got_no = no_win["assessment"]["stats"]["mean"]

    # robust_stats reports the mean rounded to 3dp and score_quotes rounds each P&L to
    # 4dp, so the comparison is made at the precision the scorer actually reports. An
    # assertion tighter than the rounding tests the rounding, not the arithmetic.
    tol = 5e-4
    ok = _check("YES fill priced at the fill price",
                abs(got_yes - exp_yes_win) < tol, f"{got_yes:+.4f} vs {exp_yes_win:+.4f}")
    ok &= _check("NO fill priced at 1 - fill price",
                 abs(got_no - exp_no_win) < tol, f"{got_no:+.4f} vs {exp_no_win:+.4f}")
    ok &= _check("the two sides are NOT priced the same",
                 abs(exp_yes_win - exp_no_win) > 0.1,
                 "a scorer that conflated them would pass the first two checks")
    ok &= _check("a loss costs the whole stake",
                 abs(yes_lose["assessment"]["stats"]["mean"] + 1.0) < 1e-9)
    return ok


def test_adverse_selection_is_detected():
    """Earn the spread on every fill, get filled only when wrong -> must NOT read as an edge.

    This is the exact failure the experiment exists to find: a maker who collects 4c of
    spread each time and is picked off by counterparties who know more. Spread earned is
    positive on every row, so any scorer that reported `mean_spread_earned` as the headline
    would call this a success. The P&L verdict has to override it.
    """
    rows = [_quote(f"T{i:02d}", "YES", 0.60, f"2026-09-{i:02d}", spread_earned=0.04)
            for i in range(1, 32)]
    out, _ = _with_log(rows, lambda q: "NO")          # filled, then always wrong
    st = out["assessment"]["stats"]

    ok = _check("spread was earned on every fill", out["mean_spread_earned"] == 0.04)
    ok &= _check("but realised P&L is negative", st["mean"] < 0, f"{st['mean']:+.3f}/$1")
    ok &= _check("verdict is NOT 'EDGE CONFIRMED'",
                 "EDGE CONFIRMED" not in out["assessment"]["verdict"],
                 out["assessment"]["verdict"][:72])
    ok &= _check("enough resolved fills to be judged at all", out["resolved_fills"] >= 30)
    return ok


def test_a_real_maker_edge_can_still_pass():
    """The scorer must be falsifiable in BOTH directions.

    A test that can only ever say "no edge" is worthless -- it would have "caught" the
    2026-10-30 false positive by accident while being incapable of ever recognising the
    real thing. A maker filled mostly when RIGHT, at favourite prices with low variance,
    must be able to clear the bar.
    """
    rows = []
    for i in range(1, 61):
        rows.append(_quote(f"W{i:02d}", "YES", 0.55, f"2026-08-{(i % 28) + 1:02d}"))
    # 80% of fills resolve our way -- a genuinely good maker, not a tail-driven fluke.
    outcomes = {q["ticker"]: ("YES" if i % 5 else "NO") for i, q in enumerate(rows)}
    out, _ = _with_log(rows, lambda q: outcomes[q["ticker"]])
    st = out["assessment"]["stats"]

    ok = _check("realised P&L is positive", st["mean"] > 0, f"{st['mean']:+.3f}/$1")
    ok &= _check("survives deleting the 5 best wins", (st.get("mean_ex_top5") or 0) > 0,
                 f"ex-top5 {st.get('mean_ex_top5')}")
    ok &= _check("not rejected out of hand",
                 "DISPROVEN" not in out["assessment"]["verdict"],
                 out["assessment"]["verdict"][:72])
    return ok


def test_unresolved_and_unfilled_are_not_scored():
    """A pending event is not a result, and an unfilled quote is not a position.

    The vault has already been inflated once by counting rows that were not bets. A quote
    whose earnings date has not passed returns None from the resolver, and a quote that
    never filled has no position at all. Either one scored as a zero or a loss would bias
    the mean toward whatever the rest of the sample happens to say.
    """
    rows = [
        _quote("FILL", "YES", 0.60, "2026-09-01"),                       # scoreable
        _quote("PEND", "YES", 0.60, "2027-06-01"),                       # not reported yet
        _quote("OPEN", "YES", 0.60, "2026-09-03", filled=""),            # never filled
    ]
    out, written = _with_log(rows, lambda q: None if q["ticker"] == "PEND" else "YES")

    ok = _check("only the filled quotes count as fills", out["filled"] == 2,
                f"filled={out['filled']} of 3 rows")
    ok &= _check("the pending event is not scored", out["resolved_fills"] == 1,
                 f"resolved_fills={out['resolved_fills']}")
    ok &= _check("the unfilled quote has no P&L",
                 all(not r["pnl"] for r in written if r["ticker"] == "OPEN"))
    ok &= _check("the pending quote has no P&L",
                 all(not r["pnl"] for r in written if r["ticker"] == "PEND"))
    ok &= _check("fill rate is over ALL quotes, not just filled ones",
                 abs(out["fill_rate"] - 2 / 3) < 5e-4, f"{out['fill_rate']} (reported to 3dp)")
    return ok


def test_already_resolved_rows_are_not_rescored():
    """Re-running the daily job must not move a settled number.

    score_quotes runs every day over the whole log. If it recomputed rows it had already
    settled, a later change to the resolver or the haircut would silently rewrite history,
    and the 2026-10-30 lesson is precisely that history must not be rewritten in the
    direction of the current hypothesis.
    """
    settled = _quote("DONE", "YES", 0.60, "2026-09-01",
                     resolved="yes", outcome_actual="NO", pnl="-1.0")
    # The resolver now claims the opposite. The stored P&L must not change.
    out, written = _with_log([settled], lambda q: "YES")
    row = written[0]
    ok = _check("stored P&L untouched", row["pnl"] == "-1.0", f"pnl={row['pnl']}")
    ok &= _check("stored outcome untouched", row["outcome_actual"] == "NO")
    ok &= _check("still counted once", out["resolved_fills"] == 1)
    return ok


def test_duplicate_events_count_once():
    """Two fills on one earnings print are one piece of evidence, not two.

    Double-counted events are what faked the sample size behind the retracted result: a
    single +28.39 win appeared twice, which inflated the mean AND tightened the confidence
    interval. A maker can easily rest quotes on several contracts for the same print.
    """
    rows = [_quote("SAME", "YES", 0.60, "2026-09-01"),
            _quote("SAME", "YES", 0.60, "2026-09-01")]
    rows[1]["slug"] = "same-q-eps-variant"
    out, _ = _with_log(rows, lambda q: "YES")
    ok = _check("both rows recorded as fills", out["filled"] == 2)
    ok &= _check("but only ONE scored", out["assessment"]["n_scored"] == 1,
                 f"n_scored={out['assessment']['n_scored']} of {out['assessment']['n_input']}")
    ok &= _check("the exclusion is reported, not silent",
                 out["assessment"]["excluded"].get("duplicate_events") == 1)
    return ok


def test_thirty_fills_is_not_enough_on_its_own():
    """The registered bar is 30 resolved fills AND a CI above zero AND tail robustness.

    The retraction's root cause was a bar that named a sample size without naming the
    effect it could detect. If a handful of giant wins can carry the verdict, the scorer
    must say so rather than report the mean.
    """
    rows = [_quote(f"L{i:02d}", "YES", 0.05, f"2026-07-{(i % 28) + 1:02d}")
            for i in range(1, 36)]
    # 5 enormous longshot wins, 30 total losses -- the 2026-10-30 shape.
    winners = {f"L{i:02d}" for i in range(1, 6)}
    out, _ = _with_log(rows, lambda q: "YES" if q["ticker"] in winners else "NO")
    st = out["assessment"]["stats"]
    ok = _check("the raw mean looks positive", st["mean"] > 0, f"{st['mean']:+.3f}/$1")
    ok &= _check("but it does not survive deleting the top 5",
                 (st.get("mean_ex_top5") or 0) < 0, f"ex-top5 {st.get('mean_ex_top5')}")
    ok &= _check("verdict is NOT 'EDGE CONFIRMED'",
                 "EDGE CONFIRMED" not in out["assessment"]["verdict"],
                 out["assessment"]["verdict"][:72])
    return ok


def test_fills_are_treated_as_executable():
    """An assumption worth stating out loud rather than leaving in the code.

    score_quotes marks every filled quote `executable: True`, which bypasses the gate that
    excluded 303 legacy rows for having no book snapshot. The justification is that a fill
    IS the evidence of executability -- but it was never written down or checked, and an
    unexamined True in a gate is how fiction got in last time.
    """
    out, _ = _with_log([_quote("EXEC", "YES", 0.60, "2026-09-01")], lambda q: "YES")
    ok = _check("a filled quote is not excluded as unfillable",
                out["assessment"]["excluded"].get("not_executable_or_unknown") == 0)
    ok &= _check("and it does reach the scored sample",
                 out["assessment"]["n_scored"] == 1)
    return ok


def main():
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    print(f"paper-maker scorer — {len(tests)} regression tests\n")
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

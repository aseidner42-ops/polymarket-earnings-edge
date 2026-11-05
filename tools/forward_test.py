"""Forward-testing: score logged live predictions after earnings resolve.

Rationale: Kelly et al. (2016) already established the over-extrapolation effect
in-sample, and our backtest (v1.1) only calibrates OUR implementation on the
shallow free history. The honest ongoing test is FORWARD: log each live
model-vs-Polymarket prediction now (edge_log), then after the print compare the
model's probability to what actually happened. This accumulates a real,
out-of-sample track record — including realized edge vs the market price.

v1.2 resolves EPS-type outcomes automatically (Finnhub gives realized EPS
surprise). Revenue / stock_up resolution is left manual (no free history) and
flagged. No look-ahead: only rows whose earnings date has passed are scored.
"""
from __future__ import annotations

import csv
from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional

import numpy as np

from tools import finnhub_client as fh
from tools.config import EDGE_LOG_PATH
from tools.edge_log import FIELDS


# ---------------------------------------------------------------------------
# P&L SCORING INTEGRITY (2026-11-03). The "EDGE CONFIRMED" verdict of 2026-10-30
# turned out to be an artifact of three scoring defects, not a real edge:
#   1. DOUBLE-COUNTING — the same earnings event is logged under several outcome
#      rows (eps_beat + eps_strike + eps_surprise_over). SBUX 2026-10-29 counted
#      its +28.39 longshot win TWICE, CBRE its +7.38 twice. Those rows are the
#      same bet on the same print: not independent, so they inflate both the mean
#      and (worse) the CI by faking sample size. Now deduplicated per
#      (ticker, earnings_date).
#   2. DISPUTED RESOLUTIONS counted as clean wins (notes flagged
#      DISPUTED-RESOLUTION, n=8, mean +3.11/$1). Excluded.
#   3. PHANTOM-PRICE FLOOR TOO LOOSE — 0.02 let in q=0.030 / q=0.034 "fills"
#      paying 32:1. Books that thin have no executable size (today's scan shows
#      maxUSD=0.0 across the board), so a $1 fill there is fiction. Floor raised
#      to MIN_Q.
# With all three corrected the mean goes +0.337 -> +0.011/$1 (breakeven) and the
# bootstrap CI covers 0. Conclusion is robust to the floor (0.05/0.10/0.15 all
# land at ~0), so it does not hinge on that one constant.
# These gates now live in tools/validation.py — a strategy-agnostic harness extracted so
# the same rules can be pointed at anything, not just earnings. The names below are thin
# aliases kept so this module (and its callers) read unchanged. The reasoning for each
# gate, and the failure that produced it, is documented in validation.py.
from tools import validation as V

HAIRCUT = V.HAIRCUT
MIN_Q = V.MIN_PRICE          # outside this band a quote is a phantom, not a price
TAIL_K = V.TAIL_K            # drop the K biggest winners for the tail-robustness check
FAVORITE_COST = V.FAVORITE_COST
LONGSHOT_COST = V.LONGSHOT_COST

required_n = V.required_n
_cost = V.contract_cost
_fill_price = V.fill_price
_pnl_per_1 = V.pnl_per_1


def _executable(row: Dict[str, Any], min_usd: float = V.MIN_BOOK_USD) -> Optional[bool]:
    """Fillability gate over an edge_log CSV row. None = unknown (no book snapshot)."""
    return V.executable(row.get("max_usd"), row.get("depth_usd"), min_usd=min_usd)


def _dedup_events(bets: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """One bet per (ticker, earnings_date) — several outcome rows on one print are the
    same wager, so counting them separately breaks independence."""
    order = {"eps_beat": 0, "eps_strike": 1}
    return V.dedup_by_group(
        bets,
        key=lambda b: (b.get("ticker"), b.get("earnings_date")),
        prefer=lambda b: order.get(b.get("outcome"), 9))


def _robust_pnl(pnls: List[float], seed: int = 0) -> Dict[str, Any]:
    st = V.robust_stats(pnls, seed=seed)
    if "n_needed" in st:                 # keep the older, explicit key for callers/UI
        st["n_needed_at_2.6pct"] = st["n_needed"]
    return st


def _verdict(st: Dict[str, Any]) -> str:
    return V.verdict(st, tail_k=TAIL_K)


def _resolve_eps(ticker: str, earnings_date: str, threshold: Optional[float],
                 stored_consensus: Optional[float] = None,
                 strike: Optional[float] = None, strike_op: str = ">") -> Optional[bool]:
    """Did the EPS outcome resolve YES? Prefers yfinance REAL announcement dates + reported EPS,
    matched to the NEAREST announcement within ±7 days; falls back to Finnhub, matching the
    NEAREST reported quarter whose approximate announce date is within ±25 days AND in the past.

    POINT-IN-TIME (critique #2, 2026-09-21): if `stored_consensus` is given (the consensus we
    LOGGED the prediction against) and there is no explicit % threshold, resolution compares the
    reported ACTUAL to that FROZEN consensus — not a re-fetched, possibly-revised one. This stops
    the moving-target flip that mis-scored GME (the same date re-resolved beat->miss as Finnhub's
    stored estimate changed under it). The reported actual is a fixed fact; the frozen consensus
    is what the bet was actually about.

    Returns None if not yet reported — NEVER resolves a future date (no look-ahead). NEAREST-record
    matching (not first-in-window) stops fiscal-vs-calendar quarter mismatches."""
    try:
        target = datetime.strptime(earnings_date, "%Y-%m-%d")
    except (TypeError, ValueError):
        return None
    now = datetime.now()
    if target.date() > now.date():          # print hasn't happened yet -> unresolvable
        return None
    thr = threshold or 0.0

    def _beat(sp, actual, est, surprise):
        if strike is not None and actual is not None:    # STRIKE contract: round(reported,2) op strike
            ra = round(float(actual), 2)
            if strike_op == ">=":
                return ra >= strike
            if strike_op == "<":
                return ra < strike
            if strike_op == "<=":
                return ra <= strike
            return ra > strike                           # '>' with equality=NO (a tie is not >)
        if thr and sp is not None:                       # explicit surprise-% contract
            return sp >= thr
        if stored_consensus is not None and actual is not None:
            return actual > float(stored_consensus)      # POINT-IN-TIME: vs the frozen consensus
        if actual is not None and est is not None:
            return actual > est
        if sp is not None:
            return sp > 0
        if surprise is not None:
            return surprise > 0
        return None

    # 1) yfinance real earnings history (precise dates + reported EPS) — NEAREST within ±7d.
    from tools import market_data as md
    best = None
    for e in md.get_earnings_history(ticker):
        if e.get("eps_actual") is None:
            continue
        d = abs((e["date"].date() - target.date()).days)
        if d <= 7 and (best is None or d < best[0]):
            best = (d, e)
    if best is not None:
        e = best[1]
        return _beat(e.get("surprise_pct"), e.get("eps_actual"), e.get("eps_estimate"), None)

    # 2) Finnhub fallback — NEAREST already-announced quarter within ±25d.
    best = None
    for s in fh.get_earnings_surprises(ticker, limit=12):
        if s.get("period") is None or s.get("surprise") is None:
            continue
        # point-in-time / strike: if we resolve against a frozen consensus OR a fixed strike, we MUST
        # have the reported actual — never fall back to a (revised) surprise sign.
        if (stored_consensus is not None or strike is not None) and s.get("actual") is None:
            continue
        try:
            approx = datetime.strptime(s["period"], "%Y-%m-%d") + timedelta(days=40)
        except ValueError:
            continue
        if approx > now:                    # quarter not reported yet -> skip
            continue
        d = abs((approx - target).days)
        if d <= 25 and (best is None or d < best[0]):
            best = (d, s)
    if best is not None:
        s = best[1]
        return _beat(s.get("surprisePercent"), s.get("actual"), s.get("estimate"), s.get("surprise"))
    return None


def score_log(apply: bool = True) -> Dict[str, Any]:
    """Resolve past EPS predictions in the edge log and report model-vs-market skill."""
    if not EDGE_LOG_PATH.exists():
        return {"note": "no edge_log.csv yet — run predict with --log first"}
    with open(EDGE_LOG_PATH, newline="") as fh_:
        rows = list(csv.DictReader(fh_))

    today = datetime.now()
    scored: List[Dict[str, Any]] = []
    for r in rows:
        if r.get("resolved"):  # already resolved
            if r.get("actual_outcome") in ("YES", "NO"):
                scored.append(r)
            continue
        ed = r.get("earnings_date") or ""
        try:
            passed = datetime.strptime(ed, "%Y-%m-%d") < today
        except ValueError:
            passed = False
        if not passed:
            continue
        if (r.get("outcome") or "").startswith("eps"):
            thr = float(r["threshold"]) if r.get("threshold") not in (None, "", "None") else None
            stored_cons = None
            _cv = r.get("consensus_eps_used")
            if _cv not in (None, "", "None"):
                try:
                    stored_cons = float(_cv)
                except ValueError:
                    stored_cons = None
            # STRIKE-GAP rows (outcome 'eps_strike') resolve on round(reported,2) > frozen strike,
            # NOT consensus-beat (pre-registered strike_gap_v1).
            _strike = None
            if r.get("outcome") == "eps_strike" and r.get("contract_strike") not in (None, "", "None"):
                try:
                    _strike = float(r["contract_strike"])
                except ValueError:
                    _strike = None
            if _strike is not None:
                res = _resolve_eps(r["ticker"], ed, None, strike=_strike, strike_op=">")
            else:
                res = _resolve_eps(r["ticker"], ed, thr, stored_consensus=stored_cons)
            if res is not None:
                r["resolved"] = "yes"
                r["actual_outcome"] = "YES" if res else "NO"
                scored.append(r)

    if apply and scored:
        with open(EDGE_LOG_PATH, "w", newline="") as fh_:
            w = csv.DictWriter(fh_, fieldnames=FIELDS)
            w.writeheader()
            # rewrite resolved rows merged back over originals (match by logged_at+ticker)
            key = lambda x: (x.get("logged_at"), x.get("ticker"), x.get("outcome"))
            res_map = {key(s): s for s in scored}
            for r in rows:
                w.writerow({k: res_map.get(key(r), r).get(k, "") for k in FIELDS})

    # Build the full vault view: every logged prediction with a status.
    def _f(v):
        try:
            return float(v)
        except (TypeError, ValueError):
            return None

    vault = []
    for r in rows:
        resolved = bool(r.get("resolved"))
        actual = r.get("actual_outcome") or ""
        mp = _f(r.get("model_prob"))
        status = actual if (resolved and actual) else "pending"
        correct = None
        if status in ("YES", "NO") and mp is not None:
            correct = (mp >= 0.5) == (status == "YES")
        # Your result vs actual result + difference explanation.
        lean = None if mp is None else ("beat/yes" if mp >= 0.5 else "miss/no")
        your_result = (f"{round(mp*100)}% ({lean})" if mp is not None else "–")
        if status == "pending":
            actual_result = "pending"
            difference = f"resolves after {r.get('earnings_date') or 'earnings'}"
        else:
            actual_result = "BEAT/YES" if status == "YES" else "MISS/NO"
            if correct:
                difference = "match"
            else:
                over = mp is not None and mp >= 0.5
                difference = (f"you leaned {lean}, actual {actual_result} — "
                              f"{'over' if over else 'under'}estimated the outcome")
        vault.append({
            "logged_at": (r.get("logged_at") or "")[:10],
            "ticker": r.get("ticker"), "outcome": r.get("outcome"),
            "earnings_date": r.get("earnings_date"),
            "model_prob": mp, "market_prob": _f(r.get("market_prob")),
            "side": r.get("side"), "status": status, "correct": correct,
            "notes": r.get("notes") or "",
            "eps_basis": r.get("eps_basis") or "",
            "your_result": your_result, "actual_result": actual_result,
            "difference": difference,
        })
    vault.sort(key=lambda v: (v["status"] != "pending", v["earnings_date"] or ""))

    resolved_rows = [v for v in vault if v["status"] in ("YES", "NO") and v["model_prob"] is not None]
    summary: Dict[str, Any] = {
        "logged": len(vault),
        "pending": sum(1 for v in vault if v["status"] == "pending"),
        "resolved": len(resolved_rows),
    }
    if resolved_rows:
        y = np.array([1.0 if v["status"] == "YES" else 0.0 for v in resolved_rows])
        mp = np.array([v["model_prob"] for v in resolved_rows])
        summary["model_accuracy"] = round(float(np.mean((mp >= 0.5) == (y == 1))), 3)
        summary["model_brier"] = round(float(np.mean((mp - y) ** 2)), 4)
        # Phantom/degenerate markets (price ~0 or ~1 = resolved/no real book) make the payout
        # ratios (1-q)/q and q/(1-q) explode and poison the P&L — exclude them (GME 2026-09-21,
        # floor raised to MIN_Q 2026-11-03), drop disputed resolutions, and deduplicate the
        # several outcome rows that share one earnings print. See the notes on MIN_Q above.
        mk = [v for v in resolved_rows
              if v["market_prob"] is not None and MIN_Q < v["market_prob"] < 1 - MIN_Q
              and "DISPUTED" not in (v.get("notes") or "").upper()
              and (v.get("eps_basis") or "") != "gaap"]
        mk = _dedup_events(mk)
        if mk:
            ym = np.array([1.0 if v["status"] == "YES" else 0.0 for v in mk])
            qm = np.array([v["market_prob"] for v in mk])
            pm = np.array([v["model_prob"] for v in mk])
            summary["market_brier"] = round(float(np.mean((qm - ym) ** 2)), 4)
            summary["model_brier_vs_market"] = round(float(np.mean((pm - ym) ** 2)), 4)
            summary["model_beats_market"] = summary["model_brier_vs_market"] < summary["market_brier"]
            # THE METRIC THAT PAYS: realized P&L per $1 staked, net of a fee/spread
            # haircut, with statistical significance + a "bet with the crowd" benchmark.
            _pnl = _pnl_per_1

            pnls, fav_pnls, wins = [], [], 0
            for v in mk:
                q = v["market_prob"]
                side = (v.get("side") or ("YES" if v["model_prob"] >= q else "NO"))
                wins += (side == v["status"])
                pnls.append(_pnl(side, q, v["status"]))
                fav = "YES" if q >= 0.5 else "NO"           # benchmark: bet with the crowd
                fav_pnls.append(_pnl(fav, q, v["status"]))

            st = _robust_pnl(pnls)
            n = st["n"]
            summary["bets"] = n
            summary["edge_hit_rate"] = round(wins / n, 3)
            summary["realized_pnl_per_$1"] = st["mean"]
            summary["pnl_median"] = st["median"]
            summary["pnl_ci95"] = st.get("ci95")
            summary["p_mean_le_0"] = st.get("p_mean_le_0")
            summary["mean_ex_top5"] = st.get("mean_ex_top5")
            summary["sd"] = st.get("sd")
            summary["n_needed_at_2.6pct"] = st.get("n_needed_at_2.6pct")
            summary["powered"] = st.get("powered")
            summary["top5_share"] = st.get("top5_share")
            summary["benchmark_favorite_pnl"] = round(float(np.mean(fav_pnls)), 3)
            summary["beats_crowd"] = st["mean"] > float(np.mean(fav_pnls))
            summary["verdict"] = _verdict(st)

    note = ("Vault empty — run PREDICT (it auto-logs) to start tracking."
            if not vault else
            f"{summary['pending']} pending, {summary['resolved']} resolved. "
            "EPS outcomes resolve automatically after the earnings date.")
    return {"summary": summary, "rows": vault, "note": note}


def edge_guard() -> Dict[str, Any]:
    """HONEST GUARD (FAST, offline — reads only ALREADY-resolved bets from the CSV, no network
    so it's safe to call on every page load). Decides whether flagged edges may be called
    'tradeable' yet, based on REALIZED forward P&L. Because the resolved bets show our biggest
    flagged edges (net_edge>=3%) are net-LOSING, nothing is called tradeable until: >=30 bets,
    overall 95% CI-low > 0 (EDGE CONFIRMED), AND the flagged subset isn't losing. Actual
    resolution/scoring happens in the daily job, not here."""
    import csv as _csv
    from tools.config import EDGE_LOG_PATH
    bets = []
    if EDGE_LOG_PATH.exists():
        for r in _csv.DictReader(open(EDGE_LOG_PATH)):
            if not r.get("resolved") or r.get("actual_outcome") not in ("YES", "NO"):
                continue
            try:
                q = float(r["market_prob"]); mp = float(r["model_prob"])
            except (TypeError, ValueError, KeyError):
                continue
            # phantom/degenerate or no-depth market -> exclude (GME 2026-09-21; floor raised
            # to MIN_Q 2026-11-03 after q~0.03 "fills" paying 32:1 faked an edge)
            if not (MIN_Q < q < 1 - MIN_Q):
                continue
            if "DISPUTED" in (r.get("notes") or "").upper():
                continue
            # A GAAP-resolving contract scores our ADJUSTED-EPS model against a GAAP outcome —
            # a different question. Counting it as model-vs-market skill (or P&L) is noise at
            # best (2026-11-03). Legacy rows have no basis recorded and are left as-is.
            if (r.get("eps_basis") or "") == "gaap":
                continue
            side = r.get("side") or ("YES" if mp >= q else "NO")
            try:
                ne = float(r["net_edge"])
            except (TypeError, ValueError, KeyError):
                ne = None
            def _nf(k):
                try:
                    return float(r[k])
                except (TypeError, ValueError, KeyError):
                    return None
            fill = _fill_price(side, q, _nf("yes_bid"), _nf("yes_ask"))
            bets.append({"ticker": r.get("ticker"), "earnings_date": r.get("earnings_date"),
                         "outcome": r.get("outcome"), "net_edge": ne,
                         "executable": _executable(r),
                         "cost": _cost(side, q),
                         "pnl": _pnl_per_1(side, q, r["actual_outcome"]),
                         # P&L at the price actually payable. For a YES bet filled at `fill`,
                         # a win returns (1-fill)/fill per $1; symmetric for NO.
                         "pnl_fill": ((1 - fill) / fill - HAIRCUT)
                                     if (side == r["actual_outcome"]) else -1.0})
    bets = _dedup_events(bets)          # one bet per earnings print — rows aren't independent
    all_pnls = [b["pnl"] for b in bets]
    flagged_pnls = [b["pnl"] for b in bets if b["net_edge"] is not None and b["net_edge"] >= 0.03]

    # EXECUTABLE TRACK — the only one that can ever confirm an edge. A bet in a book with
    # no size was never a bet. Legacy rows (no book snapshot) are 'unknown' and sit out.
    exe = [b for b in bets if b.get("executable") is True]
    exe_st = _robust_pnl([b["pnl_fill"] for b in exe], seed=2)
    unknown_n = sum(1 for b in bets if b.get("executable") is None)

    # FAVORITE track — bets where the contract we bought cost >= FAVORITE_COST. This is
    # the low-variance zone, and the only one where an edge of a realistic size is
    # detectable with a sample we could plausibly collect (see FAVORITE_COST note).
    fav = [b for b in bets if (b.get("cost") or 0) >= FAVORITE_COST]
    lng = [b for b in bets if (b.get("cost") or 0) < FAVORITE_COST]
    fav_st = _robust_pnl([b["pnl"] for b in fav], seed=4)
    lng_st = _robust_pnl([b["pnl"] for b in lng], seed=5)
    deep = [b for b in bets if (b.get("cost") or 0) < LONGSHOT_COST]

    all_st, flagged_st = _robust_pnl(all_pnls), _robust_pnl(flagged_pnls, seed=1)
    all_pnl, all_n = all_st.get("mean"), all_st.get("n", 0)
    all_ci_low = (all_st.get("ci95") or [None])[0]
    flagged_pnl, flagged_n = flagged_st.get("mean"), flagged_st.get("n", 0)
    flagged_ci_low = (flagged_st.get("ci95") or [None])[0]
    # The headline verdict comes from the EXECUTABLE track once it has enough bets;
    # until then the paper track can only ever say "not proven".
    if exe_st.get("n", 0) >= 30:
        verdict = _verdict(exe_st)
    elif all_n:
        verdict = (f"NOT MEASURABLE YET — {exe_st.get('n', 0)}/30 bets with a verified "
                   f"executable book ({unknown_n} legacy rows have no book snapshot). "
                   f"Paper P&L {all_st.get('mean')}/$1 is NOT evidence of an edge.")
    else:
        verdict = "no resolved bets yet"
    confirmed = verdict.startswith("EDGE CONFIRMED")
    flagged_losing = flagged_pnl is not None and flagged_pnl < 0
    tradeable_now = bool(confirmed and not flagged_losing)
    if tradeable_now:
        warning = ""
    elif verdict.startswith("NOT ROBUST"):
        warning = ("⚠ RESEARCH ONLY — the positive P&L is tail-driven: it disappears once the "
                   f"{TAIL_K} biggest wins are removed (mean falls to "
                   f"{all_st.get('mean_ex_top%d' % TAIL_K)}/$1). Not a tradeable edge.")
    elif flagged_losing:
        warning = (f"⚠ RESEARCH ONLY — flagged edges are currently net-LOSING "
                   f"({flagged_pnl:+.2f}/$1 over {flagged_n} resolved bets, net_edge≥3%). "
                   f"Do NOT treat as tradeable. The scanner still logs them to learn.")
    else:
        warning = ("⚠ RESEARCH ONLY — no proven edge yet (forward P&L not significantly > 0). "
                   "Flagged edges are candidates to track, not trade.")
    return {"tradeable_now": tradeable_now, "verdict": verdict,
            # Where our money actually goes, by the price of the contract bought.
            "by_cost": {
                "favorites": {"n": fav_st.get("n", 0), "mean": fav_st.get("mean"),
                              "sd": fav_st.get("sd"), "ci95": fav_st.get("ci95"),
                              "n_needed": fav_st.get("n_needed_at_2.6pct")},
                "longshots": {"n": lng_st.get("n", 0), "mean": lng_st.get("mean"),
                              "sd": lng_st.get("sd"), "ci95": lng_st.get("ci95"),
                              "n_needed": lng_st.get("n_needed_at_2.6pct")},
                "deep_longshots_under_10c": len(deep),
                "note": ("Cost = price of the contract BOUGHT (YES costs q, NO costs 1-q). "
                         "A NO bet on a 0.80 market is a 0.20 longshot purchase, not a "
                         "contrarian call on a favorite."),
            },
            "executable": {"n": exe_st.get("n", 0), "pnl_per_1": exe_st.get("mean"),
                           "ci95": exe_st.get("ci95"),
                           "mean_ex_top%d" % TAIL_K: exe_st.get("mean_ex_top%d" % TAIL_K),
                           "unknown_book_rows": unknown_n},
            "pnl_per_1": all_pnl, "n": all_n, "ci_low": all_ci_low,
            "mean_ex_top%d" % TAIL_K: all_st.get("mean_ex_top%d" % TAIL_K),
            "flagged_pnl_per_1": flagged_pnl, "flagged_n": flagged_n,
            "flagged_ci_low": flagged_ci_low, "warning": warning}


def scorecard() -> Dict[str, Any]:
    """OFFLINE forecast scorecard — reads only ALREADY-resolved rows, never the network.

    score_log() resolves pending predictions, which means API calls; calling it to render a
    page got the forecast endpoint rate-limited and 500ing after 67 seconds (2026-11-05).
    A page load must never trigger a resolution pass. Resolution belongs to the daily job.

    Returns the calibration numbers (the claim this project can actually support) together
    with the realised P&L (the claim it cannot) — deliberately in one object, so the first
    can never be shown without the second.
    """
    import csv as _csv
    rows: List[Dict[str, Any]] = []
    if EDGE_LOG_PATH.exists():
        rows = list(_csv.DictReader(open(EDGE_LOG_PATH)))

    def _f(v):
        try:
            return float(v)
        except (TypeError, ValueError):
            return None

    resolved = [r for r in rows
                if r.get("resolved") and r.get("actual_outcome") in ("YES", "NO")
                and _f(r.get("model_prob")) is not None]
    out: Dict[str, Any] = {"logged": len(rows), "resolved": len(resolved),
                           "pending": sum(1 for r in rows if not r.get("resolved"))}
    if not resolved:
        return out
    y = np.array([1.0 if r["actual_outcome"] == "YES" else 0.0 for r in resolved])
    mp = np.array([_f(r["model_prob"]) for r in resolved], float)
    out["model_accuracy"] = round(float(np.mean((mp >= 0.5) == (y == 1))), 3)
    out["model_brier_all"] = round(float(np.mean((mp - y) ** 2)), 4)

    # Head-to-head only where a real, non-phantom market price exists and the contract
    # measures what we model (GAAP contracts are a different question).
    mk = [r for r in resolved
          if _f(r.get("market_prob")) is not None
          and MIN_Q < _f(r["market_prob"]) < 1 - MIN_Q
          and "DISPUTED" not in (r.get("notes") or "").upper()
          and (r.get("eps_basis") or "") != "gaap"]
    mk = _dedup_events([{**r, "ticker": r.get("ticker"),
                         "earnings_date": r.get("earnings_date"),
                         "outcome": r.get("outcome")} for r in mk])
    if mk:
        ym = np.array([1.0 if r["actual_outcome"] == "YES" else 0.0 for r in mk])
        qm = np.array([_f(r["market_prob"]) for r in mk], float)
        pm = np.array([_f(r["model_prob"]) for r in mk], float)
        out["bets"] = len(mk)
        out["market_brier"] = round(float(np.mean((qm - ym) ** 2)), 4)
        out["model_brier"] = round(float(np.mean((pm - ym) ** 2)), 4)
        out["model_beats_market"] = out["model_brier"] < out["market_brier"]
        pnls = []
        for r in mk:
            q = _f(r["market_prob"])
            side = r.get("side") or ("YES" if _f(r["model_prob"]) >= q else "NO")
            pnls.append(_pnl_per_1(side, q, r["actual_outcome"]))
        st = _robust_pnl(pnls, seed=6)
        out["realized_pnl_per_$1"] = st.get("mean")
        out["pnl_ci95"] = st.get("ci95")
        out["pnl_median"] = st.get("median")
        out["sd"] = st.get("sd")
        out["n_needed"] = st.get("n_needed")
        out["pnl_verdict"] = _verdict(st)
    return out


def score_hypothesis(hyp_id: str) -> Dict[str, Any]:
    """Score ONE PRE-REGISTERED hypothesis — and only bets logged AFTER it was registered.

    This is the defense against manufacturing an edge by searching. Slicing 272 resolved
    bets twenty ways and keeping the best slice will always produce something that looks
    significant; on 2026-11-03 a sweep of ~20 subgroups turned up four "profitable" ones
    purely as multiple-comparisons noise. The rule here is therefore:

      * the rule must exist in output/hypotheses.json BEFORE the bets it is scored on,
      * only bets with logged_at >= registered_at count (no backward scoring, ever),
      * only bets with a verified executable book count (a bet you could not fill is not
        evidence), and
      * the success criterion is the one written at registration — not a better one found
        later.

    An observation that is not registered is a HYPOTHESIS, not a result. It earns a verdict
    only by surviving forward, on data that did not exist when it was proposed.
    """
    import json as _json
    from tools.config import OUTPUT_DIR
    try:
        reg = _json.loads((OUTPUT_DIR / "hypotheses.json").read_text())
    except Exception as e:
        return {"error": f"cannot read hypotheses.json: {e}"}
    hyp = next((h for h in reg.get("pre_registered", []) if h.get("id") == hyp_id), None)
    if hyp is None:
        return {"error": f"'{hyp_id}' is NOT pre-registered. Register it first — a rule found "
                         f"by searching resolved bets cannot be scored on those same bets."}
    tag = hyp.get("logging_note_tag") or hyp_id
    since = hyp.get("scoring_starts_at") or hyp.get("registered_at") or "0000-00-00"

    import csv as _csv
    bets, skipped_before, skipped_unexec = [], 0, 0
    if EDGE_LOG_PATH.exists():
        for r in _csv.DictReader(open(EDGE_LOG_PATH)):
            if tag not in (r.get("notes") or ""):
                continue
            if not r.get("resolved") or r.get("actual_outcome") not in ("YES", "NO"):
                continue
            if (r.get("logged_at") or "")[:10] < since:
                skipped_before += 1
                continue
            try:
                q = float(r["market_prob"]); mp = float(r["model_prob"])
            except (TypeError, ValueError, KeyError):
                continue
            if not (MIN_Q < q < 1 - MIN_Q) or "DISPUTED" in (r.get("notes") or "").upper():
                continue
            if (r.get("eps_basis") or "") == "gaap":
                continue
            if _executable(r) is not True:
                skipped_unexec += 1
                continue
            side = r.get("side") or ("YES" if mp >= q else "NO")

            def _nf(k):
                try:
                    return float(r[k])
                except (TypeError, ValueError, KeyError):
                    return None
            fill = _fill_price(side, q, _nf("yes_bid"), _nf("yes_ask"))
            bets.append({"ticker": r.get("ticker"), "earnings_date": r.get("earnings_date"),
                         "outcome": r.get("outcome"),
                         "pnl": ((1 - fill) / fill - HAIRCUT) if side == r["actual_outcome"] else -1.0})
    bets = _dedup_events(bets)
    st = _robust_pnl([b["pnl"] for b in bets], seed=3)
    return {"id": hyp_id, "status": hyp.get("status"), "scoring_starts_at": since,
            "success_criterion": hyp.get("success_criterion"),
            "kill_criterion": hyp.get("kill_criterion"),
            "bets_scored": st.get("n", 0), "stats": st, "verdict": _verdict(st),
            "excluded_logged_before_registration": skipped_before,
            "excluded_no_executable_book": skipped_unexec}


def strike_edge_status() -> Dict[str, Any]:
    """Progress of the pre-registered STRIKE-GAP experiment (hypothesis strike_gap_v1) — FAST,
    offline. Scores realized P&L (leveraged per-$1) of resolved 'eps_strike' bets only. Reports
    against the pre-registered criterion: PROVEN only at >=30 resolved bets with 95% CI-low > 0;
    DISPROVEN at CI-high < 0. Never calls it tradeable before that."""
    import csv as _csv
    from tools.config import EDGE_LOG_PATH
    H = 0.02
    pnls, pending = [], 0
    if EDGE_LOG_PATH.exists():
        for r in _csv.DictReader(open(EDGE_LOG_PATH)):
            if r.get("outcome") != "eps_strike":
                continue
            # pre-registered strike_gap_v1 scores only bets that met the ENTRY GATE (candidates),
            # not every logged 'watch' — the hypothesis is about the gated bets.
            if "candidate" not in (r.get("notes") or ""):
                continue
            if r.get("resolved") != "yes" or r.get("actual_outcome") not in ("YES", "NO"):
                pending += 1
                continue
            try:
                q = float(r["market_prob"])
            except (TypeError, ValueError):
                continue
            if not (0.02 < q < 0.98):
                continue
            side = r.get("side")
            if side not in ("YES", "NO"):
                continue
            won = side == r["actual_outcome"]
            pnls.append(((1 - q) / q - H if won else -1.0) if side == "YES"
                        else ((q / (1 - q)) - H if won else -1.0))
    n = len(pnls)
    if n >= 2:
        import numpy as _np
        arr = _np.array(pnls)
        boot = [float(_np.mean(_np.random.default_rng(i).choice(arr, n, replace=True))) for i in range(2000)]
        mean = round(float(arr.mean()), 3)
        ci_low, ci_high = round(float(_np.percentile(boot, 2.5)), 3), round(float(_np.percentile(boot, 97.5)), 3)
    else:
        mean = round(pnls[0], 3) if pnls else None
        ci_low = ci_high = None
    if n < 30:
        verdict = f"OPEN — {n}/30 resolved strike-gap bets ({pending} pending). No claim yet."
    elif ci_low is not None and ci_low > 0:
        verdict = "PROVEN — strike-gap edge positive & significant (95% CI-low > 0) over 30+ bets."
    elif ci_high is not None and ci_high < 0:
        verdict = "DISPROVEN — strike-gap edge net-negative over 30+ bets."
    else:
        verdict = "NO PROVEN EDGE — strike-gap P&L not significantly > 0 after 30+ bets."
    return {"hypothesis": "strike_gap_v1", "resolved_n": n, "pending_n": pending,
            "pnl_per_1": mean, "ci95": [ci_low, ci_high], "verdict": verdict,
            "proven": bool(n >= 30 and ci_low is not None and ci_low > 0)}

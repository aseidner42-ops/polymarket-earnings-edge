"""Orchestrate one prediction: gather data -> signals -> probability -> edge.

Keeps the user's three questions separate:
  fundamental beat probability (q1), priced-in index (q2), Polymarket edge (q3).
The selected OUTCOME decides how those combine into the headline model_prob.
"""
from __future__ import annotations

import math
from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional

from tools import ev, finnhub_client as fh, market_data as md, scoring, signals, snapshot
from tools import edgar, surprise_model, pump, smg
from tools.scar import scar_rating
from tools.config import load_weights
from tools.polymarket_client import get_market

# Supported Polymarket-style outcomes.
OUTCOMES = ("eps_beat", "revenue_beat", "revenue_over", "eps_surprise_over", "stock_up")


def _logistic(x: float) -> float:
    return 1.0 / (1.0 + math.exp(-x))


def _prob_exceed(predicted: float, threshold: float, scale: float) -> float:
    return _logistic((predicted - threshold) / scale)


def _clamp(x: float, lo: float = 0.03, hi: float = 0.97) -> float:
    return max(lo, min(hi, x))


def _band(importance: int) -> str:
    """Importance band per the user's scale: 1-30 not, 31-79 somewhat, 80-100 high."""
    if importance >= 80:
        return "HIGH"
    if importance >= 31:
        return "SOME"
    return "LOW"


def _rec(label: str, importance: int, why: str, suppliable: bool = False,
         key: Optional[str] = None, web: bool = False) -> Dict[str, Any]:
    return {"label": label, "importance": importance, "band": _band(importance),
            "why": why, "suppliable": suppliable, "key": key, "web": web}


def _recommendations(outcome, market, earnings_date, consensus_rev, kelly, confidence,
                     sigs, data) -> List[Dict[str, Any]]:
    """What extra info would sharpen this read, each rated 1-100. Everything the
    engine CAN auto-fetch is already fetched; these are the genuine remaining gaps."""
    recs: List[Dict[str, Any]] = []
    if market is None:
        recs.append(_rec("Polymarket market link", 92,
                         "The edge (model vs market) is the core output — without it you only get the model probability.",
                         suppliable=True, key="polymarket_url"))
    if not earnings_date:
        recs.append(_rec("Find the upcoming earnings date", 75,
                         "No scheduled date found; press SCAN WEB to fetch it, or type it in.",
                         suppliable=True, key="earnings_date", web=True))
    if outcome in ("revenue_beat", "revenue_over") and consensus_rev is None:
        recs.append(_rec("Revenue consensus estimate", 85,
                         "Free data lacks revenue consensus, so the revenue outcome is weakly grounded (premium-gated)."))
    if not kelly or kelly.get("ext_ret") is None:
        # Only here does a user-supplied snapshot actually help (auto-fetch failed).
        recs.append(_rec("Price history for the Kelly signal", 70,
                         "Could not auto-fetch price to compute over-extrapolation. Upload a daily snapshot CSV.",
                         suppliable=True, key="snapshot"))
    elif kelly.get("n_quarters", 0) < 8:
        # The cap here is the number of known earnings DATES (free data), NOT price.
        # A longer snapshot can't add dates, so this is a low-importance data cap.
        recs.append(_rec(f"Only {kelly['n_quarters']}/8 past earnings dates available (free-data cap)", 28,
                         "Kelly wants 8 announcement windows; free data exposes fewer dates for this name. "
                         "A longer price snapshot won't help — it adds prices, not earnings dates. Paid estimate "
                         "history (or manual dates) is the only fix.", suppliable=False))
    if confidence == "low":
        recs.append(_rec("More signal coverage (try a more-covered ticker)", 60,
                         "Few fundamental signals were available for this name."))
    if not (sigs.get("analyst_revision") and sigs["analyst_revision"].present):
        recs.append(_rec("Analyst estimate-revision history", 45,
                         "Premium-gated on free; would add a proven pre-earnings signal (Stickel 1991)."))
    opt = sigs.get("options_move")
    if not (opt and opt.present):
        recs.append(_rec("Options-implied move / skew", 25,
                         "Minor priced-in refinement; no chain available."))
    if not data.get("news"):
        recs.append(_rec("Recent news / sentiment", 20, "No headlines found; small effect."))
    recs.sort(key=lambda r: -r["importance"])
    return recs


def _announce_dates(surprises: List[Dict[str, Any]]) -> List[datetime]:
    """Approximate past announcement dates (most-recent-first) from fiscal-period
    ends + ~40 days. Used to locate the announcement windows in a price snapshot."""
    out = []
    for s in surprises:
        p = s.get("period")
        if not p:
            continue
        try:
            out.append(datetime.strptime(p, "%Y-%m-%d") + timedelta(days=40))
        except ValueError:
            continue
    return out


def gather_data(symbol: str, asof: Optional[datetime] = None,
                snapshot_path: Optional[str] = None) -> Dict[str, Any]:
    """Assemble every raw input the signal layer needs. asof set => backtest mode
    (skips live-only options data). snapshot_path => compute the Kelly ExtRet
    (central over-extrapolation signal) from a TradingView price snapshot."""
    live = asof is None
    # Prefer yfinance real earnings dates + surprises (deeper, precise dates);
    # fall back to Finnhub. Backtest mode keeps Finnhub for point-in-time slicing.
    eh = md.get_earnings_history(symbol) if live else tuple()
    if eh:
        surprises = [{
            "surprisePercent": e["surprise_pct"],
            "surprise": (None if e["eps_estimate"] is None
                         else e["eps_actual"] - e["eps_estimate"]),
            "period": e["date"].date().isoformat(),
        } for e in eh]
        real_dates = [e["date"] for e in eh]  # precise, most-recent-first
    else:
        surprises = fh.get_earnings_surprises(symbol)
        real_dates = None
    data = {
        "surprises": surprises,
        "recommendations": fh.get_recommendation_trends(symbol) if live else [],
        "revisions": md.get_estimate_revisions(symbol) if live else None,
        "pre_earnings_return": md.pre_earnings_return(symbol, asof=asof),
        "rel_strength": md.relative_strength(symbol, asof=asof),
        "volume_buildup": md.volume_buildup(symbol, asof=asof),
        "options": md.options_implied_move(symbol) if live else {},
        "news": fh.get_company_news(symbol) if live else [],
        "peer_beats": fh.peer_beat_rate(symbol) if live else None,
        "short_interest": md.get_short_interest(symbol) if live else None,
        "dispersion": md.get_forecast_dispersion(symbol) if live else None,
        "accruals": md.get_accruals(symbol) if live else None,
        # Point-in-time (asof-aware) — backtestable miss-detectors from free SEC data.
        "fundamental_trend": md.get_fundamental_trend(symbol, asof=asof),
        "piotroski": md.get_piotroski(symbol, asof=asof),
        "filing_risk": edgar.filing_risk(symbol, asof=(asof.date().isoformat() if asof else None)),
        "pump_score": pump.pump_pressure(symbol, asof=asof).get("pump"),  # PUMP as a beat signal
        "kelly": None,
    }
    announce = real_dates if real_dates else _announce_dates(surprises)
    if snapshot_path:
        try:
            data["kelly"] = snapshot.kelly_from_snapshot(snapshot_path, announce)
            data["kelly"]["source"] = "snapshot"
        except Exception as e:
            data["kelly"] = {"ext_ret": None, "error": str(e)}
    elif live:
        # Auto-fetch price (yfinance/Stooq) and compute Kelly — no user input needed.
        try:
            bars = md.get_price_bars(symbol, "2y")
            k = snapshot.extrapolated_return_measure(bars, announce)
            k["bars"] = len(bars)
            k["source"] = "auto (yfinance/stooq)"
            data["kelly"] = k
        except Exception as e:
            data["kelly"] = {"ext_ret": None, "error": str(e)}
    return data


def _model_prob_for_outcome(outcome: str, threshold: Optional[float],
                            fund: Dict[str, Any], pred: Dict[str, float],
                            pi: float, sigs: Dict[str, signals.Signal],
                            consensus_rev: Optional[float],
                            timing: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """Map fundamental/priced-in results to the selected outcome's probability."""
    beat = fund["probability"]
    eps_s, rev_s = pred["eps_surprise_pct"], pred["revenue_surprise_pct"]
    note = ""

    if outcome == "eps_beat":
        thr = threshold if threshold is not None else 0.0
        prob = beat if thr == 0 else 0.4 * beat + 0.6 * _prob_exceed(eps_s, thr, 4.0)
    elif outcome == "eps_surprise_over":
        thr = threshold if threshold is not None else 0.0
        prob = _prob_exceed(eps_s, thr, 4.0)
    elif outcome == "revenue_beat":
        thr = threshold if threshold is not None else 0.0
        prob = 0.5 * beat + 0.5 * _prob_exceed(rev_s, thr, 2.0)
    elif outcome == "revenue_over":
        if consensus_rev is None or threshold is None:
            return {"prob": None, "note": "needs consensus revenue + a dollar threshold"}
        pred_rev = consensus_rev * (1 + rev_s / 100.0)
        prob = _prob_exceed(pred_rev, threshold, max(1.0, 0.02 * consensus_rev))
        note = f"predicted revenue ~{pred_rev:,.0f} vs threshold {threshold:,.0f}"
    elif outcome == "stock_up":
        rcfg = load_weights()["reaction"]
        skew = sigs.get("options_skew")
        skew_adj = 0.10 * skew.score if (skew and skew.present) else 0.0
        oe = sigs.get("over_extrapolation")
        kelly_term = rcfg["kelly_reversal"] * oe.score if (oe and oe.present) else 0.0
        si = sigs.get("short_interest")
        squeeze = rcfg.get("short_squeeze", 0.0) * si.score if (si and si.present) else 0.0
        # Kelly 2016: high extrapolated past-announcement returns -> reversal (down).
        # Short interest adds squeeze upside on a beat.
        prob = beat - (pi - 0.5) * rcfg["priced_in_penalty"] - kelly_term + skew_adj + squeeze
        # Frazzini-Lamont 2007: small positive earnings-announcement premium.
        prob += rcfg.get("announcement_premium", 0.0)
        # DellaVigna-Pollet 2009 / Hirshleifer 2009: Friday/after-close prints get
        # under-reacted to -> bigger drift; amplify the directional tilt.
        t = timing or {}
        if t.get("friday") or t.get("amc"):
            prob = 0.5 + (prob - 0.5) * (1 + rcfg.get("inattention_amp", 0.0))
        # HONESTY CLAMP (2026-06-27): we tested post-earnings DIRECTION on ~3,000 events
        # across large-cap, small/mid-volatile, instant-reaction AND multi-week drift, and
        # every transparent free-data signal sits at a COIN FLIP (corr ~0, sign-hit ~50%);
        # PEAD has been arbitraged out of non-microcaps since ~2006. So we refuse to emit a
        # confident up/down call — compress hard toward 0.5. The only repeatable pulse is a
        # high over-extrapolation -> REVERSAL (Kelly), which the kelly_term already encodes.
        prob = 0.5 + (prob - 0.5) * rcfg.get("direction_reliability", 0.35)
        note = ("post-earnings DIRECTION ~ coin-flip on free data (tested 3,000+ events); "
                "compressed toward 50%. Only a stretched run-up -> reversal (Kelly) carries "
                "a weak signal. Treat as LOW-CONFIDENCE.")
    else:
        return {"prob": None, "note": f"unknown outcome '{outcome}'"}

    return {"prob": _clamp(prob), "note": note}


def predict(query: str, outcome: str = "eps_beat",
            threshold: Optional[float] = None,
            polymarket_url: Optional[str] = None,
            snapshot_path: Optional[str] = None,
            earnings_date_override: Optional[str] = None) -> Dict[str, Any]:
    """Full single-company prediction. Returns a result dict consumed by report.py.

    snapshot_path: optional TradingView OHLCV CSV used to compute the Kelly
    over-extrapolation measure (the project's central signal)."""
    if outcome not in OUTCOMES:
        raise ValueError(f"outcome must be one of {OUTCOMES}")

    resolved = fh.resolve_symbol(query)
    if not resolved:
        return {"error": f"could not resolve '{query}' to a ticker"}
    symbol, name = resolved["symbol"], resolved["description"]

    nxt = fh.get_next_earnings(symbol) or {}
    consensus_eps = nxt.get("epsEstimate")
    consensus_rev = nxt.get("revenueEstimate")
    consensus_source = "finnhub_calendar" if consensus_eps is not None else None
    n_analysts_fwd = None
    fh_eps = consensus_eps            # Finnhub's calendar number, kept for the stale-carry check
    # FRESH consensus (critique #2 freshness root): yfinance's LIVE next-quarter estimate overrides
    # Finnhub's free calendar, which carries a prior quarter's estimate forward (GME $0.3737 stale).
    # ONLY override with a real crowd (>=1 analyst); a 0/None-analyst yfinance value is a stub and
    # must not silently replace a real Finnhub consensus (agent review) — used only if Finnhub has
    # nothing, and then flagged as a thin stub.
    _fresh = md.get_forward_eps_consensus(symbol)
    _fresh_eps, _fresh_n = _fresh.get("eps"), _fresh.get("n_analysts")
    if _fresh_eps is not None and _fresh_n is not None and _fresh_n >= 1:
        consensus_eps = _fresh_eps
        consensus_source = "yfinance_estimate"
        n_analysts_fwd = _fresh_n
    elif _fresh_eps is not None and consensus_eps is None:
        consensus_eps = _fresh_eps
        consensus_source = "yfinance_estimate_stub"
        n_analysts_fwd = _fresh_n if _fresh_n is not None else 0

    # Earnings date: user override > Finnhub > free web scan (yfinance).
    earnings_date = earnings_date_override or nxt.get("date")
    date_source = "user" if earnings_date_override else ("finnhub" if nxt.get("date") else None)
    if not earnings_date:
        found = md.find_next_earnings_date(symbol)
        if found.get("date"):
            earnings_date, date_source = found["date"], found.get("source")

    data = gather_data(symbol, snapshot_path=snapshot_path)
    kelly = data.get("kelly")
    sigs = signals.compute_all(data)
    sector = md.get_sector(symbol)
    fund = scoring.score_fundamental(sigs, sector)
    pi = scoring.priced_in_index(sigs)["index"]
    pred = scoring.predicted_surprises(sigs, data["surprises"])

    # CWSF — firm-specific surprise estimate + uncertainty band (precise on small caps).
    sp_hist = [s.get("surprisePercent") for s in data["surprises"]
               if s.get("surprisePercent") is not None]
    _rev, _disp = (data.get("revisions") or {}), (data.get("dispersion") or {})
    _ft, _fr = sigs.get("fundamental_trend"), sigs.get("filing_risk")
    _etilt = (0.01 * _ft.score if (_ft and _ft.present) else 0.0) + \
             (0.01 * _fr.score if (_fr and _fr.present) else 0.0)
    cwsf = surprise_model.predict_surprise(
        sp_hist, revision_drift_pct=(_rev.get("trend_pct") or 0.0),
        n_analysts=_disp.get("n_analysts"), dispersion=_disp.get("dispersion"), event_tilt=_etilt)
    if cwsf:  # prefer the firm-specific surprise estimate over the crude norm-based one
        pred["eps_surprise_pct"] = cwsf["pred_surprise_pct"]

    # CENTS/LEVEL model (critique #8) — robust for small-EPS names (where % surprises explode) and
    # the engine for the contract-strike probability (#3). Uses Finnhub actual/estimate DOLLAR
    # pairs. Reuses the Finnhub surprise table (also used by the verification gate below).
    _fh_surp = fh.get_earnings_surprises(symbol, limit=12)
    _fh_pairs = [(s.get("actual"), s.get("estimate")) for s in _fh_surp
                 if s.get("actual") is not None and s.get("estimate") is not None]
    cwsf_level = surprise_model.predict_eps_level(
        consensus_eps, _fh_pairs, revision_drift_pct=(_rev.get("trend_pct") or 0.0),
        n_analysts=_disp.get("n_analysts"), dispersion=_disp.get("dispersion"), event_tilt=_etilt)

    # SCAR — conviction gate / miss-risk flag (NOT a binary predictor). Validated on
    # 30 volatile names: it can't flip calls without false positives, but it flagged
    # 100% of the misses. So it lowers conviction + warns, rather than overriding.
    _fp = surprise_model.fingerprint(sp_hist)
    _unprof = any((s.get("surprise") is not None and (s.get("actual") or 0) < 0) for s in data["surprises"][:3]) \
        if data["surprises"] and "actual" in (data["surprises"][0] or {}) else False
    scar = scar_rating(sigs, md.get_revenue_series(symbol, asof=None),
                       _fp["mu"] if _fp else None, scoring.load_weights()["weights"],
                       scoring.FUNDAMENTAL, fingerprint_consistency=(_fp["consistency"] if _fp else None),
                       unprofitable=_unprof, model_prob=fund["probability"],
                       fingerprint_sigma=(_fp["sigma"] if _fp else None))
    scar_flag, scar_reasons = False, []
    # Below the (data-calibrated) predictability threshold = low-conviction, size down.
    from tools.scar import predictability_threshold
    _pthr = predictability_threshold()
    if scar.get("predictability") is not None and scar["predictability"] < _pthr:
        scar_flag = True; scar_reasons.append(f"low predictability ({scar['predictability']}/100)")
    if scar.get("acceleration") is not None and scar["acceleration"] < -0.15:
        scar_flag = True; scar_reasons.append(f"growth decelerating ({scar['acceleration']:+.2f})")
    if _fr and _fr.present and _fr.score <= -0.5:
        scar_flag = True; scar_reasons.append("adverse SEC filings")
    if _ft and _ft.present and _ft.score <= -0.4:
        scar_flag = True; scar_reasons.append("deteriorating fundamentals")
    if scar.get("coherence", 1) < 0.45:
        scar_flag = True; scar_reasons.append(f"signals conflict (coherence {scar.get('coherence')})")

    confidence = scoring.confidence_label(fund)
    if scar_flag and confidence == "high":
        confidence = "medium"  # SCAR says don't be overconfident here

    # VERIFICATION / ABSTAIN GATE (GME 2026-09-21). Never emit a confident number on unverified
    # inputs — the GME 82% HIGH was built on a STALE consensus (Finnhub's free calendar carried
    # GME's already-reported March estimate 0.3737 forward as the "next" quarter) plus thin
    # coverage (κ=0.319). Two checks are feasible on free data:
    #  (a) consensus freshness — if the "next" consensus EPS exactly equals an ALREADY-REPORTED
    #      prior quarter's estimate, it's a stale carry and the target number can't be trusted
    #      -> ABSTAIN (emit NULL probability, not a guess);
    #  (b) surprise-model coverage κ -> collapses confidence (thin coverage is never "high").
    verify_fail: List[str] = []
    # Finnhub's surprise table (not gather_data's yfinance one) carries the per-quarter ESTIMATE +
    # actual — needed to detect a stale carry. An exact 4-decimal match to an already-reported
    # quarter's estimate is a near-certain stale carry, not coincidence. (_fh_surp fetched above.)
    _prior_ests = [s.get("estimate") for s in _fh_surp
                   if s.get("actual") is not None and s.get("estimate") is not None]
    # Only the UN-overridden Finnhub calendar number can be a stale carry — check IT specifically
    # (not a yfinance override, which would false-abstain on a coincidental match; agent review).
    stale_consensus = (consensus_source == "finnhub_calendar" and fh_eps is not None
                       and any(abs(float(fh_eps) - e) < 1e-6 for e in _prior_ests))
    if stale_consensus:
        verify_fail.append(f"consensus EPS {fh_eps} equals an already-reported prior quarter's "
                           "estimate (Finnhub carry) and no fresh estimate available — unverified")
    if consensus_eps is None and outcome in ("eps_beat", "eps_surprise_over"):
        verify_fail.append("no consensus EPS available for an EPS contract")
    _kappa = (cwsf or {}).get("kappa")
    if _kappa is not None and confidence == "high":
        if _kappa < 0.30:
            confidence = "low"
        elif _kappa < 0.45:
            confidence = "medium"
    # Ultra-thin analyst coverage (0-1 estimates, e.g. GME) — the consensus is one desk's mark, not
    # a real crowd. Fresh, so we don't abstain, but confidence must be LOW (critique #7).
    if n_analysts_fwd is not None and n_analysts_fwd <= 1:
        confidence = "low"
    abstain = bool(verify_fail)
    if abstain:
        confidence = "abstain"

    # Confidence interval: wider with fewer fundamental signals + more disagreement.
    _np, _ag = fund["n_present"], fund["agreement"]
    _margin = max(0.03, min(0.30, 0.5 / math.sqrt(max(_np, 1)) * (1.3 - _ag)))
    if scar_flag:
        _margin = min(0.38, _margin * 1.4)  # SCAR-flagged -> widen the band (less sure)

    # Announcement timing (DellaVigna-Pollet 2009): Friday / after-market-close.
    friday = False
    if earnings_date:
        try:
            friday = datetime.strptime(earnings_date, "%Y-%m-%d").weekday() == 4
        except ValueError:
            pass
    timing = {"friday": friday, "amc": (nxt.get("hour") == "amc")}

    mapped = _model_prob_for_outcome(outcome, threshold, fund, pred, pi, sigs,
                                     consensus_rev, timing)
    model_prob = mapped["prob"]

    # Blend the CWSF beat probability into the magnitude outcomes. PREFER the cents/level model
    # (critique #8: %-surprise is toxic when consensus EPS is small); fall back to the %-model.
    # Its band self-widens for small/low-coverage names -> pulls them honestly toward 0.5.
    if model_prob is not None and outcome in ("eps_beat", "eps_surprise_over"):
        cwsf_p = None
        if cwsf_level and outcome == "eps_beat" and consensus_eps:
            # consensus beat = reported EPS > consensus (dollars; no rounding contract here)
            cwsf_p = surprise_model.p_level_over(cwsf_level["pred_eps"], cwsf_level["sigma_eps"],
                                                 consensus_eps)
            mapped["note"] += (f" | CWSF(cents) p={cwsf_p:.2f} "
                               f"(pred ${cwsf_level['pred_eps']:.3f}±${cwsf_level['sigma_eps']:.3f} "
                               f"vs ${consensus_eps:.3f})")
        elif cwsf:
            thr = (threshold or 0.0)
            cwsf_p = surprise_model.p_beat(cwsf["pred_surprise_pct"], cwsf["sigma"], thr)
            mapped["note"] += (f" | CWSF(%) p={cwsf_p:.2f} "
                               f"(pred {cwsf['pred_surprise_pct']:+.1f}%±{cwsf['sigma']:.1f})")
        if cwsf_p is not None:
            wt = 0.5 if outcome == "eps_surprise_over" else 0.35
            model_prob = _clamp((1 - wt) * model_prob + wt * cwsf_p)

    # Fresh-severe-miss: a big miss LAST quarter (<-20%) tends to persist (Bernard-Thomas
    # PEAD / negative-SUE drift). Cap the beat probability — validated to catch real
    # repeat-missers (EA) without flipping steady beaters.
    _recent_surp = next((s.get("surprisePercent") for s in data["surprises"]
                         if s.get("surprisePercent") is not None), None)
    fresh_miss = _recent_surp is not None and _recent_surp < -50  # catastrophic only (rare, conservative)
    if fresh_miss and model_prob is not None and outcome in ("eps_beat", "eps_surprise_over"):
        model_prob = min(model_prob, 0.45)

    market = get_market(polymarket_url) if polymarket_url else None
    vix = md.get_vix()
    regime = vix.get("percentile")  # higher VIX percentile -> bigger uncertainty haircut

    # RIGHT-TARGET (critique #3): a Polymarket EPS contract resolves on reported EPS vs a FIXED
    # strike set at creation, with a comparison operator + cent rounding + equality=NO — NOT "beat
    # current consensus". If the contract states an EPS-adjacent dollar strike, model
    # P(round(reported EPS, 2) <op> strike) DIRECTLY in dollars via the cents/level model (#8) —
    # honoring the operator and the half-cent rounding boundary — and use THAT as the contract
    # probability. Expose the strike gap (consensus drift vs the frozen strike). When no strike is
    # stated, fall back to the consensus-beat probability.
    contract_prob = None; contract_strike = None; strike_gap = None
    contract_operator = None; contract_non_gaap = None; contract_basis = None
    strike_abstain = None
    if market and consensus_eps and cwsf_level:
        from tools.polymarket_client import parse_eps_strike
        sk = parse_eps_strike((market.get("resolution_text") or "") + " " + (market.get("question") or ""))
        s = sk.get("strike")
        contract_basis = sk.get("eps_basis")
        # GAAP TARGET MISMATCH GATE (2026-11-03). Analyst consensus (yfinance/Finnhub) and our
        # whole surprise history are ADJUSTED / non-GAAP. A contract that resolves on GAAP EPS is
        # a different measure, and for firms with large non-operating marks the two diverge by
        # more than any edge we could have. Computing P(GAAP EPS > GAAP strike) from an adjusted
        # distribution is a unit error, and it manufactures enormous fake edges: UBER 2026-11-04
        # came out at model 0.68 vs market 0.19 purely from this. We cannot price it, so we say so.
        if contract_basis == "gaap":
            strike_abstain = ("contract resolves on GAAP EPS, but our consensus and surprise "
                              "history are non-GAAP/adjusted — different measures, not priceable")
            s = None
        # only trust a strike within a sane band of consensus (avoids grabbing a revenue/price $)
        if s and 0.3 <= (s / consensus_eps) <= 3.0:
            _raw_cp = surprise_model.p_eps_over(
                cwsf_level["pred_eps"], cwsf_level["sigma_eps"], s,
                equality_no=sk.get("equality_no", True), operator=sk.get("operator", ">"))
            # OOS-validated Platt recal — the raw contract_prob under-predicts P(clear strike)
            # (firms beat ~72%); corrects the systematic bias (output/strike_calibration.json).
            contract_prob = surprise_model.recalibrate_contract_prob(_raw_cp)
            contract_strike = s
            contract_operator = sk.get("operator", ">")
            contract_non_gaap = sk.get("non_gaap")
            strike_gap = round(consensus_eps - s, 4)

    # EDGE ENGINE: SCAR-shrinkage anchored on the market. Use the contract-strike probability
    # when we have one (the right target); else the consensus-beat model probability.
    edge_basis = contract_prob if contract_prob is not None else model_prob
    edge_info = None
    prob_for_ev = edge_basis
    # MARKET-VALIDITY GATE (fix 2026-09-21): a resolved/degenerate market (price 0 or 1, closed,
    # or no order book) is NOT a tradeable price. Treat it as NO market — never compute an edge
    # against a phantom 0%/100% price or recommend "BUY @ 0.00". Requires a real executable book.
    # VALIDITY is separate from TRADEABILITY (critique #11): a market is VALID if it is active with
    # a live TWO-SIDED order book (a genuine 1¢/99¢ market is valid) — the phantom GME market had NO
    # book, which is what we reject here. Whether it's economically worth trading (spread, liquidity,
    # EV) is ev.evaluate()'s job, not a validity band.
    _q = market.get("yes_price") if market else None
    _bid, _ask = (market or {}).get("best_bid"), (market or {}).get("best_ask")
    _valid_market = bool(
        market and not market.get("closed")
        and _bid is not None and _ask is not None and _ask > _bid   # live two-sided book
        and _q is not None and 0.0 < float(_q) < 1.0)               # sane price (0/1 = resolved)
    # A GAAP-resolving contract is not mispriced for us — it is UNPRICED BY US. Our model
    # answers "does adjusted EPS beat adjusted consensus"; the contract pays on GAAP. Any
    # gap between the two is our unit error, not the crowd's mistake, so no edge is computed
    # (2026-11-03). ~22% of the live earnings markets resolve on GAAP, so this is not rare.
    if contract_basis == "gaap":
        _valid_market = False
    if _valid_market and edge_basis is not None and not abstain:
        edge_info = scoring.edge_estimate(sigs, float(_q),
                                          model_prob=edge_basis, scar=scar.get("predictability"))
        prob_for_ev = edge_info["market_aware_prob"]
    else:
        prob_for_ev = None   # no valid market / abstaining -> no edge, no trade recommendation
    edge = ev.evaluate(prob_for_ev, market, regime=regime) if (_valid_market and prob_for_ev is not None) else {
        "recommendation": ("ABSTAIN — inputs unverified (" + "; ".join(verify_fail) + ")" if abstain
                           else ("market resolved / no live order book — nothing tradeable"
                                 if market else "no Polymarket price available")),
        "side": None, "net_edge": None, "executable_edge": None, "max_tradeable_usd": None}
    if contract_basis == "gaap" and market:
        edge["recommendation"] = ("NO EDGE COMPUTED — this contract resolves on GAAP EPS; our "
                                  "consensus and surprise history are non-GAAP/adjusted. Different "
                                  "measures: any apparent gap is our unit error, not a mispricing.")

    # STRIKE-GAP EDGE (route A, pre-registered hypothesis strike_gap_v1). A SEPARATE, structural
    # edge that deliberately does NOT SCAR-shrink toward the market (the whole point is the crowd
    # misprices the FROZEN strike) and does NOT use our beat-signal blend (those disagreements lose).
    # It bets P(reported EPS > frozen strike) — from FRESH consensus + the firm's cents-model
    # distribution, OOS-recalibrated — against the market price. UNPROVEN: no historical PM strikes,
    # so it is forward-only; gated + logged, never called tradeable until the forward vault clears
    # the pre-registered success criterion (see output/hypotheses.json + forward_test.strike_edge_status).
    strike_edge = None
    if _valid_market and contract_prob is not None and contract_strike is not None and not abstain:
        se = ev.evaluate(contract_prob, market, regime=regime)
        _exe = se.get("executable_edge")
        se["contract_prob"] = round(contract_prob, 3)
        se["strike_edge_vs_market"] = round(contract_prob - float(_q), 3)
        se["basis"] = "P(reported EPS > frozen strike) vs market price (structural, not shrunk)"
        # CANDIDATE gate — a bet is only a candidate when ALL hold:
        #  (1) fresh consensus with real coverage (>=3 analysts);
        #  (2) POSITIVE executable edge >=5% after walking the book (not abs — a big NEGATIVE
        #      executable edge means it LOSES after friction, e.g. NKE -0.30; earlier abs() bug);
        #  (3) contract_prob in the CALIBRATED MIDDLE [0.15,0.85] — the cents sigma is too tight for
        #      well-covered names so the tails explode to 0/1 (COST 0.00) and are miscalibrated
        #      (backtest low tail pred 0.06 -> realized 0.21); extremes log as 'watch', not bet;
        #  (4) MEANINGFUL STRIKE DRIFT — the frozen strike must be displaced from FRESH consensus by
        #      >= max(2% of consensus, 0.5 sigma). This is the crux: the pre-registered edge is a
        #      MECHANICAL drift (stale frozen strike vs moved consensus), NOT us out-arguing the
        #      market on a near-consensus strike (KBH strike 0.90 ~= consensus 0.90 = the proven
        #      LOSER pattern). No drift -> not a strike-gap bet.
        _sig = (cwsf_level or {}).get("sigma_eps") or 0.0
        _drift = abs(strike_gap) if strike_gap is not None else 0.0
        _meaningful_drift = _drift >= max(0.02 * abs(consensus_eps), 0.5 * _sig)
        se["strike_drift"] = round(_drift, 4)
        se["structural_candidate"] = bool(
            consensus_source == "yfinance_estimate" and (n_analysts_fwd or 0) >= 3
            and _exe is not None and _exe >= 0.05
            and 0.15 <= contract_prob <= 0.85
            and _meaningful_drift)
        se["hypothesis"] = "strike_gap_v1"
        strike_edge = se
    filings = edgar.get_recent_filings(symbol)

    # Evidence: the actual top BEAT-MODEL drivers by REAL model contribution (fitted coef·z / hand
    # weight·score) — NOT raw |score|. Critique #4: the old panel ranked by |score|, so signals
    # that barely counted (pre_earnings_drift ≈0 coef) or aren't even in the beat model
    # (options_skew, short_interest) topped it. Only beat-model signals justify the beat prob;
    # priced-in / options signals live in the priced-in index, not here.
    _contribs = fund.get("logit_contributions", {})
    beat_present = [s for s in sigs.values() if s.present and s.name in _contribs]
    beat_present.sort(key=lambda s: abs(_contribs[s.name]), reverse=True)
    evidence = [f"{s.name}: {s.evidence} (contribution {_contribs[s.name]:+.2f})"
                for s in beat_present[:5]]

    # Risk factors / missing data.
    risks: List[str] = []
    missing = [n for n, s in sigs.items() if not s.present]
    if missing:
        risks.append("missing signals: " + ", ".join(missing))
    if confidence == "low":
        risks.append("low confidence: few fundamental signals available")
    if verify_fail:
        risks.append("VERIFICATION FAILED — abstaining, no probability emitted: "
                     + "; ".join(verify_fail))
    if n_analysts_fwd is not None and n_analysts_fwd <= 1:
        risks.append(f"ultra-thin analyst coverage ({n_analysts_fwd} estimate) — consensus is one "
                     "desk's mark, not a crowd; low confidence")
    if (consensus_source == "yfinance_estimate" and nxt.get("epsEstimate") is not None
            and consensus_eps is not None
            and abs(float(nxt.get("epsEstimate")) - float(consensus_eps)) > 0.02):
        risks.append(f"used FRESH consensus ${consensus_eps} (yfinance live); Finnhub calendar had "
                     f"${nxt.get('epsEstimate')} — likely a stale prior-quarter carry")
    if not earnings_date:
        risks.append("no earnings date found — press SCAN WEB or enter it manually")
    elif date_source and date_source != "finnhub":
        risks.append(f"earnings date {earnings_date} via {date_source}")
    if consensus_rev is None and outcome in ("revenue_beat", "revenue_over"):
        risks.append("consensus revenue unavailable (revenue estimate likely premium-gated)")
    if market is None and polymarket_url:
        risks.append("could not resolve Polymarket market from the URL/slug")
    if market and market.get("resolution_text"):
        risks.append("verify resolution criteria: " + str(market["resolution_text"])[:160])
    # Outcome vs market mismatch (auto-inferred from the market's wording).
    inferred = (market or {}).get("inferred") or {}
    if market and inferred.get("outcome") and inferred["outcome"] != outcome:
        risks.append(f"market wording looks like '{inferred['outcome']}' but you tested "
                     f"'{outcome}' — align them or the edge compares different things")
    if market and market.get("price_momentum") is not None:
        mom = market["price_momentum"]
        risks.append(f"Polymarket price momentum {mom:+.0%} over the window "
                     f"({'crowd moving up' if mom > 0 else 'crowd moving down'})")
    if vix.get("value") is not None and vix.get("percentile", 0) >= 0.8:
        risks.append(f"high-vol regime (VIX {vix['value']}, {vix['percentile']:.0%} percentile) "
                     "— wider haircut applied, outcomes noisier")
    if filings.get("last_8k"):
        risks.append(f"latest SEC 8-K filed {filings['last_8k']}"
                     + (f", 10-Q/K {filings['last_periodic']}" if filings.get("last_periodic") else ""))
    if fresh_miss:
        risks.append(f"fresh severe miss last quarter ({_recent_surp:.0f}%) — negative surprises persist (PEAD)")
    if scar_flag:
        risks.append("SCAR miss-risk flag: " + ", ".join(scar_reasons) + " — lower conviction, size down")
    if outcome == "stock_up":
        risks.append("post-earnings DIRECTION tested at coin-flip on free data (~3,000 events, "
                     "corr ~0 / sign-hit ~50%; PEAD gone from non-microcaps since ~2006). "
                     "Probability is compressed toward 50% on purpose — treat as low-confidence, "
                     "NOT a reliable up/down call.")
    if kelly and kelly.get("error"):
        risks.append("snapshot error: " + str(kelly["error"]))
    elif not kelly or kelly.get("ext_ret") is None:
        risks.append("could not compute Kelly over-extrapolation (no usable price/earnings history)")
    elif kelly.get("source", "").startswith("auto"):
        risks.append(f"Kelly over-extrapolation auto-computed from {kelly.get('n_quarters')} "
                     "real earnings windows (yfinance)")

    result = {
        "company": name, "ticker": symbol,
        "earnings_date": earnings_date,
        "earnings_date_source": date_source,
        "earnings_hour": nxt.get("hour"),
        "outcome": outcome, "threshold": threshold,
        "consensus_eps": consensus_eps, "consensus_revenue": consensus_rev,
        "consensus_source": consensus_source, "n_analysts_fwd": n_analysts_fwd,
        "fiscal_year": nxt.get("year"), "fiscal_quarter": nxt.get("quarter"),
        "earnings_event_id": (f"{symbol}_FY{nxt.get('year')}Q{nxt.get('quarter')}"
                              if nxt.get("year") and nxt.get("quarter") else None),
        "predicted_eps_surprise_pct": round(pred["eps_surprise_pct"], 2),
        "predicted_revenue_surprise_pct": round(pred["revenue_surprise_pct"], 2),
        "cwsf_surprise_band": (f"{cwsf['pred_surprise_pct']:+.1f}% ± {cwsf['sigma']:.1f} "
                               f"(coverage κ={cwsf['kappa']})" if cwsf else None),
        "cwsf_sigma": (cwsf["sigma"] if cwsf else None),
        "cwsf_level_band": (f"${cwsf_level['pred_eps']:.3f} ± ${cwsf_level['sigma_eps']:.3f} "
                            f"(cents model, κ={cwsf_level['kappa']}, n={cwsf_level['n']})"
                            if cwsf_level else None),
        "scar_conviction": scar.get("coherence"),
        "scar_predictability": scar.get("predictability"),
        "scar_acceleration": scar.get("acceleration"),
        "scar_flag": scar_flag,
        "scar_reasons": scar_reasons,
        "fundamental_beat_prob": round(fund["probability"], 3),
        "fundamental_only_prob": fund.get("fundamental_only_prob"),
        "market_adjustment_pp": fund.get("market_adjustment_pp"),
        "market_adjustment_raw_pp": fund.get("market_adjustment_raw_pp"),
        "market_capped": fund.get("market_capped"),
        "bear_signals": sum(1 for n in scoring.FUNDAMENTAL
                            if n in sigs and sigs[n].present and sigs[n].score < -0.15),
        "priced_in_index": round(pi, 3),
        "kelly_ext_ret": (round(kelly["ext_ret"], 4)
                          if kelly and kelly.get("ext_ret") is not None else None),
        "vix": vix,
        "model_prob": (None if abstain else
                       (round(model_prob, 3) if model_prob is not None else None)),
        "raw_model_prob": (round(model_prob, 3) if model_prob is not None else None),
        "abstain": abstain,
        "verify_fail": verify_fail,
        "market_aware_prob": (edge_info["market_aware_prob"] if edge_info else None),
        "edge_vs_market": (edge_info["edge_vs_market"] if edge_info else None),
        "lambda_scar": (edge_info.get("lambda_scar") if edge_info else None),
        "contract_strike": contract_strike,
        "contract_prob": (round(contract_prob, 3) if contract_prob is not None else None),
        "contract_operator": contract_operator,
        "contract_non_gaap": contract_non_gaap,
        "contract_basis": contract_basis,
        "strike_abstain": strike_abstain,
        "strike_gap": strike_gap,
        "strike_edge": strike_edge,
        "edge_tilt": (edge_info["tilt"] if edge_info else None),
        "edge_drivers": (edge_info["drivers"] if edge_info else []),
        "model_prob_ci": (None if abstain else
                          ([round(max(0.02, model_prob - _margin), 3),
                            round(min(0.98, model_prob + _margin), 3)]
                           if model_prob is not None else None)),
        "model_note": mapped["note"],
        "confidence": confidence,
        "market": market,
        "edge": edge,
        "evidence": evidence,
        "risks": risks,
        "recommendations": _recommendations(outcome, market, earnings_date, consensus_rev,
                                             kelly, confidence, sigs, data),
    }
    result["smg"] = smg.smg_edge(result)
    result["pump"] = pump.pump_pressure(
        symbol, asof=None,
        ext_ret=(kelly.get("ext_ret") if kelly else None))
    return result

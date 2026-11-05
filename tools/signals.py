"""Compute academic earnings signals from collected raw data.

Each signal is normalized to a score in [-1, +1] where POSITIVE leans toward a
fundamental BEAT (or, for priced-in signals, toward "the move is already
expected"). Every signal carries a `present` flag and a short `evidence`
string so the report can explain itself and absent signals contribute nothing.

Signals are pure functions of a `data` dict so they can be reused unchanged by
both the live predictor and the backtester.
"""
from __future__ import annotations

import math
import statistics
from dataclasses import dataclass
from typing import Any, Dict, List, Optional


@dataclass
class Signal:
    name: str
    score: float            # [-1, +1]; + = leans beat / move-priced-in
    present: bool
    evidence: str


def _tanh(x: float) -> float:
    return math.tanh(x)


def _mean(xs: List[float]) -> Optional[float]:
    xs = [x for x in xs if x is not None]
    return sum(xs) / len(xs) if xs else None


def sig_sue_momentum(surprises: List[Dict[str, Any]]) -> Signal:
    """Standardized Unexpected Earnings momentum (Bernard-Thomas 1989; SUE defined
    per Livnat-Mendenhall 2006 / Foster-Olsen-Shevlin 1984).

    SUE standardizes the recent surprise by the firm's OWN surprise volatility, so a
    +3% beat for a steady firm counts more than +3% for an erratic one. Falls back to
    a flat scale when there's too little history to estimate the std-dev."""
    pcts = [s.get("surprisePercent") for s in surprises if s.get("surprisePercent") is not None]
    if not pcts:
        return Signal("sue_momentum", 0.0, False, "no surprise history")
    recent = _mean(pcts[:4])
    if len(pcts) >= 4:
        sd = statistics.pstdev(pcts[:12])
        if sd and sd > 0:
            sue = recent / sd
            return Signal("sue_momentum", _tanh(sue / 2.0), True,
                          f"SUE {sue:+.2f} (avg {recent:+.1f}% / std {sd:.1f})")
    return Signal("sue_momentum", _tanh(recent / 10.0), True,
                  f"avg surprise last {min(4,len(pcts))}q = {recent:+.1f}% (no std yet)")


def sig_dispersion(disp: Optional[Dict[str, Any]]) -> Signal:
    """Analyst forecast dispersion (Diether-Malloy-Scherbina 2002, Johnson 2004).

    Low dispersion + many analysts = a tight, predictable consensus (mildly positive);
    high dispersion = uncertainty and, empirically, weaker outcomes (negative)."""
    if not disp or disp.get("dispersion") is None:
        return Signal("dispersion", 0.0, False, "no estimate dispersion")
    d = disp["dispersion"]
    n = disp.get("n_analysts") or 0
    # Center near a 10% typical spread; tighter -> +, wider -> -.
    score = _tanh((0.10 - d) / 0.10)
    if n and n < 3:
        score *= 0.5  # thin coverage -> less trustworthy
    return Signal("dispersion", score, True,
                  f"dispersion {d:.0%} across {n} analysts")


def sig_surprise_streak(surprises: List[Dict[str, Any]]) -> Signal:
    """Beat rate over available quarters; persistent beaters tend to beat again."""
    flags = [1 if (s.get("surprise") or 0) > 0 else 0
             for s in surprises if s.get("surprise") is not None]
    if not flags:
        return Signal("surprise_streak", 0.0, False, "no surprise history")
    rate = sum(flags) / len(flags)
    streak = 0
    for f in flags:  # surprises are most-recent-first
        if f:
            streak += 1
        else:
            break
    score = _tanh((rate - 0.5) * 4 + streak * 0.15)
    return Signal("surprise_streak", score, True,
                  f"beat {sum(flags)}/{len(flags)}q, current streak {streak}")


def sig_analyst_revision(recs: List[Dict[str, Any]],
                         revisions: Optional[Dict[str, Any]] = None) -> Signal:
    """Analyst EPS-estimate revisions (Stickel 1991, Chan 1996).

    Preferred: REAL estimate revisions — net up/down analyst count + estimate
    drift vs 30-90d ago (yfinance). Fallback: recommendation-trend bullishness shift.
    """
    if revisions:
        up, dn = revisions.get("up"), revisions.get("down")
        trend = revisions.get("trend_pct")
        if up is not None and dn is not None and (up + dn) > 0:
            net = (up - dn) / (up + dn)
            score = _tanh(net * 2.0 + (trend or 0) / 10.0)
            return Signal("analyst_revision", score, True,
                          f"{int(up)} up / {int(dn)} down rev"
                          + (f", est {trend:+.1f}% vs prior" if trend is not None else ""))
        if trend is not None:
            return Signal("analyst_revision", _tanh(trend / 10.0), True,
                          f"estimate {trend:+.1f}% vs 30-90d ago")

    def bull(r: Dict[str, Any]) -> Optional[float]:
        tot = sum(r.get(k, 0) for k in ("strongBuy", "buy", "hold", "sell", "strongSell"))
        if tot == 0:
            return None
        return (2 * r.get("strongBuy", 0) + r.get("buy", 0)
                - r.get("sell", 0) - 2 * r.get("strongSell", 0)) / tot
    if len(recs) < 2:
        return Signal("analyst_revision", 0.0, False, "insufficient recommendation history")
    now, prev = bull(recs[0]), bull(recs[1])
    if now is None or prev is None:
        return Signal("analyst_revision", 0.0, False, "no recommendation counts")
    delta = now - prev
    return Signal("analyst_revision", _tanh(delta * 5.0), True,
                  f"analyst bullishness {delta:+.2f} MoM")


def sig_pre_earnings_drift(pre_ret: Optional[float]) -> Signal:
    """Short-horizon momentum into the print (Jegadeesh-Titman 1993)."""
    if pre_ret is None:
        return Signal("pre_earnings_drift", 0.0, False, "no price history")
    return Signal("pre_earnings_drift", _tanh(pre_ret / 0.10), True,
                  f"pre-earnings return {pre_ret:+.1%}")


def sig_over_extrapolation(pre_ret: Optional[float],
                           kelly: Optional[Dict[str, Any]] = None) -> Signal:
    """CENTRAL signal — Kelly et al. 2016 over-extrapolation.

    Preferred: the extrapolated-return measure (ExtRet) from a TradingView snapshot
    — recency-weighted average of the last 8 earnings-announcement returns. High
    ExtRet => investors over-extrapolate past good announcements => over-buy pre-print
    => beat already priced in + post-earnings reversal risk. POSITIVE score = strongly
    over-extrapolated (scoring uses it as a priced-in / reaction headwind).

    Fallback (no snapshot): extreme recent run-up beyond a +/-8% band.
    """
    if kelly and kelly.get("ext_ret") is not None:
        er = kelly["ext_ret"]
        streak = " streak+" if kelly.get("streak") else ""
        return Signal("over_extrapolation", _tanh(er / 0.04), True,
                      f"Kelly ExtRet {er:+.1%} over {kelly.get('n_quarters')}q{streak}")
    if pre_ret is None:
        return Signal("over_extrapolation", 0.0, False, "no snapshot / price history")
    excess = pre_ret - max(-0.08, min(0.08, pre_ret))
    return Signal("over_extrapolation", _tanh(excess / 0.10), excess != 0.0,
                  f"fallback run-up beyond +/-8% band = {excess:+.1%}")


def sig_rel_strength(rs: Dict[str, Optional[float]]) -> Signal:
    """Relative strength vs index and sector ETF."""
    vals = [rs.get("vs_index"), rs.get("vs_sector")]
    m = _mean([v for v in vals if v is not None])
    if m is None:
        return Signal("rel_strength", 0.0, False, "no benchmark data")
    return Signal("rel_strength", _tanh(m / 0.05), True,
                  f"outperformance avg {m:+.1%} vs index/sector")


def sig_volume_buildup(buildup: Optional[float]) -> Signal:
    """Pre-earnings volume vs baseline (Beaver 1968: volume = info arrival).
    Treated as priced-in pressure, not directional."""
    if buildup is None:
        return Signal("volume_buildup", 0.0, False, "no volume data")
    return Signal("volume_buildup", _tanh(buildup / 0.5), True,
                  f"recent volume {buildup:+.0%} vs baseline")


def sig_options_skew(options: Dict[str, Optional[float]]) -> Signal:
    """Put/call IV skew: positive skew = downside fear = bearish (negative score)."""
    skew = options.get("skew")
    if skew is None:
        return Signal("options_skew", 0.0, False, "no options chain")
    return Signal("options_skew", _tanh(-skew / 0.05), True,
                  f"put-call IV skew {skew:+.2f}")


def sig_options_move(options: Dict[str, Optional[float]]) -> Signal:
    """Implied expected-move size: bigger = more outcome already priced in
    (non-directional; feeds the priced-in index only)."""
    mv = options.get("implied_move_pct")
    if mv is None:
        return Signal("options_move", 0.0, False, "no options chain")
    return Signal("options_move", _tanh(mv / 0.06), True,
                  f"options implied move {mv:.1%}")


# Lightweight headline lexicon for a free news-sentiment proxy. Not NLP — a
# transparent keyword tilt the report can explain. Tune freely.
_POS_WORDS = ("beat", "beats", "raise", "raised", "upgrade", "upgraded", "record",
              "strong", "surge", "soar", "tops", "outperform", "bullish", "growth",
              "wins", "approval", "expands", "guidance raise")
_NEG_WORDS = ("miss", "misses", "cut", "cuts", "downgrade", "downgraded", "weak",
              "plunge", "slump", "lawsuit", "probe", "recall", "warning", "bearish",
              "layoff", "delay", "investigation", "guidance cut", "falls")


import json as _json
import os as _os
import re as _re

_LM_PATH = _os.path.join(_os.path.dirname(__file__), "lm_dict.json")
_LM_CACHE: Optional[Dict[str, frozenset]] = None
_TOKEN = _re.compile(r"[a-z]+")


def _lm() -> Optional[Dict[str, frozenset]]:
    """Load the Loughran-McDonald FINANCE sentiment dictionary (public, sraf.nd.edu)
    parsed into category word-sets. Cached. None if the file is missing."""
    global _LM_CACHE
    if _LM_CACHE is None:
        try:
            raw = _json.loads(open(_LM_PATH).read())
            _LM_CACHE = {k: frozenset(v) for k, v in raw.items()}
        except Exception:
            _LM_CACHE = {}
    return _LM_CACHE or None


def sig_news_sentiment(news: List[Dict[str, Any]]) -> Signal:
    """News tone via the Loughran-McDonald FINANCE dictionary (Loughran-McDonald 2011/2020;
    Tetlock 2007; Tetlock-Saar-Tsechansky 2008). Generic dictionaries misclassify ~75% of
    'negative' words in financial text (cost, tax, liability, capital are not negative here),
    so LM's finance-specific lists matter. Tone = (pos-neg)/(pos+neg+1) over recent company
    news, damped when uncertainty words are heavy (hedged/ambiguous tone = less informative).
    Whole-word matched. Falls back to the legacy keyword list if the LM dict is unavailable."""
    if not news:
        return Signal("news_sentiment", 0.0, False, "no recent news")
    lm = _lm()
    if lm:
        neg_set = lm.get("negative", frozenset())
        pos_set = lm.get("positive", frozenset())
        unc_set = lm.get("uncertainty", frozenset())
        pos = neg = unc = ntok = 0
        for n in news:
            for tok in _TOKEN.findall((n.get("headline", "") + " " + n.get("summary", "")).lower()):
                ntok += 1
                if tok in neg_set: neg += 1
                if tok in pos_set: pos += 1
                if tok in unc_set: unc += 1
        if pos + neg == 0:
            return Signal("news_sentiment", 0.0, True, f"{len(news)} headlines, no LM tone words")
        tone = (pos - neg) / (pos + neg + 1)
        unc_frac = unc / ntok if ntok else 0.0          # heavy hedging dilutes the read
        damp = max(0.4, 1.0 - 4.0 * unc_frac)
        return Signal("news_sentiment", _tanh(tone * 2.0) * damp, True,
                      f"LM {len(news)} headlines: {pos} pos / {neg} neg / {unc} unc")
    pos = neg = 0
    for n in news:
        text = (n.get("headline", "") + " " + n.get("summary", "")).lower()
        pos += sum(1 for w in _POS_WORDS if w in text)
        neg += sum(1 for w in _NEG_WORDS if w in text)
    total = pos + neg
    if total == 0:
        return Signal("news_sentiment", 0.0, True, f"{len(news)} headlines, no clear tone")
    return Signal("news_sentiment", _tanh((pos - neg) / total * 2.0), True,
                  f"{len(news)} headlines, {pos} pos / {neg} neg (legacy)")


def sig_piotroski(piotroski: Optional[Dict[str, Any]]) -> Signal:
    """Piotroski (2000) F-Score — a 9-point fundamental-strength composite (profitability,
    leverage/liquidity, operating efficiency). High F = a financially strengthening firm,
    a quality prior that lifts the beat base rate; low F = fragile. Orthogonal to the
    surprise-history and price-momentum signals. Free from yfinance financials; absent when
    too few of the 9 components are computable."""
    if not piotroski or piotroski.get("score_norm") is None:
        return Signal("piotroski", 0.0, False, "no financials for F-score")
    return Signal("piotroski", piotroski["score_norm"], True,
                  f"F-score {piotroski['f_score']}/{piotroski['n_components']}")


def sig_pump(pump_score: Optional[float]) -> Signal:
    """PUMP pre-earnings pressure as a beat signal. PUMP (-100..100) = abnormal 5-day run-up
    normalized by the stock's own expected move. Positive run-up mildly precedes a beat
    (validated OOS: adding PUMP lowered held-out Brier). Grouped with price-momentum signals
    so it doesn't double-count the pre-earnings drift. Score = PUMP/100 -> [-1,1]."""
    if pump_score is None:
        return Signal("pump", 0.0, False, "no pre-earnings price data")
    return Signal("pump", max(-1.0, min(1.0, pump_score / 100.0)), True,
                  f"pre-earnings pressure {pump_score:+.0f}/100")


def sig_peer_beats(peer: Optional[Dict[str, Any]]) -> Signal:
    """Sector tailwind: fraction of peers that beat last quarter. Broad peer
    beats lift this name's base rate."""
    if not peer or peer.get("rate") is None:
        return Signal("peer_beats", 0.0, False, "no peer data")
    rate = peer["rate"]
    return Signal("peer_beats", _tanh((rate - 0.5) * 4), True,
                  f"{int(rate*peer['n'])}/{peer['n']} peers beat last qtr")


def sig_short_interest(short_pct: Optional[float]) -> Signal:
    """Short interest as % of float. High short interest => squeeze potential on a
    beat (positive for a post-earnings pop). Reaction-only signal."""
    if short_pct is None:
        return Signal("short_interest", 0.0, False, "no short-interest data")
    return Signal("short_interest", _tanh((short_pct - 0.05) / 0.10), True,
                  f"short interest {short_pct:.1%} of float")


def sig_accruals(accruals: Optional[float]) -> Signal:
    """Earnings quality via accruals (Sloan 1996). Low/negative accruals = cash-backed,
    persistent earnings (positive); high accruals = low quality, mean-reverting (negative)."""
    if accruals is None:
        return Signal("accruals", 0.0, False, "no quarterly financials")
    return Signal("accruals", _tanh(-accruals / 0.05), True,
                  f"accruals ratio {accruals:+.3f} of assets")


def sig_filing_risk(fr: Optional[Dict[str, Any]]) -> Signal:
    """SEC 8-K event risk (free alternative to paid event/guidance feeds). Bearish-only:
    restructuring, officer departures, impairments, restatements before the print raise
    miss risk. Absence is neutral (0), never bullish."""
    if not fr or fr.get("risk") is None:
        return Signal("filing_risk", 0.0, False, "no EDGAR filings")
    risk = fr["risk"]
    if risk <= 0:
        return Signal("filing_risk", 0.0, True, "no adverse 8-K items recently")
    return Signal("filing_risk", -_tanh(risk * 1.5), True,
                  "adverse 8-K: " + "; ".join(fr.get("flags", [])[:2]))


def sig_fundamental_trend(ft: Optional[Dict[str, Any]]) -> Signal:
    """Revenue deceleration + operating-margin compression (Sloan-adjacent quality).
    Deteriorating fundamentals precede misses (UAA pattern); improving = positive."""
    if not ft or (ft.get("rev_growth") is None and ft.get("margin_delta") is None):
        return Signal("fundamental_trend", 0.0, False, "no quarterly financials")
    rg = ft.get("rev_growth") or 0.0
    md_ = ft.get("margin_delta") or 0.0
    score = _tanh(rg / 0.15 * 0.5 + md_ / 0.04 * 0.5)
    if ft.get("op_margin") is not None and ft["op_margin"] < 0:
        score = min(score, -0.2)  # outright unprofitable -> bearish floor
    return Signal("fundamental_trend", score, True,
                  f"rev growth {rg:+.0%}, op-margin Δ {md_:+.1%}")


def compute_all(data: Dict[str, Any]) -> Dict[str, Signal]:
    """Build every signal from the assembled raw-data dict."""
    surprises = data.get("surprises", []) or []
    recs = data.get("recommendations", []) or []
    pre_ret = data.get("pre_earnings_return")
    rs = data.get("rel_strength", {}) or {}
    buildup = data.get("volume_buildup")
    options = data.get("options", {}) or {}
    news = data.get("news", []) or []
    kelly = data.get("kelly")
    sigs = [
        sig_sue_momentum(surprises),
        sig_surprise_streak(surprises),
        sig_analyst_revision(recs, data.get("revisions")),
        sig_pre_earnings_drift(pre_ret),
        sig_over_extrapolation(pre_ret, kelly),
        sig_rel_strength(rs),
        sig_volume_buildup(buildup),
        sig_options_skew(options),
        sig_options_move(options),
        sig_news_sentiment(news),
        sig_peer_beats(data.get("peer_beats")),
        sig_short_interest(data.get("short_interest")),
        sig_dispersion(data.get("dispersion")),
        sig_accruals(data.get("accruals")),
        sig_fundamental_trend(data.get("fundamental_trend")),
        sig_filing_risk(data.get("filing_risk")),
        sig_piotroski(data.get("piotroski")),
        sig_pump(data.get("pump_score")),
    ]
    out = {s.name: s for s in sigs}
    _confirm_pump(out)   # SCAR-style: a STRETCHED run-up only counts when other signals agree
    return out


# Signals that carry a real directional beat/miss lean (exclude pure priced-in / reaction ones).
_LEAN_SIGNALS = ("sue_momentum", "surprise_streak", "analyst_revision", "fundamental_trend",
                 "accruals", "piotroski", "news_sentiment", "filing_risk", "peer_beats")


def _confirm_pump(sigs: Dict[str, "Signal"]) -> None:
    """Make PUMP more RELIABLE the way SCAR does — by confirmation. Backtest: raw PUMP is weak,
    but a STRETCHED run-up (|PUMP|>=40) that AGREES with the other signals' direction predicts
    a beat ~93% (vs 86% base), while one that CONFLICTS is noise/reversal (Kelly). So: amplify a
    stretched, agreeing PUMP; damp a stretched PUMP that fights the fundamentals. Sub-threshold
    PUMP is left alone. Adjusts the 'pump' signal in place; the OOS auto-fitter judges its weight."""
    p = sigs.get("pump")
    if not p or not p.present or abs(p.score) < 0.40:   # only act on STRETCHED pump
        return
    lean = num = den = 0.0
    for n in _LEAN_SIGNALS:
        s = sigs.get(n)
        if s and s.present and s.score != 0:
            num += s.score
            den += abs(s.score)
    if den == 0:
        return
    lean = num / den                      # net directional lean of the other signals, [-1,1]
    agree = (p.score > 0) == (lean > 0)
    factor = min(1.0 + 0.5 * abs(lean), 1.5) if agree else max(1.0 - 0.7 * abs(lean), 0.3)
    sigs["pump"] = Signal("pump", max(-1.0, min(1.0, p.score * factor)), True,
                          p.evidence + (f" · confirmed×{factor:.2f}" if agree else f" · CONFLICTS×{factor:.2f}"))

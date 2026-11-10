"""Polymarket edge scanner (attacks gaps 1 + 4).

Fans out across live Polymarket earnings/stock markets, runs the model on each,
and ranks by model-vs-market divergence (net + executable edge). This is how you
FIND the few markets where the crowd disagrees with the model — instead of
checking one ticker at a time. Every scanned market is auto-logged to the forward
vault, so the track record fills itself.
"""
from __future__ import annotations

import re
from typing import Any, Dict, List

from tools import edge_log, predict as predict_mod, report
from tools import validation as V
from tools.polymarket_client import search_markets

_TICKER_RE = re.compile(r"\(([A-Z]{1,5})\)")


def _ticker_from(question: str) -> str:
    m = _TICKER_RE.search(question or "")
    return m.group(1) if m else ""


def _family(outcome):
    if outcome in ("eps_beat", "eps_surprise_over"):
        return "eps"
    if outcome in ("revenue_beat", "revenue_over"):
        return "revenue"
    if outcome == "stock_up":
        return "stock"
    return None


# Search terms per outcome family — surface the right kind of market. Wider net = more
# candidate markets found (the bottleneck was too few search terms).
_TERMS = {
    "eps": ["beat earnings", "earnings beat", "eps", "quarterly earnings",
            "earnings report", "Q2 earnings", "Q3 earnings", "miss earnings", "earnings"],
    "revenue": ["revenue beat", "revenue", "sales", "quarterly revenue",
                "Q2 revenue", "Q3 revenue", "earnings"],
    "stock": ["close above", "finish above", "stock above", "stock price"],
    # Default (no outcome chosen): EPS + revenue only — NOT stock-price targets, which we
    # proved are a coin-flip to price (would only manufacture fake edges).
    None: ["beat earnings", "earnings beat", "quarterly earnings", "earnings report",
           "revenue beat", "Q2 earnings", "Q3 earnings", "eps", "revenue"],
}


def write_landscape(result: Dict[str, Any], stamp: str) -> None:
    """Append a one-line weekly snapshot of the Polymarket landscape to
    output/scan_landscape.csv (queryable history of how many markets/edges existed each
    run), and raise a LOUD flag file the moment a TRUSTWORTHY (tradeable) edge appears so
    the weekly cron run can't bury it. `stamp` is passed in (no Date.now in callers' way)."""
    from tools.config import OUTPUT_DIR
    rows = result.get("rows", [])
    trade = [r for r in rows if r.get("trustworthy")]
    best = max(rows, key=lambda r: r.get("edge_quality", -9), default=None)
    land = OUTPUT_DIR / "scan_landscape.csv"
    new = not land.exists()
    with open(land, "a") as fh:
        if new:
            fh.write("scanned_at,markets,tradeable,best_ticker,best_outcome,"
                     "best_net_edge,best_edge_quality,best_max_usd,"
                     "best_expected_profit_usd,best_paper_only\n")
        b = best or {}
        # NOTE: best_edge_quality changed units on 2026-11-03 — it was an edge FRACTION,
        # it is now conviction-weighted EXPECTED DOLLAR PROFIT. Rows before that date are
        # fractions; the explicit $ column starts then and is blank for older rows.
        fh.write(f"{stamp},{len(rows)},{len(trade)},{b.get('ticker','')},"
                 f"{b.get('outcome','')},{b.get('net_edge','')},"
                 f"{b.get('edge_quality','')},{b.get('max_usd','')},"
                 f"{b.get('expected_profit_usd','')},{b.get('paper_only','')}\n")
    flag = OUTPUT_DIR / "EDGE_FOUND.txt"
    if trade:
        with open(flag, "a") as fh:
            for r in trade:
                fh.write(f"{stamp}  {r['ticker']} {r['outcome']} side={r.get('side')} "
                         f"net_edge={r.get('net_edge')} exec={r.get('executable_edge')} "
                         f"max=${r.get('max_usd')} SCAR={r.get('predictability')} "
                         f"SMG_psi={r.get('smg_psi')} | {r.get('url')}\n")


def scan(limit: int = 12, outcome: str = None, threshold: float = None,
         terms: List[str] = None) -> Dict[str, Any]:
    """Gather open markets matching the chosen OUTCOME family, evaluate each for that
    outcome (+ threshold), rank by edge, auto-log. outcome=None -> infer per market."""
    fam = _family(outcome)
    terms = terms or _TERMS.get(fam, _TERMS[None])
    seen, cands = set(), []
    for term in terms:
        for c in search_markets(term, limit=20):
            if c.get("closed") or c["slug"] in seen:
                continue
            tk = _ticker_from(c.get("question", ""))
            if not tk:
                continue
            inf = c.get("inferred") or {}
            cfam = _family(inf.get("outcome"))
            # If the user picked an outcome, only scan markets that resolve on THAT family.
            if fam and cfam != fam:
                continue
            # Default scan (no outcome): skip stock-price-target markets — direction is a
            # coin-flip on free data, so any "edge" there is noise. Keep eps/revenue only.
            if not fam and cfam == "stock":
                continue
            # PRICE PRE-FILTER (2026-11-10). A candidate priced outside [0.05, 0.95] is a
            # market that has effectively already settled: the book has no size on the side
            # we would buy, predict() returns no side, and the row logs as a non-bet. In the
            # 2026-11-05..10 scans, 23 of 43 slots were spent this way — over half the daily
            # predict budget producing nothing scoreable. The price is already in the search
            # payload, so skipping costs nothing and frees those slots for live markets.
            # NOT a quality judgement: these markets are still real, just no longer tradeable.
            yp = c.get("yes_price")
            if yp is not None and not (V.MIN_PRICE < float(yp) < 1 - V.MIN_PRICE):
                continue
            # GAAP PRE-FILTER (2026-11-10). Polymarket encodes the EPS basis in its own
            # slug: "...-quarterly-earnings-gaap-eps-..." vs "...-nongaap-eps-...".
            # predict() already REFUSES GAAP contracts (our consensus and surprise history
            # are non-GAAP, so pricing one compares two different measures — it produced a
            # fake 49-point "edge" on UBER). Spending a predict call to reach that refusal
            # wastes a scan slot. Checked against every row carrying both fields: 8/8 agree
            # with the basis parsed from the resolution text.
            #
            # Deliberately ASYMMETRIC: skip only when the slug says gaap EXPLICITLY, never
            # on silence. A silent slug still gets predicted and refused by the real parser,
            # so a mislabelled slug costs a wasted call, never a mispriced bet. This changes
            # only WHERE the refusal happens, never WHETHER it happens.
            _s = (c.get("slug") or "").lower()
            if "gaap" in _s and "nongaap" not in _s and "non-gaap" not in _s:
                continue
            seen.add(c["slug"])
            c["ticker"] = tk
            cands.append(c)

    # Dedup by (ticker, outcome family): keep the single most-liquid market per ticker so
    # we don't burn the predict budget on 5 threshold-variants of the same name -> more
    # unique tickers actually get scanned.
    best: Dict[Any, Dict[str, Any]] = {}
    for c in cands:
        key = (c["ticker"], _family((c.get("inferred") or {}).get("outcome")))
        if key not in best or c.get("volume", 0) > best[key].get("volume", 0):
            best[key] = c
    cands = sorted(best.values(), key=lambda c: -c.get("volume", 0))

    rows: List[Dict[str, Any]] = []
    for c in cands[:limit]:
        inf = c.get("inferred") or {}
        use_outcome = outcome or inf.get("outcome") or "eps_beat"
        use_thr = threshold if (outcome and threshold is not None) else inf.get("threshold")
        try:
            r = predict_mod.predict(c["ticker"], outcome=use_outcome,
                                    threshold=use_thr, polymarket_url=c["slug"])
        except Exception:
            continue
        if "error" in r or r.get("model_prob") is None:
            continue
        edge = r.get("edge") or {}
        edge_log.log_edge(r, notes="scan")
        # STRIKE-GAP forward experiment (pre-registered strike_gap_v1): if the contract has a real
        # frozen strike, log the structural strike edge as a distinct 'eps_strike' bet so the
        # forward vault can judge route A over time. Logged whether candidate or watch.
        _se = r.get("strike_edge") or {}
        if _se.get("side") in ("YES", "NO") and r.get("contract_strike") is not None:
            edge_log.log_strike_edge(r)
        _mp = r["model_prob"]
        _q = (r.get("market") or {}).get("yes_price")
        # FADE-THE-BEAT: market confidently prices a BEAT but our model says MISS — the user's
        # #1 scenario. Conditions: q>=0.70 (implied miss <=30%), model<=0.50, >=2 bearish
        # confirmers (backtest: lifts real-miss precision ~27%->33%), AND — crucially — SCAR
        # conviction >=45. That last gate makes the FADE TAKE and SCAR AGREE: if SCAR doesn't
        # trust the miss call (signals conflict, like UBER at SCAR 9), it is NOT a fade — it
        # falls through to a low-conviction "potential" instead. No more contradictory takes.
        _fade_beat = (r["outcome"] in ("eps_beat", "revenue_beat")
                      and _q is not None and _q >= 0.70 and _mp <= 0.50
                      and (r.get("bear_signals") or 0) >= 2
                      and (r.get("scar_predictability") or 0) >= 45)
        rows.append({
            "ticker": c["ticker"], "question": c.get("question"),
            "slug": c["slug"], "url": c.get("url"),
            "earnings_date": r.get("earnings_date"),
            "outcome": r["outcome"], "threshold": use_thr,
            "model_prob": _mp,
            "market_prob": _q,
            "fade_beat": _fade_beat,
            "strike_edge": r.get("strike_edge"),
            "contract_strike": r.get("contract_strike"),
            "strike_edge_candidate": bool((r.get("strike_edge") or {}).get("structural_candidate")),
            "net_edge": edge.get("net_edge"),
            "executable_edge": edge.get("executable_edge"),
            "max_usd": edge.get("max_tradeable_usd"),
            "edge_at_max": edge.get("edge_at_max"),
            "expected_profit_usd": edge.get("expected_profit_usd"),
            "top_of_book_usd": edge.get("top_of_book_usd"),
            "fill_reason": edge.get("fill_reason"),
            # Price of the contract we would BUY (YES costs q, NO costs 1-q). A NO bet on a
            # 0.80 market is a 0.20 longshot purchase — the zone that loses >60% in the
            # 46k-contract Kalshi study and whose variance makes any edge unmeasurable.
            "contract_cost": edge.get("contract_cost"),
            "is_longshot": edge.get("is_longshot"),
            "is_deep_longshot": edge.get("is_deep_longshot"),
            # A "gap" measured against the MID of a 23-cent spread is mostly fiction —
            # carry the spread so a wide-book divergence can never masquerade as an edge.
            "book_spread": (r.get("market") or {}).get("book_spread"),
            # Raw book + contract basis, so the paper-maker (tools/maker.py) can quote
            # against the real touch instead of the mid, and skip GAAP contracts.
            "yes_bid": (r.get("market") or {}).get("best_bid"),
            "yes_ask": (r.get("market") or {}).get("best_ask"),
            "depth_usd": (r.get("market") or {}).get("depth_usd"),
            "eps_basis": r.get("contract_basis"),
            "side": edge.get("side"), "confidence": r["confidence"],
            "predictability": r.get("scar_predictability"),
            "smg": r.get("smg"), "pump": r.get("pump"),
            "smg_psi": (r.get("smg") or {}).get("psi"),
            "smg_live": (r.get("smg") or {}).get("live"),
            "recommendation": edge.get("recommendation"),
            # Deep-dive payload: full readout + improvement recommendations.
            "report_rows": [[label, extract(r)] for label, extract in report.ROWS],
            "recommendations": r.get("recommendations", []),
            "evidence": r.get("evidence", []),
            "risks": r.get("risks", []),
        })
    # TRUSTWORTHY edge = executable + survives liquidity + SCAR predictability >= 45.
    for r in rows:
        _q0 = r.get("market_prob")
        r["trustworthy"] = ((r.get("max_usd") or 0) > 0 and (r.get("executable_edge") or 0) > 0
                            and (r.get("predictability") or 0) >= 45
                            and _q0 is not None and 0.05 < _q0 < 0.95
                            # never call a deep-longshot purchase trustworthy: documented
                            # >60% average loss, and unmeasurable variance
                            and not r.get("is_deep_longshot"))
        # Edge QUALITY = CONVICTION-WEIGHTED EXPECTED DOLLAR PROFIT (rewritten 2026-11-03).
        #
        # It used to be an edge FRACTION, and it fell back to the paper net_edge whenever the
        # book was unreadable. Both were wrong in the same direction: a 51% "edge" in a 3-cent
        # book that fills $20 outranked a 5% edge that fills $5,000. That ranking is what fed
        # the vault the unfillable bets whose fictional P&L produced the 2026-10-30 fake
        # "EDGE CONFIRMED". Rank by the money actually on the table, after walking the book:
        #     quality = expected_profit_usd * (SCAR / 100)
        # No paper fallback — a market we cannot fill scores 0 and is flagged paper_only. It is
        # still scanned, still logged and still shown (so nothing is hidden), just never ranked
        # above something real.
        conv = (r.get("predictability") or 0) / 100.0
        profit = r.get("expected_profit_usd")
        # A price pinned at the extremes is a resolved/phantom book, not a 99% opinion —
        # same exclusion the forward scorer uses (MIN_Q), so scanning and scoring agree.
        _q = r.get("market_prob")
        degenerate = _q is None or not (0.05 < _q < 0.95)
        if degenerate and not r.get("fill_reason"):
            r["fill_reason"] = f"price pinned at {_q} — phantom/resolved book, not a real quote"
        r["paper_only"] = bool(degenerate) or not (
            (r.get("max_usd") or 0) > 0 and profit is not None and profit > 0)
        r["edge_quality"] = 0.0 if r["paper_only"] else round(profit * conv, 2)
        # Keep the old fraction for display/compat — it is informative, just not rankable.
        exe = r.get("executable_edge")
        r["edge_fraction"] = round((exe if exe is not None else (r.get("net_edge") or 0)) * conv, 4)
    # Rank: trustworthy first, then fillable-at-all, then SMG-live, then expected $ profit.
    # Rank: trustworthy, then fillable, then FAVORITE purchases ahead of longshot ones
    # (2026-11-05), then SMG-live, then expected $ profit. The favorite leg is grounded in
    # Burgi-Deng-Whelan plus our own cost-bucket data (win rate 0.10 at <10c vs 0.83 at
    # >70c), not in a search of our own P&L.
    rows.sort(key=lambda x: (x["trustworthy"], not x.get("paper_only", True),
                             not x.get("is_longshot", False),
                             bool(x.get("smg_live")), x.get("edge_quality", -9)),
              reverse=True)
    tradeable = [r for r in rows if r["trustworthy"]]
    # Persist to the Edge Book (accumulates real + still-active potential across scans).
    try:
        from datetime import datetime
        from tools import edge_book
        def _gap(r):
            mp, q = r.get("model_prob"), r.get("market_prob")
            return abs(mp - q) if (mp is not None and q is not None) else 0
        believable = [r for r in rows if r.get("trustworthy")]
        # Potential still includes paper-only divergences (you asked to see every potential
        # edge) — they are just ranked last and carry paper_only so nothing dresses an
        # unfillable 3-cent gap up as money.
        potential = [r for r in rows if not r.get("trustworthy") and _gap(r) >= 0.15]
        potential.sort(key=lambda r: (not r.get("paper_only", True), r.get("edge_quality", 0)),
                       reverse=True)
        edge_book.upsert(believable, potential, datetime.now().isoformat(timespec="seconds"))
    except Exception:
        pass
    return {"scanned": len(rows), "tradeable": len(tradeable), "rows": rows}

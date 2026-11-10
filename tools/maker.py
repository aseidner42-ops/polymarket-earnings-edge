"""Maker quote sheet — paper market making, forward-tested.

WHY THIS EXISTS
---------------
We have spent five months as a TAKER: lifting the ask, paying the spread, trying to prove
an edge of a few percent. The arithmetic of that is bad and was measured directly:

    live earnings books (n=10)   mean spread 8.0c   median 5.5c   max 23c
    half-spread a maker captures / a taker pays     4.0c
    as a return on a 0.50 contract                  +8.0%
    documented taker-side edge on >=50c contracts   +2.6%
                                                    -> the spread is 3.1x the edge

We pay that 4 cents on every single trade, to chase something a third its size. Meanwhile
Bürgi-Deng-Whelan (46,282 Kalshi contracts) find the positive return belongs to MAKERS and
the favorite-longshot bias is worse on the TAKER side. We have been structurally on the
wrong side of every trade we make.

This module asks the opposite question: if we POSTED a quote at our model's fair value and
waited, what would have happened?

WHAT IT DOES, AND DOES NOT, DO
------------------------------
It generates quotes and logs them. It never places an order. Nothing here touches money;
the hard rule against auto-trading is unchanged. A quote is a row in a CSV with a
timestamp, and later `score_quotes` checks it against where the market actually went.

THE MEASUREMENT THAT MATTERS: ADVERSE SELECTION
-----------------------------------------------
Any fool can claim the spread on paper. The question that decides whether market making
works is whether you get filled *precisely when you are wrong*. A resting bid is a free
option written to anyone who knows more than you: it gets hit when news arrives, and
ignored when it doesn't. So `score_quotes` measures three things separately:

    fill_rate       did the market ever trade through our quote
    spread_earned   what we'd have been paid for providing liquidity
    adverse_pnl     how those fills actually resolved

If the spread earned is positive but the fills resolve badly, market making does not work
here — and that finding is worth more than any edge estimate, because it is cheap and it
arrives in weeks rather than the ~2,400 bets a taker edge would need.

SCOPE (set 2026-11-05)
----------------------
Quote only FAVORITE purchases — the contract we would buy costs >= 0.50 — in books with at
least $25 of real size. That is the low-variance zone registered as favorite_zone_v1, and
the only one where a result would be interpretable at a sample size we can collect.
"""
from __future__ import annotations

import csv
from collections import Counter
from datetime import datetime
from typing import Any, Dict, List, Optional

from tools import validation as V
from tools.config import OUTPUT_DIR

QUOTE_LOG = OUTPUT_DIR / "maker_quotes.csv"

FIELDS = [
    "quoted_at", "ticker", "outcome", "slug", "url", "earnings_date",
    "model_prob", "mid", "best_bid", "best_ask", "book_spread", "depth_usd",
    "side", "contract_cost_if_filled", "our_bid", "our_ask", "edge_vs_fair",
    "size_usd", "scar", "eps_basis", "rationale",
    # filled in later by score_quotes
    "resolved", "filled", "fill_price", "spread_earned", "outcome_actual", "pnl", "notes",
    # WHICH test detected the fill. A daily book snapshot can only see a cross that happened
    # to persist until the poll, so "zero fills" from that test alone is not evidence of
    # anything. Recorded so the two detection methods can never be conflated.
    "fill_method",
]

# How far inside the touch we post. Quoting AT the touch is just being a taker with extra
# steps; quoting too far inside gives away the spread we are trying to earn. One cent
# inside is the smallest improvement that takes queue priority on a 1c-tick book.
IMPROVE = 0.01
MIN_EDGE_VS_FAIR = 0.02     # don't post unless our price is this far from fair value
MIN_SPREAD = 0.03           # below this there is not enough spread to be worth capturing


def fair_value(model_prob: float, scar: Optional[float], mid: float,
               lambda_max: float = 0.40) -> float:
    """Our fair value, shrunk toward the market by conviction.

    Same SCAR shrinkage the edge engine uses: P = mid + (SCAR/100)*lambda_max*(model-mid).
    Low conviction means we barely deviate from the crowd — which for a maker is the right
    default, because the crowd's mid is already a better forecast than most of what we have.
    """
    lam = (float(scar or 0) / 100.0) * lambda_max
    return mid + lam * (float(model_prob) - mid)


def _rej(reasons, name: str) -> None:
    """Record why a market was not quoted, then reject it."""
    if reasons is not None:
        reasons[name] += 1
    return None


def build_quote(row: Dict[str, Any], reasons=None) -> Optional[Dict[str, Any]]:
    """Turn one scanned market into a quote, or None if we should not quote it.

    `row` is a scanner row (it already carries the book, model probability, SCAR, cost and
    basis). Every rejection is a documented rule, not a judgement call.

    Pass a Counter as `reasons` to get the rejection tally. Without it the funnel is
    invisible: on 2026-11-10 the quote log held 11 rows after 5 days with no way to tell
    whether the cause was a thin universe or a filter set too tight, and the kill criterion
    for maker_fill_v1 needs 60 quotes over 3 weeks to be reachable at all.
    """
    mkt_mid = row.get("market_prob")
    bid, ask = row.get("yes_bid"), row.get("yes_ask")
    if bid is None or ask is None:
        bid, ask = (row.get("market") or {}).get("best_bid"), (row.get("market") or {}).get("best_ask")
    model = row.get("model_prob")
    if mkt_mid is None or model is None or bid is None or ask is None:
        return _rej(reasons, "no book or no model prob")
    # Same exclusions the scorer uses, so quoting and scoring never disagree.
    if not (V.MIN_PRICE < float(mkt_mid) < 1 - V.MIN_PRICE):
        return _rej(reasons, "price outside [0.05,0.95] (settled)")
    if (row.get("eps_basis") or "") == "gaap":
        return _rej(reasons, "GAAP contract (unpriceable)")
    spread = float(ask) - float(bid)
    if spread < MIN_SPREAD:
        return _rej(reasons, f"spread < {MIN_SPREAD:.2f}")
    if V.executable(row.get("max_usd"), row.get("depth_usd")) is not True:
        return _rej(reasons, "no executable size behind the quote")

    fv = fair_value(model, row.get("predictability"), float(mkt_mid))
    # Which side do we want to BE LONG if filled? Buy YES if fair value is above the mid.
    side = "YES" if fv >= float(mkt_mid) else "NO"
    # FAVORITE-ONLY SCOPE: the contract we'd end up holding must cost >= 0.50.
    # Buying YES costs our bid; buying NO costs 1 - (our ask).
    if side == "YES":
        our_bid = round(min(fv - MIN_EDGE_VS_FAIR, float(bid) + IMPROVE), 3)
        cost_if_filled = our_bid
        our_ask = None
        edge = fv - our_bid
    else:
        our_ask = round(max(fv + MIN_EDGE_VS_FAIR, float(ask) - IMPROVE), 3)
        cost_if_filled = round(1.0 - our_ask, 3)
        our_bid = None
        edge = our_ask - fv
    if cost_if_filled < V.FAVORITE_COST:
        return _rej(reasons, "longshot purchase (cost < 0.50) — out of registered scope")
    if not (0.01 < cost_if_filled < 0.99):
        return _rej(reasons, "degenerate cost")
    if edge < MIN_EDGE_VS_FAIR:
        return _rej(reasons, f"our price < {MIN_EDGE_VS_FAIR:.2f} from fair value")

    return {
        "quoted_at": datetime.now().isoformat(timespec="seconds"),
        "ticker": row.get("ticker"), "outcome": row.get("outcome"),
        "slug": row.get("slug"), "url": row.get("url"),
        "earnings_date": row.get("earnings_date"),
        "model_prob": round(float(model), 4), "mid": round(float(mkt_mid), 4),
        "best_bid": bid, "best_ask": ask, "book_spread": round(spread, 4),
        "depth_usd": row.get("depth_usd"),
        "side": side, "contract_cost_if_filled": cost_if_filled,
        "our_bid": our_bid, "our_ask": our_ask, "edge_vs_fair": round(float(edge), 4),
        "size_usd": min(float(row.get("max_usd") or 0), 100.0),
        "scar": row.get("predictability"), "eps_basis": row.get("eps_basis"),
        "rationale": (f"fair {fv:.3f} vs mid {float(mkt_mid):.3f}; post "
                      f"{'bid ' + format(our_bid, '.3f') if our_bid else 'ask ' + format(our_ask, '.3f')}; "
                      f"spread {spread:.3f}; cost if filled {cost_if_filled:.3f}"),
        "resolved": "", "filled": "", "fill_price": "", "spread_earned": "",
        "outcome_actual": "", "pnl": "", "notes": "paper-maker",
    }


LAST_REJECTIONS: "Counter[str]" = Counter()


def build_sheet(rows: List[Dict[str, Any]], limit: int = 25) -> List[Dict[str, Any]]:
    """Quotes for a scan, best edge-vs-fair first.

    Side effect: the rejection tally for this call lands in LAST_REJECTIONS, so the
    quote funnel can be read instead of inferred.
    """
    global LAST_REJECTIONS
    reasons: "Counter[str]" = Counter()
    out = [q for q in (build_quote(r, reasons) for r in rows) if q]
    LAST_REJECTIONS = reasons
    out.sort(key=lambda q: -q["edge_vs_fair"])
    return out[:limit]


def log_quotes(quotes: List[Dict[str, Any]]) -> int:
    """Append to the quote log, skipping a same-day repeat of the same market/side."""
    existing = read_quotes()
    seen = {(q.get("slug"), q.get("side"), (q.get("quoted_at") or "")[:10]) for q in existing}
    fresh = [q for q in quotes
             if (q["slug"], q["side"], q["quoted_at"][:10]) not in seen]
    if not fresh:
        return 0
    new_file = not QUOTE_LOG.exists()
    with open(QUOTE_LOG, "a", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=FIELDS)
        if new_file:
            w.writeheader()
        for q in fresh:
            w.writerow({k: q.get(k, "") for k in FIELDS})
    return len(fresh)


def read_quotes() -> List[Dict[str, Any]]:
    if not QUOTE_LOG.exists():
        return []
    with open(QUOTE_LOG, newline="") as fh:
        return list(csv.DictReader(fh))


def _f(v) -> Optional[float]:
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def check_fills(quotes: List[Dict[str, Any]], book_lookup) -> int:
    """Would a resting quote have been hit? Marks fills in place.

    `book_lookup(slug) -> {best_bid, best_ask}` supplies the CURRENT book. A resting BID is
    filled when the market's best ask drops to or below it (someone sold into us); a resting
    ASK is filled when the best bid rises to or above it. This is a conservative proxy — it
    needs the quoted price to be crossed, not merely touched — and it ignores queue position,
    which means real fill rates would be LOWER than measured here, never higher.
    """
    n = 0
    for q in quotes:
        if q.get("filled") not in (None, "", "no"):
            continue
        book = book_lookup(q.get("slug")) or {}
        bb, ba = _f(book.get("best_bid")), _f(book.get("best_ask"))
        ours_bid, ours_ask = _f(q.get("our_bid")), _f(q.get("our_ask"))
        hit, px, how = False, None, ""
        if ours_bid is not None and ba is not None and ba <= ours_bid:
            hit, px, how = True, ours_bid, "book_cross"
        elif ours_ask is not None and bb is not None and bb >= ours_ask:
            hit, px, how = True, ours_ask, "book_cross"
        # The book test above is a SINGLE SNAPSHOT taken once a day while the quote rests
        # continuously, so on its own it would miss any fill that did not happen to persist
        # until the poll. Fall back to the minute-resolution price path over the quote's
        # life: if price reached our quote at any point, a counterparty was transacting
        # there. Weaker evidence than a cross, hence recorded separately and never mixed in.
        if not hit:
            path = _price_path(book.get("yes_token_id"), _quote_ts(q))
            if path:
                lows = [pp for _ts, pp in path]
                if ours_bid is not None and min(lows) <= ours_bid:
                    hit, px, how = True, ours_bid, "price_path"
                elif ours_ask is not None and max(lows) >= ours_ask:
                    hit, px, how = True, ours_ask, "price_path"
        if hit:
            q["filled"] = "yes"
            q["fill_price"] = px
            q["fill_method"] = how
            mid = _f(q.get("mid"))
            # What liquidity provision paid us: distance from the mid at quote time.
            q["spread_earned"] = round(abs(mid - px), 4) if mid is not None else ""
            n += 1
    return n


def score_quotes(resolve_outcome) -> Dict[str, Any]:
    """Score filled quotes once their events resolve. The adverse-selection test.

    `resolve_outcome(quote) -> "YES" | "NO" | None` returns the realised outcome.

    Reports the fill rate, the spread we would have earned, and — the number that decides
    everything — how those fills actually resolved. A maker who earns the spread and still
    loses money is being adversely selected: filled exactly when wrong.
    """
    quotes = read_quotes()
    filled = [q for q in quotes if q.get("filled") == "yes"]
    changed = False
    for q in filled:
        if q.get("resolved"):
            continue
        actual = resolve_outcome(q)
        if actual not in ("YES", "NO"):
            continue
        cost = _f(q.get("fill_price")) if q.get("side") == "YES" else (
            1.0 - _f(q.get("fill_price")) if _f(q.get("fill_price")) is not None else None)
        if cost is None:
            continue
        q["resolved"] = "yes"
        q["outcome_actual"] = actual
        q["pnl"] = round(V.payoff(cost, q.get("side") == actual), 4)
        changed = True
    if changed:
        with open(QUOTE_LOG, "w", newline="") as fh:
            w = csv.DictWriter(fh, fieldnames=FIELDS)
            w.writeheader()
            for q in quotes:
                w.writerow({k: q.get(k, "") for k in FIELDS})

    scored = [q for q in quotes if q.get("resolved") == "yes" and _f(q.get("pnl")) is not None]
    bets = [{"ticker": q.get("ticker"), "earnings_date": q.get("earnings_date"),
             "outcome": q.get("outcome"), "executable": True, "pnl": _f(q.get("pnl"))}
            for q in scored]
    assessment = V.assess(
        bets, key=lambda b: (b["ticker"], b["earnings_date"]), require_executable=True)

    spreads = [_f(q.get("spread_earned")) for q in filled if _f(q.get("spread_earned")) is not None]
    n_q = len(quotes)
    out: Dict[str, Any] = {
        "quotes_logged": n_q,
        "filled": len(filled),
        "fill_rate": round(len(filled) / n_q, 3) if n_q else None,
        "mean_spread_earned": round(sum(spreads) / len(spreads), 4) if spreads else None,
        "resolved_fills": len(scored),
        "assessment": assessment,
    }
    # THE ADVERSE-SELECTION READ.
    if assessment["stats"].get("n", 0) >= 5 and out["mean_spread_earned"]:
        mean_pnl = assessment["stats"].get("mean")
        out["adverse_selection"] = (
            "FILLS RESOLVE BADLY — we are being picked off: the spread is earned but the "
            "positions lose. Market making does not work here on these quotes."
            if (mean_pnl is not None and mean_pnl < 0) else
            "fills resolve positively so far — not yet powered, keep collecting")
    else:
        out["adverse_selection"] = (
            f"not enough resolved fills yet ({assessment['stats'].get('n', 0)}) to say "
            f"whether we are being adversely selected")
    out["honest_note"] = (
        "Paper quotes. Fills are inferred from the market crossing our price and ignore "
        "queue position, so the real fill rate would be LOWER. No orders were placed.")
    return out


# ---------------------------------------------------------------------------
# Live wiring
# ---------------------------------------------------------------------------
def _live_book(slug: str) -> Dict[str, Any]:
    """Current book for a slug, plus the YES token id for the price-path check."""
    from tools import polymarket_client as pc
    try:
        m = pc.get_market(slug) or {}
    except Exception:
        return {}
    toks = m.get("token_ids") or []
    return {"best_bid": m.get("best_bid"), "best_ask": m.get("best_ask"),
            "yes_token_id": toks[0] if toks else None}


def _quote_ts(q: Dict[str, Any]) -> Optional[int]:
    """Unix seconds the quote was posted — the earliest moment a fill could occur."""
    from datetime import datetime as _dt
    s = (q.get("quoted_at") or "").strip()
    if not s:
        return None
    try:
        return int(_dt.fromisoformat(s).timestamp())
    except ValueError:
        return None


def _price_path(token_id: str, since_ts: Optional[int] = None) -> List[tuple]:
    """Minute-resolution YES price path, newest window first. [] if unavailable.

    Polymarket exposes no public trade prints (data-api /trades is 410, CLOB /trades needs
    auth), but /prices-history returns a ~60-second price series without a key. If that
    price ever reached our resting quote while it sat there, a counterparty was transacting
    at our price and we would plausibly have been filled.

    This is a PROXY, not a confirmed trade: the series is a price, not our queue position,
    so a real maker could still have been behind other orders at the same price. It is used
    only to decide whether "zero fills" is real or an artifact of polling once a day.
    """
    import urllib.request, json as _json
    if not token_id:
        return []
    # The endpoint rejects some interval/fidelity pairs: interval=1w with fidelity=1 is a
    # 400, which silently returned [] and made the whole price-path check a no-op the first
    # time it shipped (caught 2026-11-10 by printing the point count instead of trusting it).
    # Two valid windows, unioned: a week at hourly resolution covers the quote's life, a day
    # at minute resolution catches brief touches the hourly series would smooth away.
    out: List[tuple] = []
    for interval, fidelity in (("1w", 60), ("1d", 1)):
        url = (f"https://clob.polymarket.com/prices-history?market={token_id}"
               f"&interval={interval}&fidelity={fidelity}")
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "research-client"})
            with urllib.request.urlopen(req, timeout=25) as r:
                data = _json.loads(r.read().decode("utf8", "replace"))
        except Exception:
            continue
        for pt in (data or {}).get("history", []) or []:
            try:
                ts, px = int(pt["t"]), float(pt["p"])
            except (KeyError, TypeError, ValueError):
                continue
            # ONLY prices from after the quote was posted. The 1w window reaches back days
            # before the quote existed, and counting those as fills would book a trade that
            # happened before we were in the market -- the same look-back error this project
            # has already had to retract once. Without this filter 9 of 10 resting quotes
            # read as filled; the filter is what makes the number mean anything.
            if since_ts is not None and ts < since_ts:
                continue
            out.append((ts, px))
    return out


def _resolve(q: Dict[str, Any]) -> Optional[str]:
    """Realised outcome for a quote, reusing the earnings resolver."""
    from tools import forward_test as ft
    ed = q.get("earnings_date") or ""
    if not ed:
        return None
    try:
        from datetime import datetime as _dt
        if _dt.strptime(ed, "%Y-%m-%d") >= _dt.now():
            return None                      # not yet reported — no look-ahead
    except ValueError:
        return None
    try:
        res = ft._resolve_eps(q.get("ticker"), ed, None)
    except Exception:
        return None
    return None if res is None else ("YES" if res else "NO")


def daily(limit: int = 30) -> Dict[str, Any]:
    """One full paper-maker cycle: quote, check resting quotes for fills, score.

    Safe to call repeatedly. Places no orders, moves no money.
    """
    from tools import scanner
    out: Dict[str, Any] = {}
    try:
        scan = scanner.scan(limit=limit)
        sheet = build_sheet(scan.get("rows", []))
        out["scanned"] = scan.get("scanned")
        out["quotes_built"] = len(sheet)
        out["quotes_logged"] = log_quotes(sheet)
        # Why the other markets were not quoted — so a low quote count can be diagnosed
        # (thin universe vs filters too tight) rather than guessed at.
        out["rejected"] = dict(LAST_REJECTIONS)
        out["sheet"] = sheet
    except Exception as e:
        out["quote_error"] = str(e)

    # Check every still-open quote against the current book.
    quotes = read_quotes()
    open_q = [q for q in quotes if q.get("filled") in (None, "", "no")
              and not q.get("resolved")]
    if open_q:
        cache: Dict[str, Dict[str, Any]] = {}

        def lookup(slug):
            if slug not in cache:
                cache[slug] = _live_book(slug)
            return cache[slug]
        try:
            n = check_fills(open_q, lookup)
            if n:
                merged = {(q.get("slug"), q.get("side"), (q.get("quoted_at") or "")[:10]): q
                          for q in open_q}
                with open(QUOTE_LOG, "w", newline="") as fh:
                    w = csv.DictWriter(fh, fieldnames=FIELDS)
                    w.writeheader()
                    for q in quotes:
                        k = (q.get("slug"), q.get("side"), (q.get("quoted_at") or "")[:10])
                        w.writerow({f: merged.get(k, q).get(f, "") for f in FIELDS})
            out["new_fills"] = n
        except Exception as e:
            out["fill_error"] = str(e)

    try:
        out["score"] = score_quotes(_resolve)
    except Exception as e:
        out["score_error"] = str(e)
    return out

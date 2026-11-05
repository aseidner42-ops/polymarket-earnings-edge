"""Net-of-cost expected value of our model probability vs the Polymarket price.

Polymarket binary share pays $1 if the outcome resolves YES. Buying YES costs
the YES price q; buying NO costs (1 - q). Our model says P(YES) = p.

  gross edge (prob points) = p - q   (YES side) ;  q - p  (NO side)

We subtract trading frictions (half-spread, fee, and an uncertainty haircut for
model error / liquidity / resolution risk) before recommending anything. No edge
is flagged unless it clears `min_edge_to_flag`. Never auto-trades.
"""
from __future__ import annotations

from typing import Any, Dict, Optional

from tools.config import load_weights


def evaluate(model_prob: float, market: Optional[Dict[str, Any]],
             regime: Optional[float] = None) -> Dict[str, Any]:
    """Compare model_prob (P of YES) to the market's YES price; return net edge + call.

    regime = VIX 1y percentile [0,1]; high vol widens the uncertainty haircut."""
    f = load_weights()["frictions"]
    if market is None or market.get("yes_price") is None:
        return {
            "market_prob": None, "side": None, "gross_edge": None,
            "net_edge": None, "friction": None, "ev_per_dollar": None,
            "recommendation": "no Polymarket price available",
        }

    q = float(market["yes_price"])
    # Half-spread cost: prefer the live CLOB book spread, then Gamma, then default.
    book_spread = market.get("book_spread")
    gamma_spread = market.get("spread")
    if book_spread is not None:
        half_spread, spread_src = book_spread / 2.0, "book"
    elif gamma_spread:
        half_spread, spread_src = gamma_spread / 2.0, "gamma"
    else:
        half_spread, spread_src = f["assumed_spread"], "default"
    # Scale the uncertainty haircut by volatility regime: calm ~1x, extreme ~1.5x.
    haircut = f["uncertainty_haircut"] * (1 + 0.5 * regime) if regime is not None else f["uncertainty_haircut"]
    friction = half_spread + f["taker_fee"] + haircut

    side = "YES" if model_prob >= q else "NO"
    gross_edge = (model_prob - q) if side == "YES" else (q - model_prob)
    net_edge = gross_edge - friction

    # EV per $1 staked on the recommended side.
    if side == "YES":
        ev_per_dollar = (model_prob - q) / q if q > 0 else None
    else:
        ev_per_dollar = (q - model_prob) / (1 - q) if q < 1 else None

    # COST OF THE CONTRACT WE BUY. Buying YES costs q; buying NO costs 1-q. A NO bet on a
    # market trading at 0.80 is a 0.20 LONGSHOT PURCHASE — not a contrarian read on a
    # favorite. 61% of our resolved bets turned out to be longshot buys, mostly the fades.
    cost = q if side == "YES" else (1.0 - q)

    # LONGSHOT GATE (2026-11-05). Bürgi-Deng-Whelan (UCD, Jan 2026; 46,282 Kalshi contracts)
    # find buyers of contracts under 10c lose OVER 60% of their money, while contracts above
    # 50c earn a small, significant positive return. Our own vault agrees: by cost of the
    # contract bought, win rate runs 0.10 (under 10c) -> 0.83 (over 70c), and the payoff SD
    # runs 7.41 -> 0.46. The cheap end is both the losing zone AND the unmeasurable one, so
    # we refuse to recommend it rather than keep paying tuition there. This does not hide the
    # market: it is still scanned, priced and logged — just never recommended as a BUY.
    if cost < 0.10:
        rec = (f"NO BUY — {side} costs {cost:.2f}, a deep longshot. Contracts under 10c lose "
               f">60% on average (46k-contract Kalshi study) and their payoff variance makes "
               f"any edge here unmeasurable. Paper edge was {net_edge:+.1%}.")
    elif net_edge >= f["min_edge_to_flag"]:
        rec = f"BUY {side} @ {q:.2f} (net edge {net_edge:+.1%} after costs)"
        if cost < 0.50:
            rec += (f" | LONGSHOT: the {side} contract costs {cost:.2f}; this sits in the "
                    f"favorite-longshot-bias zone and needs a far larger sample to prove out")
    else:
        rec = f"no actionable edge (net {net_edge:+.1%} < {f['min_edge_to_flag']:.0%} threshold)"

    # Thin-market caution — prefer real book depth, else Gamma liquidity.
    depth = market.get("depth_usd")
    liq = market.get("liquidity")
    if depth is not None and depth < 2000:
        rec += f" | CAUTION: thin book (~${depth:,.0f} near top)"
    elif depth is None and liq is not None and liq < 5000:
        rec += " | CAUTION: low liquidity"

    # Executable edge: walk the real order book for the recommended side.
    executable = _executable(model_prob, side, market, f)

    return {
        "market_prob": q,
        "side": side,
        "gross_edge": gross_edge,
        "net_edge": net_edge,
        "friction": friction,
        "spread_source": spread_src,
        "ev_per_dollar": ev_per_dollar,
        "contract_cost": round(cost, 4),
        "is_longshot": bool(cost < 0.50),
        "is_deep_longshot": bool(cost < 0.10),
        "executable_edge": executable.get("edge_at_100"),
        "max_tradeable_usd": executable.get("max_usd"),
        "edge_at_max": executable.get("edge_at_max"),
        "expected_profit_usd": executable.get("expected_profit_usd"),
        "top_of_book_usd": executable.get("top_of_book_usd"),
        "fill_reason": executable.get("fill_reason"),
        "recommendation": rec + executable.get("suffix", ""),
    }


def _executable(model_prob: float, side: str, market: dict, f: dict) -> dict:
    """Walk the live book to get the realistic edge after slippage, and the max $
    you can deploy before the edge is eaten. Buying YES lifts asks; buying NO hits
    bids (cost = 1 - bid). Returns {edge_at_100, max_usd, suffix}."""
    asks = market.get("ask_levels") or []
    bids = market.get("bid_levels") or []
    levels = asks if side == "YES" else [[1 - p, s] for p, s in bids]  # NO cost = 1 - bid
    levels = [(p, s) for p, s in levels if p and s]
    if not levels:
        return {"edge_at_100": None, "max_usd": None, "edge_at_max": None,
                "expected_profit_usd": None, "top_of_book_usd": 0.0,
                "fill_reason": "no order book on the tradable side", "suffix": ""}
    win_prob = model_prob if side == "YES" else (1 - model_prob)

    def avg_fill(stake_usd: float):
        spent = shares = 0.0
        for price, size in levels:
            lvl_usd = price * size
            take = min(lvl_usd, stake_usd - spent)
            if take <= 0:
                break
            shares += take / price
            spent += take
        return (spent / shares) if shares > 0 else None, spent

    fee = f["taker_fee"] + f["uncertainty_haircut"]
    fill100, _ = avg_fill(100.0)
    edge_at_100 = (win_prob - fill100 - fee) if fill100 is not None else None

    # Max stake while net edge stays > min_edge_to_flag. The ladder starts at $25: these
    # books are small (a live Nov-2026 scan showed $100-$300 near the top), and starting at
    # $50 reported max_usd=0 for markets that genuinely could absorb $25-49 — understating
    # fillability is as dishonest as overstating it.
    max_usd = 0.0
    for stake in (25, 50, 100, 250, 500, 1000, 2500, 5000):
        fp, got = avg_fill(float(stake))
        if fp is None or got < stake * 0.95:
            break
        if (win_prob - fp - fee) >= f["min_edge_to_flag"]:
            max_usd = float(stake)
        else:
            break

    # EXPECTED DOLLAR PROFIT at the size actually deployable (2026-11-03). An edge
    # FRACTION is the wrong thing to rank on: a 40% edge on a book that fills $50 is worth
    # $20, a 3% edge that fills $5,000 is worth $150. Ranking by fraction is exactly what
    # steered the scanner into unfillable 3-cent books whose paper P&L then faked an edge.
    # Priced at the AVERAGE FILL for the whole stake, so slippage is paid, not assumed away.
    # Why a market is unfillable, so "paper_only" is explained rather than asserted.
    top_usd = sum(p * sz for p, sz in levels[:3])
    edge_at_max = expected_profit_usd = None
    if max_usd > 0:
        fp_max, _got = avg_fill(max_usd)
        if fp_max is not None:
            edge_at_max = win_prob - fp_max - fee
            expected_profit_usd = max_usd * edge_at_max

    suffix = ""
    if edge_at_100 is not None:
        suffix = f" | executable edge {edge_at_100:+.1%} on $100; max ~${max_usd:,.0f} before edge gone"
        if expected_profit_usd is not None:
            suffix += f" (~${expected_profit_usd:,.0f} expected at that size)"
    if max_usd <= 0:
        # Distinguish the two very different reasons, because they mean opposite things:
        # a real edge the book is too thin to hold, vs no edge at the price you'd pay.
        best_px = levels[0][0]
        if (win_prob - best_px - fee) < 0:
            reason = (f"no edge at the touch: pays {best_px:.2f} for a {win_prob:.2f} "
                      f"chance (mid flatters it)")
        elif top_usd < 25:
            reason = f"real edge but no size: only ~${top_usd:,.0f} on the top 3 levels"
        else:
            reason = f"edge eaten by slippage before $25 (~${top_usd:,.0f} near top)"
    else:
        reason = ""
    return {"edge_at_100": edge_at_100, "max_usd": max_usd, "edge_at_max": edge_at_max,
            "expected_profit_usd": expected_profit_usd, "top_of_book_usd": round(top_usd, 2),
            "fill_reason": reason, "suffix": suffix}

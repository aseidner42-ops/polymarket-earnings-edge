---
date: 2026-11-05
tags: [polymarket, strategy, pivot, decision]
---

# Pivot options — what this could become

Context: confirming a taker edge needs ~2,400 favorite-zone bets (years, *assuming* an edge exists). Before choosing, I tested the most intellectually appealing pivot. **It failed.** That result narrows the list usefully.

---

## First, a pivot I can rule OUT today

**"Trade the stock instead of the binary."** Kelly 2016 is a paper about *returns* — pre-earnings run-up and post-earnings reversal. We converted a return-predictability finding into a binary beat/miss bet, which threw away the magnitude and bought a base-rate ceiling. Going back to the stock would give deep liquidity, real size, and small per-bet variance. Obvious move.

Tested on 480 cached backtest events with realized post-earnings returns:

| | corr with post-earnings return |
|---|---|
| **our model's beat probability** | **+0.0019** |
| actual beat (did it happen) | +0.0488 |
| surprise magnitude (%) | +0.0786 |
| PUMP | +0.0324 |
| best of 8 signals (filing_risk) | +0.0850 |

PUMP quintiles, Kelly predicts the top should **reverse**: Q1 −0.0008 → Q5 +0.0022, **top-minus-bottom +0.0031, t = +0.33.** Nothing, and the wrong sign.

**Read:** our model predicts something that barely moves the stock. Beating consensus is already priced; what moves price is magnitude and guidance. **None of our eight signals predicts returns.**

*Honest caveat:* this is a 2-day return measure on 480 events from free data, not Kelly's methodology (8 quarters of [0,+1] windows, portfolio sorts, 1983–2014, driven by individual-investor order flow). We may have failed to implement it rather than disproven it. But **as built, the stock route has no signal to trade.**

---

## Option A — Become a maker. Quote, don't lift. ⭐ strongest money path

The one number that matters, from live earnings books this session (n=10):

| | |
|---|---|
| mean book spread | **8.0¢** (median 5.5¢, max 23¢) |
| half-spread a maker captures / a taker pays | **4.0¢** |
| as a return on a 0.50 contract | **+8.0%** (median +5.5%) |
| documented *taker-side* maker edge, ≥50¢ | +2.6% |
| **ratio** | **3.1×** |

**The spread is three times larger than the edge we've spent five months failing to prove.** We pay it on every trade. Bürgi-Deng-Whelan: the positive return belongs to **Makers**; the longshot bias is worse on the **Taker** side. We are structurally on the wrong side of every single trade we make.

- **What we'd keep:** everything. Calibration tells us where fair value sits; the spread pays us for waiting there. This is the one framing where our *only validated asset* converts directly into money.
- **Variance:** collapses. Buying at the bid instead of the ask on a 0.50 contract is ~8% before any forecasting skill.
- **What's new and hard:** adverse selection (you get filled exactly when you're wrong), inventory risk, quote management, being online at the right times, and genuinely small size.
- **Rules:** never auto-trade stays. This becomes a *quote sheet* — "post NO at 0.62 on X, size $Y" — that a human places.
- **Provable in:** far fewer bets, because the edge per trade is ~3× bigger and the variance is lower.

**Honest risk:** market making is a different craft, and the one Polymarket empirical study found model spreads 1.8–5.4× observed — the theory doesn't fit these books well. You would be learning a new discipline, not extending this one.

## Option B — Cross-venue / cross-contract arbitrage

Same event, two prices: Polymarket vs Kalshi vs options-implied. The edge is the *gap between venues*, not a forecast — **no forecasting skill required**, so none of our 2,400-bet problem applies.

- **Assets we already have:** `kalshi_client.py`, `market_data.py` options chain, the book walker.
- **Provable in:** days. Either the gaps exist net of fees or they don't — measurable immediately.
- **Risk:** probably small and rare, needs capital on both venues, and resolution-criteria mismatches between venues are exactly the trap we just hit with GAAP.

## Option C — Ship the forecast, not the bet

Our **one asset that already passes its own test**: model Brier **0.1544 vs market 0.1674**, out of sample, surviving every tightening. That is a calibrated pre-earnings probability sheet that beats the crowd.

- **Provable at n≈300 today** — not 2,400. It's already proven.
- **No liquidity constraint, no fills, no spread, no variance problem.**
- It stops being a trading project. Whether that's a pivot or an exit depends on what you want from it.

## Option D — Narrow hard to the structural strike-gap

Mechanical mispricing (strike frozen at creation vs consensus drifting) — doesn't require out-forecasting anyone. Pre-registered, re-baselined. But GAAP contracts removed a chunk, n is tiny (4 scored), and it still pays the taker spread. **Keep running it; it isn't a pivot on its own.**

## Option E — Be the measurement layer

The genuinely differentiated thing we built isn't a model — it's the falsification harness: executable-fill gating, pre-registration that blocks backward scoring, tail-robustness, power requirements, trial counting for DSR. Nearly every open-source prediction-market bot has **no validation at all**. A reusable "point this at your strategy and it will tell you honestly whether you have anything" is a real gap in the field.

---

## My recommendation

**A, with B as the fast falsifiable probe.**

Run **B first** — it's days, not months, and it either exists or it doesn't. While that runs, build **A's quote sheet**: use the model we already trust to post a fair-value bid/ask, and start measuring what *would* have filled. That's a forward test of market making that costs nothing and needs no capital — log quotes, check against the tape, see if we'd have been adversely selected.

**C in parallel regardless** — it's already true and costs nothing to state. Don't let the unproven money edge obscure the proven forecasting one.

The thing to stop: paying 4¢ of spread to take the other side of a 2.6% edge, 2,400 times.

Related: [[2026-11-05 Power, Cost and the Favorite Zone]], [[2026-11-05 Prior Art — What To Steal]].

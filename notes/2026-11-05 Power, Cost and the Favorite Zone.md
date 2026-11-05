---
date: 2026-11-05
tags: [polymarket, power-analysis, favorite-longshot, methodology]
---

# Power, contract cost, and the favorite zone

Acting on [[2026-11-05 Prior Art — What To Steal]]. Three changes, all of which make the system **harder** on itself.

## The correction that drove everything

I had been thinking of "contract price" as the market's YES price. Wrong. **What matters is the price of the contract you BUY:** YES costs `q`, NO costs `1 − q`.

So a **NO bet on a market trading at 0.80 costs 0.20 — that is a longshot purchase**, not a contrarian read on a favorite. Re-cutting the vault by that number:

| cost of contract bought | n | mean | **SD** | win rate | bets needed @2.6% |
|---|---|---|---|---|---|
| < 0.10 | 40 | +1.267 | **7.41** | 0.10 | 311,675 |
| 0.10–0.30 | 87 | −0.180 | 2.11 | 0.15 | 25,223 |
| 0.30–0.50 | 66 | +0.103 | 1.28 | 0.44 | 9,314 |
| 0.50–0.70 | 58 | +0.103 | 0.79 | 0.67 | 3,522 |
| 0.70+ | 64 | −0.030 | **0.46** | 0.83 | 1,189 |

**61% of our resolved bets (193/315) are longshot purchases — 147 of them are the fade trades.** That single reframing explains the fades losing far better than any signal story: we weren't fading favorites, we were *buying longshots*, the zone that loses >60% across 46,282 Kalshi contracts.

## 1. Power — our 30-bet bar was never capable

The SD column is the real finding. Variance decides whether an edge is **detectable at all**:

- favorites (cost ≥ 0.50): SD **0.65** → ~**2,400** bets to resolve a 2.6% edge
- longshots (cost < 0.50): SD **2.20** → ~**27,400** bets
- whole vault as it stands: SD **1.746** → ~**17,318** bets

**We set the bar at 30.** At that n a 2.6% edge and zero are indistinguishable — which is exactly how a tail fluke cleared it on 2026-10-30.

Shipped: `required_n(sd, effect)`; every stats block now carries `sd`, `n_needed_at_2.6pct`, `powered`; and `_verdict()` has a new **UNDERPOWERED** state that fires when a CI excludes zero on a sample too small to mean it. Live headline now reads:

> UNDERPOWERED — 303 bets, but SD 1.746 means ~17,318 are needed to resolve a realistic (+2.6%/contract) edge. Any 'significant' result at this n is more likely a tail fluke.

That is a worse-sounding and more accurate statement than anything the system has said before.

## 2. Stop buying the losing zone

`ev.evaluate` now computes `contract_cost` and **refuses to recommend any contract costing under 10¢** — the >60%-average-loss bucket — regardless of paper edge:

> NO BUY — YES costs 0.04, a deep longshot. Contracts under 10c lose >60% on average (46k-contract Kalshi study) and their payoff variance makes any edge here unmeasurable.

Between 10¢ and 50¢ a BUY still flags but carries an explicit LONGSHOT warning. Scanner ranks favorites above longshots, never calls a deep longshot `trustworthy`, and the app's scan table has a new **COST** column (green ≥0.50 / amber <0.50 / red <0.10) with the reasoning on hover. Nothing is hidden — these markets are still scanned, priced and logged. They just stop being recommendations.

## 3. Pre-registered `favorite_zone_v1`

Registered **today, scored only from today**, with its provenance written down: it comes from the **external Kalshi paper, not from searching our own vault** — so it is not a multiple-comparisons artifact of our data. Two caveats kept in the file:

1. The literature's +2.6% belongs to **Makers**. We are a **Taker**, the side the same paper shows does worse — so our expected effect is *smaller* and the required n *larger*.
2. Our own favorites subset is +0.033/$1 on n=117, CI [−0.087, +0.148] — consistent with the hypothesis and equally consistent with zero. That is **prior data and does not count toward this test.**

`strike_gap_v1` had its success criterion upgraded to require power, and now honestly reports 4 scored bets against a ~19,045 requirement.

## 4. Trial counter (for the Deflated Sharpe Ratio)

`hypotheses.json` now has a `trial_log`. DSR and PBO both need the **number of trials** to judge whether the best result clears the expected maximum under zero skill. We recorded *that* searches happened but never *how many* — the exact input the correction needs. Seeded with the 2026-11-03 sweep (20 subgroup tests, 4 "profitable", ~1 false positive expected by chance) and today's 6 cost-bucket cuts. **Running total: 26.**

## What's still true, and what it costs

Model Brier **0.1544 vs market 0.1674** on the same bets — the forecasting advantage survives every tightening so far. The money does not: **−0.032/$1**, median −1.00, ex-top-5 −0.196.

And the honest planning number is brutal: even in the favorite zone, confirming an edge needs **~2,400 resolved bets**. At our current rate that is years, and it assumes an edge is there. The alternatives are to reduce variance further, or to change what we are — from a taker lifting thin asks into something that earns the spread instead of paying it.

Related: [[2026-11-03 Edge Confirmed — Retracted]], [[2026-11-03 Road to a Real Edge]], [[2026-11-03 Rank on Fills Not Fractions]].

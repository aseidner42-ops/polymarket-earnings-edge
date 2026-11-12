# Case study: retracting my own result

**Project:** Polymarket Earnings Edge — a research system for pricing earnings contracts
**Period:** June–November 2026 · ~9,000 lines of Python · 1,188 resolved forward predictions
**Outcome:** the system's headline finding was false. I found it, retracted it, and rebuilt
the validation layer so the same class of error is caught automatically.

---

## The result

On **30 October 2026** the automated forward test fired its success condition for the first
time:

> EDGE CONFIRMED — +0.394/$1 over 351 bets, 95% CI [0.045, 0.744], beats_crowd True

This was the bar I had pre-registered months earlier: at least 30 resolved bets, with a 95%
confidence interval excluding zero. The system had cleared it. Every number was computed
correctly.

It was wrong.

---

## The audit

Four days later I went through the resolved bets line by line, expecting to confirm the
result. The first thing I checked was tail concentration — how much of the profit came from
how few bets.

**The top 5 of 382 bets carried 89% of all P&L.** The top 10 carried 121%, meaning
everything else was collectively negative. The median bet lost its entire stake.

That is not an edge. That is a lottery ticket that happened to hit. Pulling the thread found
three independent defects, each sufficient on its own:

**1 — The sample was not independent.**
A single earnings print was logged under multiple outcome rows. Starbucks' 29 October print
appeared twice, each copy carrying the same **+28.39** win. The duplication inflated the
mean, but the worse damage was to the confidence interval: duplicate rows fake sample size,
and a fake *n* is what makes a CI look tight. The significance was partly manufactured by a
data-modelling bug.

**2 — Disputed resolutions were scored as clean wins.**
Eight rows flagged `DISPUTED-RESOLUTION` averaged **+3.11/$1** and were counted as settled.

**3 — The fills were fiction.**
The phantom-price filter admitted "fills" at prices of **0.030 and 0.034** — contracts
paying 32:1 — in order books holding a few dollars. The P&L assumed a $1 fill at the
midpoint of a market nobody could trade.

**Corrected: +0.337 → +0.003 per $1.** Probability the true mean is ≤ 0: **51%.** Dead
breakeven, and worse than blindly backing the favourite.

I checked the conclusion wasn't an artifact of my own choice of threshold: at price floors
of 0.05, 0.10 and 0.15 the mean was +0.011, +0.007 and −0.026. Breakeven everywhere.

---

## The deeper problem

Fixing the arithmetic was not enough, because the test itself had never been capable.

My success criterion was "30 bets with a CI above zero." I had never asked what effect size
30 bets could actually resolve. Working it backwards from the observed payoff variance:

| measurement | SD | bets needed for a 95% CI above zero |
|---|---|---|
| as I was measuring it | 1.75 | **~17,300** |
| restricted to low-variance favourites | 0.65 | **~2,400** |
| **my bar** | | **30** |

At 30 bets a real 2.6% edge and zero are indistinguishable. The criterion could only ever
have been passed by luck — and it was. A success threshold that does not name the effect
size it can detect is decoration.

The variance itself turned out to be diagnostic. Re-cutting the data by **the price of the
contract actually bought** — a "NO" bet on a market at 0.80 costs 0.20, so it is a longshot
purchase, not a contrarian view — showed **61% of my bets were longshot buys**:

| cost of contract bought | n | win rate | payoff SD |
|---|---|---|---|
| under 10¢ | 40 | 0.10 | 7.41 |
| 50–70¢ | 58 | 0.67 | 0.79 |
| over 70¢ | 64 | 0.83 | 0.46 |

An independent study of 46,282 Kalshi contracts (Bürgi, Deng & Whelan, 2026) finds buyers
of sub-10¢ contracts lose **over 60%** of their money. I had been systematically buying the
single worst-documented bucket in the literature, and calling the occasional 32:1 hit an
edge.

---

## What I changed

The fixes are rules in code, not intentions:

- **Independence** — one payoff per underlying event, deduplicated automatically
- **Fillability** — the order book is frozen at bet time; a bet with no executable size is
  not evidence and does not count
- **Price realism** — P&L is computed at the price actually payable (the ask for a buy),
  never the midpoint. Mid-pricing had been gifting the model half the spread on every bet
- **Tail robustness** — a result must survive deleting its five best wins
- **Statistical power** — the verdict now reports `UNDERPOWERED` when the sample cannot
  resolve a realistic effect, *before* it reports significance
- **Pre-registration** — a rule can only be scored on data that postdates its registration,
  plus a trial counter, because twenty subgroup searches yield one "significant" result by
  chance (I had run twenty)

All of it sits in a strategy-agnostic module with **10 regression tests**, each named for
the failure it prevents. One test feeds in the exact 2026-10-30 shape — 95 losses and 5
giant wins — and asserts the verdict is *not* "edge confirmed." Another checks the harness
can still confirm a genuine edge, so it is not unfalsifiable in the other direction.

**The cost was real.** 272 resolved bets became unusable for proving profitability, because
none had recorded whether they could be filled. The evidence clock went back to 0 of 30.

---

## What survived

The model's forecasts score lower than the market's, but I had not tested whether that is
more than noise, and when I did it is not:

**Model Brier 0.1585 vs market price 0.1687**, n = 326 resolved bets, out of sample. The
gap is +0.0102 with a block-bootstrap (by ticker) 95% CI of **[−0.014, +0.035]**; in about 21%
of resamples the model is no better than the market. It has also moved: 0.0135 at n = 298,
0.0096 at n = 322, 0.0102 at n = 326. So this is a lead, not a measured advantage. Both the
model and the market beat a base-rate-only Brier of 0.1721. Reproduce with
`python -m tools.brier_gap`.

It still doesn't make money — realised P&L **−0.017/$1**. The gap between those two facts is
the most useful thing the project taught me: **even a real calibration advantage over a market is not
the same as being able to profit from it.** The difference is spread, liquidity and
fillability. On live books the mean bid-ask spread is 8¢ while the documented edge is 2.6%.
The spread is 3.1× the edge, and as a taker I paid it on every single trade.

That reframing is now the live experiment: posting quotes instead of lifting them, on
paper, measuring whether fills arrive precisely when I'm wrong. First measurement: a 40%
fill rate earning 6.79¢ of spread — a number that existed only after finding the fill
detector had been reporting zero because it could not see the thing it measured.

---

## Postscript: the same error, in the tool built to catch it

Three days after the retraction I rebuilt the replication test for the academic paper the
project is based on. Its verdict function checked whether each leg of the hypothesis
pointed the predicted **direction** — and on real data it reported *"both legs point the
predicted way"* off a reversal of **−0.16% with t = −0.23**. Noise wearing a minus sign.

I caught it, required significance instead of sign, and left a comment in the code saying
what happened. The corrected result is more interesting than either the original null or
the false positive: over 1,796 events the pre-announcement **buying pressure replicates at
t = 2.58**, while the **reversal — the half that would actually pay — does not appear at
all** in liquid large caps.

The point of including this: the failure mode is not a one-off bug. It is a standing
tendency to accept a result that points the right way, and it got past me while I was
actively building a tool whose only job was to test for it. That is why the checks live in
code with regression tests, rather than in my intentions.

## Why I'm presenting the failure

Because the alternative was presenting the false positive, and I'd rather be the person who
caught it.

The eleven days when this system reported a confirmed edge are the most instructive part of
the project. Nothing external forced the audit; the number looked good, the methodology
looked sound, and it would have been easy to keep. What found it was the habit of checking
whether a result depends on a handful of observations — and being willing to act when the
answer is yes.

Everything above is in the repository with dated research notes written as it happened,
including the retraction.

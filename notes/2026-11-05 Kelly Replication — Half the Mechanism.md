---
date: 2026-11-05
tags: [kelly, replication, research, extret]
---

# Kelly (2016) replication: half the mechanism is real

This project is built on Kelly's over-extrapolation finding, and an earlier pass failed to
reproduce it (+0.03 correlation, quintile spread +0.003, t = 0.33). I said at the time that
might be our implementation rather than the paper. **It was our implementation.**

Rebuilt to the paper's specification and retested: **1,796 earnings events across 47
tickers**, ~12 years of announcement history, all returns market-adjusted against SPY.

## What the earlier pass got wrong

1. **Announcement timing.** A print released after the close reacts the *next* trading day.
   Treating the calendar date as day 0 for an after-close announcement puts the entire
   reaction outside the measurement window. yfinance timestamps carry the hour; after-close
   events are now shifted one trading day.
2. **No market adjustment.** Raw returns over a 2014–2026 bull market are mostly beta.
3. **Wrong variable.** It tested PUMP, a cousin of ExtRet, not ExtRet — the recency-weighted
   average of the last 8 announcement-window [0,+1] returns.
4. **Thin event history.** Needed 8 genuine prior announcements per event; now has them.

## The two legs

The over-extrapolation story requires **both**: investors bid the stock up before the
print (H1), and it reverses after (H2). H1 alone is momentum. H2 alone is mean reversion.

### H1 — pre-announcement run-up: **REPLICATES**

Abnormal return over [−5, −1], by ExtRet quintile:

| Q1 (low ExtRet) | Q2 | Q3 | Q4 | Q5 (high) |
|---|---|---|---|---|
| −0.13% | +0.29% | +0.54% | +0.65% | **+0.75%** |

**Top − bottom = +0.88%, t = 2.58**, top-quintile CI95 [+0.28%, +1.27%] — excludes zero,
with standard errors clustered by ticker. Monotonic across all five buckets.

Firms whose recent earnings announcements went well **are** bid up into the next print.
That is Kelly's buying-pressure leg, and it is present in our universe.

### H2 — post-announcement reversal: **DOES NOT replicate**

| window | top − bottom | t |
|---|---|---|
| [+2, +21] | −0.16% | −0.23 |
| [+2, +42] | **+0.35%** (wrong sign) | +0.35 |
| [+1, +5] | −0.78% | −1.84 |
| announcement [0,+1] | −0.94% | −1.18 |

Directionally right in three of four windows, significant in none. The short window and the
announcement reaction itself lean the predicted way — high-ExtRet names do react slightly
worse — but it does not clear the bar.

## Verdict: PARTIAL

**The run-up is real; the reversal — the tradeable half — is not detectable in our
universe.** Investors over-extrapolate exactly as described. What we cannot find is the
part where that mispricing gets paid back to someone.

Entirely consistent with the paper: Kelly's effect is strongest in small, retail-held names
with individual-investor order flow. Our universe is 47 liquid large caps — the most
efficiently priced names on the market, and precisely where a retail-driven anomaly should
be weakest. **A null here is evidence about our tradable universe, not a refutation.**

## A bug I introduced and caught

My first `analyse()` checked only the **sign** of each leg. Run against real data it
cheerfully reported *"both legs point the predicted way"* — off a reversal spread of −0.16%
with **t = −0.23**. Noise wearing a minus sign.

That is the October failure reproduced *inside the tool built to test for it*. The function
now requires significance, not direction, and the comment in the code says why. Worth
recording: the instinct to accept a favourable sign is strong enough that it got past me
while I was actively writing a replication harness.

## What this changes

- `tools/kelly_replication.py` is reproducible — re-run it and it redraws from live data.
- The honest claim shifts from *"we couldn't reproduce Kelly"* to **"we reproduce the
  buying-pressure leg at t = 2.58 and cannot find the reversal in liquid large caps."**
  Stronger, more specific, and more useful.
- It also explains why PUMP never predicted returns: PUMP is downstream of the leg that
  works (run-up), and the leg that would pay (reversal) isn't there to capture.
- If this were ever revived as a strategy it would need small-cap, retail-heavy names —
  where our free data is worst. Documented, not attempted.

Related: [[2026-11-05 Pivot Options]], [[2026-11-05 Prior Art — What To Steal]].

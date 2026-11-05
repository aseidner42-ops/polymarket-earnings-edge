---
date: 2026-11-03
tags: [polymarket, forward-test, methodology, pre-registration]
---

# Road to a real edge — and how we stop faking one

Follow-on to [[2026-11-03 Edge Confirmed — Retracted]].

## Part 1 — Why the fake edge happened, and what now blocks it

The retraction fixed *arithmetic* (duplicates, disputed rows, phantom prices). That wasn't the root cause. Three deeper holes stayed open:

### Hole 1 — we never recorded whether a bet was executable
Every P&L number assumed a **$1 fill at the mid price**. Nothing in the vault said whether a book with real size existed. That's how 3¢ "fills" paying 32:1 got counted as money.

Worse, it made the pre-registered `strike_gap_v1` hypothesis **unscorable**: its entry gate requires `min_executable_edge_after_friction >= 0.05` and a live two-sided book, and not one of its 41 resolved rows recorded the book. Those 41 bets showed **+0.542/$1, CI [+0.09, +1.05], robust to the top 5** — which looks like the success criterion being met. It is not, and it must not be claimed: *an unverifiable gate is a failed test, not a passed one.*

**Fixed:** `edge_log` now freezes `yes_bid, yes_ask, book_spread, depth_usd, max_usd, executable_edge` at log time. Only bets with ≥$25 of real size count.

### Hole 2 — we scored fills at the mid
Buying YES means lifting the **ask**; buying NO means paying `1 − bid`. Scoring at the mid silently handed the model half the spread as free alpha on *every single bet*. On a 3¢ book that's a bigger effect than any edge we were claiming.

**Fixed:** `_fill_price()` — P&L at the price actually payable.

### Hole 3 — multiple comparisons (the real edge factory)
Today I sliced 272 resolved bets ~20 ways looking for where we beat the market. Four slices came back "profitable." **At a 5% level you expect about one false positive per 20 tests by chance alone.** This is exactly how the 10-30 confirmation was born, and it would happen again every time I go looking.

**Fixed:** `forward_test.score_hypothesis(id)` is now the only path to a verdict, and it refuses to score anything that isn't pre-registered in `output/hypotheses.json`. It counts **only bets logged after `scoring_starts_at`** — no backward scoring, ever. Rules found by searching are filed under `observed_not_registered` with their caution noted, carrying zero weight until they survive on data that did not exist when they were proposed.

### The scoring rules now frozen in `hypotheses.json`
1. Scored only via `score_hypothesis(id)`; unregistered rule → refused.
2. Only `logged_at >= scoring_starts_at`. No backward scoring.
3. Only bets with a verified executable book (≥$25).
4. P&L at fill price, never mid.
5. Confirm needs ≥30 such bets **AND** bootstrap CI-low > 0 **AND** positive after removing the top 5 wins.
6. The success criterion is the one written at registration. Finding a better one later voids the test.

## Part 2 — The honest cost

**The clock restarts at 0/30.** Live verdict is now:

> NOT MEASURABLE YET — 0/30 bets with a verified executable book (272 legacy rows have no book snapshot). Paper P&L 0.011/$1 is NOT evidence of an edge.

272 resolved bets just became unusable for *proving profitability*. That is the correct price. They remain fully useful for calibration, which does not depend on fills.

## Part 3 — What actually gets us to a confirmed edge

**The asset we already have is real:** on the newer half of the vault, model Brier **0.1532 vs market 0.1891** — we predict earnings outcomes better than the crowd, out of sample, on data the model never saw. That is a genuine, measured, honestly-obtained advantage.

**What it is not:** money. Betting that advantage is breakeven (+0.003/$1) and *loses to blindly backing the favorite* (+0.019). The gap between "better forecast" and "profit" is entirely **friction and fillability** — we are most right exactly where the book is thinnest and widest.

So the path is not "predict better." It's **convert the forecast advantage into fills**:

1. **Measure real money (done today).** Executable track + fill prices. Without this, a confirmation means nothing — as we just learned.
2. **Hunt liquidity, not disagreement.** Current selection ranks by `net_edge`, which systematically steers us into unfillable 3¢ books. Rank instead by *expected profit after walking the actual book*, which is already computed (`executable_edge`, `max_tradeable_usd`) but is not what drives selection.
3. **Let `strike_gap_v1` run clean.** It targets a *mechanical* mispricing (frozen strike vs drifting consensus), not an attempt to out-forecast analysts. It is the pre-registered one, and the only subgroup whose in-sample result survived a tail test. Re-baselined to 0/30 on verified books — if it is real, it will come back.
4. **Volume.** The calendar seeder already logs ~20 consensus-covered names/day. More *fillable* markets scanned = faster to 30 real bets.
5. **Keep autofit on calibration.** It improves the forecast, which is the raw material. It cannot create an edge by itself, and shouldn't be asked to.

## The standing test
Before any future profit claim, three questions:
- Could these bets have actually been **filled**? (executable track)
- Was the rule written down **before** these bets existed? (`score_hypothesis`)
- Does it survive **removing its five best wins**? (tail leg)

The 10-30 confirmation failed all three. See [[honesty-mandate]].

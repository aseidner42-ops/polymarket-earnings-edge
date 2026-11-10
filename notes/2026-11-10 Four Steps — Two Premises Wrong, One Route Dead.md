# 2026-11-10 — Four steps, two premises wrong, one route dead

Worked steps 1-4 in order. Two of my own premises turned out to be wrong, one route
closed outright, and the single most useful result came from a bug in a tool I had just
built to measure something else. Step 5 was a constraint and was honoured.

---

## 1 — Book-snapshot coverage · PREMISE WRONG, real bug found underneath

I claimed "66 of 124 rows lost depth_usd, 119 lost max_usd." Wrong denominator.

| source | n | market_prob | depth | max_usd |
|---|---|---|---|---|
| `cal-fwd` | 65 | 0 | 0 | 0 |
| `strikegap-fwd` | 16 | 16 | 16 | 16 |
| `scan` | 43 | 42 | 42 | 20 |

65 of the 66 were earnings-calendar rows with **no Polymarket market at all** — nothing
to snapshot. And of the 43 scan rows, the 23 missing `max_usd` all had an **empty side**:
they were GAAP refusals or markets scanned after settling. **Every sided bet already had
100% book coverage.** Nothing to fix.

The real bug was downstream. Six scorers did:

```python
side = row["side"] or ("YES" if model_prob >= q else "NO")
```

which invents the side the system *explicitly declined to take* and books a P&L for it.
Three resolved rows reading "market resolved / no live order book — nothing tradeable"
were counted as real positions.

Audited first: of 679 sideless resolved rows, 628 had no market, 39 no live book, 12
GAAP. **Not one was a real bet.** Added `validation.bet_side()`, patched all six sites,
added `test_refusal_is_not_a_bet` (11 groups pass).

**Effect: P&L −0.029 → −0.033/$1.** The three invented bets were winners. Removing them
made the number worse, which is how you know the fix was right.

Also split the two sample sizes, which this surfaced: `bets=322` (Brier) vs `pnl_n=319`
(money). One n for both is how three fake bets hid.

## 2 — Favorite-zone throughput · PREMISE WRONG, hard ceiling found

Shipped two free wins: a **price pre-filter** (skip candidates outside [0.05, 0.95] —
23 of 43 slots had gone to settled markets) and a **GAAP slug pre-filter** (Polymarket
encodes basis in its own slug; 8/8 agreement with the parsed resolution text; skips only
on an explicit `gaap`, never on silence). Added a `slug` column so bets are traceable at
all — its absence is what made the GAAP/slug question untestable earlier the same day.

**Tested and rejected:** widening search terms (Q4/Q1, "nongaap eps", "adjusted EPS", 6
more). Yield +3 candidates, all the same NVDA gross-margin market, an outcome we don't
model.

**The finding that matters:** the scan limit was never the constraint. Live right now
there are **14 tradeable earnings candidates against a daily limit of 40**. Measured
accumulation: **17 sided bets/week**, peaking at 44 in earnings season, 4-5 between.
At ~1/3 landing in the favorite zone, the 2,400 bets that zone needs is **~9 years**.

Favorite-zone throughput cannot be fixed by scanning harder. The ceiling is how many
earnings contracts Polymarket lists, and we are already at it.

## 3 — Maker quotes · THE REAL RESULT

`maker_fill_v1` has a kill branch: *60 quotes over 3 weeks with ZERO fills kills the
maker pivot.* The log read **0 fills on 10 quotes**. That looked like the branch coming
true. It was the instrument.

Detection was a **once-daily book snapshot** counting a fill only when the opposite touch
had *crossed* our price and the cross still persisted at the poll. On the 32-43¢ spreads
we quote, that needs the ask to collapse across the whole spread. And a real seller
hitting our bid would **take** it, leaving no visible ask at all — the test could not see
the thing it was built to measure.

Added a price-path fallback from `/prices-history`. **Made two mistakes doing it:**

1. Queried `interval=1w&fidelity=1` → **HTTP 400**, returned `[]` silently, the whole
   fallback was a no-op. Caught only by printing the point count instead of trusting it.
2. Then searched the full week — including prices from **before the quote existed** —
   and reported **9 of 10 filled**. A fill booked before we were in the market.
   Restricting to post-quote prices gives **4 of 10**.

**Result: fill rate 0.00 → 0.40, mean spread earned 6.79¢** against a documented edge of
2.6%. The maker pivot is alive. The zero-fill branch is no longer operative; the
**adverse-selection** branch is, unchanged and unamended, at 1 resolved fill of 30.

Criteria were **not** edited. Logged under a new `instrument_changes` section so a
measurement fix can never be mistaken for a result. +3 trials (35 total) — including
that the adopted proxy reports *fewer* fills than the one it replaced.

Honest limit: the price path is a proxy, not a trade print (no public trades endpoint —
data-api 410, CLOB 401). A price touching our quote means someone transacted there, not
that we held queue priority. Real fills would be **lower** than 40%, never higher.

## 4 — Cross-venue arb · ROUTE CLOSED

Kalshi's public API needs no key. Walked the full series catalogue, then **all 25,000
open markets**, matching on EPS / earnings per share / quarterly revenue.

**Zero company EPS-beat markets.** The 265 "earnings" series are almost all
`KXEARNINGSMENTION*` — markets on what a company will *say* on its call, an NLP problem
on transcripts, not an EPS outcome. The 63 apparent non-mention hits were string
collisions: `eps` inside **Eps**tein, De**eps**eek, P**eps**i. Of 25,000 open markets the
one match was a tennis market whose random ID ends in `EPS`.

**There is no counterpart instrument, so there is no gap to arbitrage.** The route isn't
hard, it's empty. Cost of finding out: ~30 unauthenticated API calls.

## 5 — Don't touch the model · HONOURED

Verified by diff: no `scoring.py`, `signals.py`, `weights.yaml`, `autofit.py`, `smg.py`
or `fitted_model.json` change across all four steps.

---

## Where this leaves the project

| | |
|---|---|
| Forecasting | Brier **0.1581** vs market **0.1677**, n=322 — supported, still narrowing |
| Money | **−0.033/$1**, CI [−0.214, +0.165], n=319. ~16,981 bets needed |
| Favorite zone | ~9 years at the measured listing rate — **not a viable path** |
| Cross-venue arb | **Closed.** No instrument exists |
| **Maker** | **fill rate 0.40, spread earned 6.79¢** — the only live route with a number |

Three of five routes are now closed or capped by arithmetic. The maker experiment is the
one place where a real measurement appeared today, and it appeared only because the tool
measuring it was broken in a way that flattered the opposite conclusion.

The recurring lesson, third time now: **the bug is usually in the instrument, and it
usually points the way I expected.**

---
date: 2026-11-05
tags: [polymarket, build, maker, validation, forecast]
---

# Built: C + E, with A running in parallel

Three things shipped. One is a product, one is infrastructure, one is an experiment.

---

## E — The falsification harness, extracted → `tools/validation.py`

The validation logic was tangled inside `forward_test.py` next to earnings-specific resolution code. Pulled out into a module that knows nothing about earnings, Polymarket, or any signal: it takes realised payoffs and returns a verdict you can defend.

Its docstring opens with *why* it exists — the 2026-10-30 false positive — and each gate is documented as the specific failure it prevents:

| gate | the failure it catches |
|---|---|
| `dedup_by_group` | **not independent** — SBUX counted twice, inflating the mean *and* faking the sample size |
| `executable` | **not fillable** — $1 fills assumed in books holding $9 |
| `fill_price` | **mid-pricing** — gifting half the spread on every bet |
| `robust_stats` | **not robust** — top 5 of 382 bets held 89% of all P&L |
| `required_n` | **not powered** — a 30-bet bar that could only be passed by luck |
| `Registry` | **searched, not predicted** — pre-registration + a trial counter for the Deflated Sharpe Ratio |

`forward_test` now delegates to it; results are byte-identical, so nothing changed except that there's one implementation instead of several.

**`tools/test_validation.py` — 10 regression tests, all passing.** Each is named for the real failure it guards:
- `test_tail_driven_mean_is_rejected` — feeds in the exact 2026-10-30 shape (95 losses, 5 giant wins) and asserts the verdict is **not** "EDGE CONFIRMED"
- `test_underpowered_beats_significant` — a CI that excludes zero on 60 bets must still report **UNDERPOWERED**
- `test_genuine_edge_can_still_pass` — the harness must not be unfalsifiable in the *other* direction either; a real, powered, non-tail-driven edge does get confirmed
- `test_fill_price_never_the_mid` — reproduces the EXPE case: the mid flattered that edge by **0.115**
- `test_registry_blocks_backward_scoring` — refuses unfalsifiable hypotheses and refuses to amend a live one

One test failed on the first run. It asserted `required_n == 2402` and got 2401 — **my assertion was brittle, not the code**; fixed to a range.

---

## A — Paper maker → `tools/maker.py`

The argument in one line: **we pay ~4¢ of spread to chase a ~2.6% edge. The spread is 3.1× the edge.** Bürgi-Deng-Whelan find the positive return belongs to *makers*; we have been a taker on every trade for five months.

So: post fair-value quotes, log them, and see what *would* have filled. **No orders are placed. Nothing is at risk.**

**Quoting rules** (every rejection is a documented rule, not a judgement call):
- fair value = SCAR-shrunk toward the mid — low conviction means barely deviating from the crowd, which for a maker is correct
- post 1¢ inside the touch (quoting *at* the touch is being a taker with extra steps)
- **favorites only**: the contract we'd hold must cost ≥ 0.50
- needs ≥3¢ of spread, ≥$25 of real size, a non-phantom price, and a non-GAAP contract

Verified each rule rejects correctly on synthetic books, then ran it live.

**The measurement that actually matters is adverse selection.** Anyone can claim the spread on paper. A resting bid is a free option written to whoever knows more than you — it gets hit when news arrives and ignored when it doesn't. So fills, spread earned, and *how those fills resolved* are tracked separately. If spread-earned is positive but the positions lose, market making doesn't work here — and that finding arrives in **weeks, not the ~2,400 bets** a taker edge would need.

Fill detection requires the market to **cross** our price, not merely touch it, and ignores queue position — so measured fill rates are an **upper bound**. Real ones would be lower.

**First live quote: AVGO YES @ 0.887**, edge +4.6% vs fair, 10.3¢ spread, cost-if-filled 0.887 (a favorite). Wired into the daily job, so it accumulates without anyone running it.

Live books are startlingly wide — FNF **54¢**, NMAX 44¢, ABNB 43¢, SOUN 41¢. That is either the whole opportunity or a warning that nobody will ever trade with us. The experiment is designed to tell which.

---

## C — The forecast sheet → `/api/forecast_sheet` + app panel

The one asset that passes its own test: **model Brier 0.1544 vs market 0.1674**, out of sample, surviving every tightening this week.

The endpoint returns upcoming earnings with our probability, SCAR, evidence and risks — and **in the same response**, deliberately:
- the scorecard (Brier vs the market, accuracy, n)
- the realised P&L, its CI, and `money_edge_proven: false`
- four explicit limits, led by *"This is a FORECAST, not a trade recommendation. Being better calibrated than the market is not the same as making money against it."*

Each row also carries `tradeable` and `not_tradeable_because`, so a forecast can never be mistaken for a trade idea.

They ship together because **this project's entire failure mode was letting "better calibrated" quietly imply "makes money."** Separating them into different screens would rebuild that trap.

---

## State after today

| | |
|---|---|
| forecasting edge | **real** — Brier 0.1544 vs 0.1674 |
| money edge | **not proven**, not powered (~2,400 bets needed) |
| paper-maker fills | 1 quote, 0 fills, adverse selection unknown |
| validation harness | extracted, 10/10 tests passing |

Nothing shipped today created an edge. Two of the three make it harder to fool ourselves, and the third tests whether we've been standing on the wrong side of the book the entire time.

Related: [[2026-11-05 Pivot Options]], [[2026-11-05 Power, Cost and the Favorite Zone]], [[2026-11-05 Prior Art — What To Steal]].

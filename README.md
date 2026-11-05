# Polymarket Earnings Edge

A research system that tries to find mispriced earnings contracts on Polymarket — and, so
far, has mostly succeeded at proving that its own findings were wrong.

That is the honest summary, and it is the point of the project.

---

## The one-paragraph version

I built a model that estimates the probability a company beats its earnings consensus,
compares that to Polymarket's implied probability, and flags the gaps. In October it
reported a confirmed edge: **+0.394 per $1 over 351 bets, 95% CI [0.045, 0.744]**. Every
number was computed correctly. The conclusion was false. An audit found the result rested
on two double-counted rows and a pair of 3¢ longshot fills in books with no liquidity —
**the top 5 of 382 bets carried 89% of all profit.** I retracted it, rebuilt the validation
layer so the same class of error is caught automatically, and reset the evidence clock to
zero. What survived the audit is a genuine, measured forecasting advantage that does not
make money.

---

## What is actually true, with numbers

| Claim | Status |
|---|---|
| Forecasts earnings outcomes better than the market price | **Supported.** Brier **0.1544** vs market **0.1679**, n = 298 resolved bets, out of sample |
| Directional accuracy | 76% — but the base rate is ~86%, so this is *not* the interesting number |
| Makes money | **No.** Realised P&L **−0.023/$1**, 95% CI [−0.21, +0.19] |
| Could it *detect* an edge if one existed? | **Not yet.** At current payoff variance, ~17,500 bets would be needed; ~2,400 if restricted to low-variance favourites |

1,047 resolved forward predictions, logged before the events, scored after.

<img src="docs/calibration-light.svg#gh-light-mode-only" width="620" alt="Calibration curve: model vs market price">
<img src="docs/calibration-dark.svg#gh-dark-mode-only" width="620" alt="Calibration curve: model vs market price">

Both curves sit above the diagonal: the model and the market alike under-predict how often
these outcomes occur. The model is closer to it — that is the Brier win — but neither is
well calibrated, and saying so matters more than the win.

The gap between row 1 and row 3 is the whole story: **being better calibrated than a market
is not the same as being able to profit from it.** The difference is spread, liquidity and
fillability, and this project is largely a record of learning exactly how much that costs.

---

## The retraction

Worth reading in full in [`notes/2026-11-03 Edge Confirmed — Retracted.md`](notes/). Three
independent defects, each sufficient on its own:

1. **Double-counted events.** One earnings print was logged under several outcome rows, so
   a single +28.39 longshot win was counted twice. That inflated the mean *and* faked the
   sample size — which is what made the confidence interval look tight.
2. **Disputed resolutions scored as clean wins.** Eight rows, mean +3.11/$1.
3. **Phantom prices.** The filter admitted "fills" at q = 0.030 paying 32:1, in books with
   no executable size. A $1 fill at 3¢ is fiction.

Corrected: **+0.337 → +0.003/$1**, P(mean ≤ 0) = 0.51. The edge was five lucky rows.

A later audit found a fourth, deeper problem: **the success criterion itself was
incapable.** "30 bets with a CI above zero" cannot distinguish a realistic 2.6% edge from
zero at this variance. The bar could only ever have been cleared by luck — and was.

---

## What the system does now

```
workflows/   plain-language SOPs           tools/   29 Python modules, ~8,000 lines
notes/       dated research notes          literature/  129 papers, indexed
config/      tunable weights               output/  the forward-test vault
```

**Forecasting** — 12 signals drawn from the accounting literature (standardised earnings
surprise, analyst-revision momentum, dispersion, accruals, Piotroski F-score,
pre-earnings price pressure), combined by a transparent logistic blend whose weights are
refit weekly out-of-sample and adopted only if they beat the incumbent on held-out data.

**Validation** (`tools/validation.py`) — a strategy-agnostic harness, with 10 regression
tests, where every gate is a past failure turned into a rule:

- independence (one payoff per event)
- fillability (was there real size at a real price)
- price realism (pay the touch, never the mid)
- tail robustness (does the mean survive deleting its 5 best wins)
- **statistical power** (is this sample capable of answering the question at all)
- pre-registration (a rule must predate the data that scores it) and a trial counter,
  because twenty subgroup searches produce one "significant" result by chance

**Live experiment** (`tools/maker.py`) — the current hypothesis is that we have been on the
wrong side of every trade. Measured on live books, the mean spread is **8¢** while the
documented edge is **2.6%** — *the spread is 3.1× the edge, and we pay it every time.* The
module posts fair-value quotes on paper and measures what would have filled, above all
whether fills arrive precisely when we are wrong. No orders are placed.

---

## Hard rules, enforced in code

- Never output a guaranteed yes/no — always a probability, a confidence, and the evidence
- Never auto-trade. Nothing here places an order
- Public data only
- Never rely on a single signal
- Always account for liquidity, spread, fees and resolution rules before calling anything
  an edge

---

## Things this project got wrong, on the record

Kept visible deliberately — they are the most informative part.

- **Claimed an edge that did not exist** (above), for eleven days
- **Asserted a module "can't overfit"** when it was adaptively overfitting a reused
  validation split; deleted the claim, replaced the gate with a block-bootstrap test
- **Mispriced every GAAP contract** — ~22% of markets resolve on GAAP EPS while all our
  consensus data is non-GAAP. The mismatch produced a 49-point "edge" on UBER that was a
  unit error. Now refused outright rather than priced
- **Blamed the data before the code.** I reported that Kelly (2016) failed to replicate
  (+0.003, t = 0.33) and flagged it as *possibly* our implementation. It was our
  implementation: no announcement-timing alignment, no market adjustment, and the wrong
  variable. Rebuilt to spec over 1,796 events, the buying-pressure leg replicates at
  **t = 2.58**; the reversal leg does not. Half the mechanism is real — the half that
  would pay is the half we cannot find
- **Wrote the same bug into the replication harness.** Its verdict function checked the
  *sign* of each leg, not significance, and duly announced "both legs point the predicted
  way" off a reversal of −0.16% at **t = −0.23**. Caught and fixed. The instinct to accept
  a favourable sign got past me while I was actively building a tool to test for it

---

## Running it

```bash
python -m venv .venv && .venv/bin/pip install -r requirements.txt
echo "FINNHUB_API_KEY=your_key" > .env        # Polymarket needs no key

.venv/bin/python -m tools.cli predict AAPL    # one company
.venv/bin/python -m tools.cli backtest        # calibration over history
.venv/bin/python -m tools.cli forward         # score the live vault
.venv/bin/python -m tools.cli serve           # local web app on :8000
.venv/bin/python -m tools.test_validation     # 10 regression tests
.venv/bin/python -m tools.kelly_replication   # replicate the core paper (1,796 events)
.venv/bin/python -m tools.make_figures        # redraw the calibration figure
```

---

## Why it is built this way

Probabilistic reasoning and deterministic execution are kept apart: markdown SOPs describe
*what* to do, Python modules do it, and the agent coordinating them never performs the
calculation itself. Five chained steps at 90% accuracy give you 59% — so anything that can
be made deterministic is.

The same instinct drives the validation layer. A research system that cannot reject its own
conclusions will eventually report whatever its author hoped to find. This one has rejected
most of them.

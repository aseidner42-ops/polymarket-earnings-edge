---
date: 2026-11-05
tags: [polymarket, literature, prior-art, methodology, power-analysis]
---

# Prior art — what to steal, ranked by what it fixes here

Searched for comparable projects and research. Most open-source Polymarket bots are not worth copying. Two bodies of academic work are worth a great deal, and one of them says our forward test is **statistically incapable of proving what we're asking it to prove.**

---

## 1. Bürgi, Deng & Whelan — "Makers and Takers: The Economics of the Kalshi Prediction Market" (UCD, Jan 2026)

**The most important thing on this list.** 46,282 contracts / 12,403 events / 2021–Apr 2025 / 300,000+ prices. Kalshi, not Polymarket, but the same quote-driven binary microstructure.

Findings, verified from the paper itself:

| finding | number |
|---|---|
| average return on all Kalshi contracts | **−20%** |
| buyers of contracts under 10¢ | **lose over 60%** of their money |
| contracts above 50¢ | small **positive** return, statistically significant |
| **Makers** on ≥50¢ contracts, after commission | **+2.6%** (SD 33%) |
| Makers vs Takers | Makers earn more; the longshot bias is **worse on the taker side** |

### What this says about us — three hits, all painful

**(a) Our fake edge was the single best-documented losing bet in the literature.** The retracted "EDGE CONFIRMED" rested on YES fills at **q=0.030 and q=0.034** paying 32:1. That is precisely the sub-10¢ bucket that loses >60% across 46k contracts. We didn't find an anomaly; we got lucky twice in the worst zone on the board and the old scorer counted one of them twice.

**(b) We are structurally on the wrong side of every trade.** We walk the ask — we are **always a Taker**, the side the paper shows is both lower-returning and more exposed to the longshot bias. The documented positive return belongs to **Makers**. Our whole engine is built to take.

**(c) Our own data already agrees.** From the 2026-11-03 decomposition: `side=YES` (with the crowd) **+1.074**, `side=NO` (fading favorites) **−0.188**; fades **−0.273/$1 over 177 bets**. The paper's framing explains why — buying high-priced favorites is the positive-return side, and we've been systematically selling it.

### Why it hasn't been arbitraged (their Section 6) — reads as a description of our problem
- **Small volumes.** Top-decile Kalshi market averaged **$526,245** total volume; live order-book depth is far smaller. Anyone deploying real size as a Maker has to post worse prices, and many quotes never match. *Matches exactly what we measured: $100–300 top-of-book, nothing fillable.*
- **Riskiness.** SD of returns **dwarfs** the mean (33% SD on a 2.6% edge).
- **Proper risk aversion** (Samuelson 1963; Pratt–Zeckhauser 1987) — rational investors may rationally decline a small positive edge with that variance.

---

## 2. The power calculation this forces — our 30-bet bar is meaningless

If the true edge is the size the literature documents, **our pre-registered success criterion cannot detect it.**

| measurement convention | SD | bets needed for 95% CI-low > 0 |
|---|---|---|
| paper's: return on a ≥50¢ contract, +2.6% effect | 0.33 | **~620** |
| ours: leveraged payout per $1, +0.05 effect | 1.76 | **~4,700** |
| ours, restricted to q≥0.50 | 1.69 | **~16,300** |

**We set the bar at 30.** At 30 bets we cannot distinguish a real +2.6% edge from zero — which is *exactly* how a tail-driven fluke cleared it on 2026-10-30. The bar wasn't just too low; it was incapable in principle.

Two fixes, both grounded rather than tuned:
1. **Change the measurement.** Report **return on the contract price**, restricted to the ≥50¢ zone (leverage ≤2×). That is both where the literature finds profit and where SD collapses ~5×, cutting the required sample by roughly 25×.
2. **Raise the bar to a power-justified n** (~600 for a 2.6% effect), and state the detectable effect size up front. A success criterion that doesn't name the effect size it can detect is decoration.

---

## 3. Bailey, Borwein, López de Prado & Zhu — Probability of Backtest Overfitting (CSCV) + Deflated Sharpe Ratio

The rigorous version of the guard improvised on 2026-11-03 after sweeping ~20 subgroups and finding four "profitable" ones.

- **PBO / CSCV** — rank-based, non-parametric: how often does the in-sample winner underperform out-of-sample?
- **Deflated Sharpe Ratio** — parametric: does the best result clear the expected *maximum* under zero skill, given how many trials were run?
- They are complementary; passing both is stronger than either. **CSCV's block reshuffling breaks strict time ordering, so PBO does not replace walk-forward or genuine forward testing.**

**Steal:** track a trial counter (every subgroup, every weight fit, every threshold tried) and deflate by it. Right now `hypotheses.json` records *that* a search happened; it doesn't record *how many* trials, which is the input DSR needs.

---

## 4. Market making in prediction markets — the structural question

If the documented edge belongs to Makers and we are always a Taker, the strategic question is whether we can quote instead of lift.

- *Toward Black-Scholes for Prediction Markets: A Unified Kernel and Market Maker's Handbook* (arXiv 2510.15205) — stochastic-control market making for binary claims bounded in (0,1); authors note order-intensity forms for prediction markets have **not** been systematically studied, and parameters are illustrative, not calibrated.
- A **logit-space** adaptation handles the bounded price (spreads auto-compress near 0 and 1; needs a half-spread floor).
- An empirical thesis on a Polymarket BTC-strike market found model-implied spreads **1.8–5.4×** the observed bid–ask — a theoretical benchmark, not a description of real book behavior.

**Honest read:** this is a genuinely different business (inventory risk, adverse selection, quote management) and the empirical fit is weak. It is the most promising *structural* answer to "our calibration doesn't convert to money" and also the largest change in what this project is. Not a side quest.

---

## 5. Calibration benchmarks — external scoring

- **Metaculus FutureEval** (launched Feb 2026) — scores models on accuracy, calibration and discrimination as questions resolve; Bot Tournaments with ~$175k/yr in prizes; "spot scoring" counts only the forecast at question close.
- **ForecastBench** (Forecasting Research Institute) — continuously updated, unaffiliated.

**Steal:** we only ever score ourselves against our own vault. An external, adversarial benchmark would tell us whether the 0.1532-vs-0.1891 Brier advantage is real skill or an artifact of which markets we choose to cover.

---

## 6. Open-source Polymarket bots — mostly not worth copying

`pallaoro/polymarket-ai-trading` (mean reversion + LLM gate + Kelly, paper-trading, 0 stars), `Gabagool2-2/polymarket-trading-bot-python` (YES/NO mismatch arb, MIT), `Benjamin-cup/...V2` (5-minute crypto momentum). **None shows live audited returns**, and most of the write-ups are promotional posts for their own repos.

Worth having anyway: the **official clients** — `Polymarket/py-clob-client` and `polymarket/clob-client` — as a reference for correct auth and order handling if we ever place a real order.

---

## What I'd actually change here

1. **Stop buying longshots, permanently.** The <10¢ zone loses >60% over 46k contracts. `MIN_Q=0.05` is too generous; the literature says the floor belongs near **0.50**, not 0.05.
2. **Re-express P&L as return on contract price**, and run the ≥50¢ book as the primary track. ~25× more statistical power for free.
3. **Replace the 30-bet bar with a power-justified n**, and write the detectable effect size into `hypotheses.json`.
4. **Count the trials** and deflate for them (DSR/PBO).
5. **Take the maker question seriously** — it's the only read of the evidence where our calibration advantage could become money, and it is a different project.

The uncomfortable summary: **the literature already knows that taking liquidity on cheap longshots in a thin quote-driven market loses about 20–60% of your money, and that is a fair description of what our engine was built to do.** Everything we independently discovered on 2026-11-03 — the longshot tail, the losing fades, the unfillable books — is the same finding, rediscovered at n=299 instead of n=46,282.

Related: [[2026-11-03 Edge Confirmed — Retracted]], [[2026-11-03 Road to a Real Edge]], [[2026-11-03 Rank on Fills Not Fractions]].

## Sources
- Bürgi, Deng & Whelan, *Makers and Takers: The Economics of the Kalshi Prediction Market*, UCD, Jan 2026 — https://www.karlwhelan.com/Papers/Kalshi.pdf · https://cepr.org/publications/dp20631
- Bailey, Borwein, López de Prado & Zhu, *The Probability of Backtest Overfitting* — https://papers.ssrn.com/abstract=2326253 · https://escholarship.org/content/qt4w1110bb/qt4w1110bb.pdf
- Bailey & López de Prado, *The Deflated Sharpe Ratio* — https://papers.ssrn.com/abstract=2460551
- *Toward Black-Scholes for Prediction Markets* — https://arxiv.org/pdf/2510.15205
- Metaculus FutureEval — https://metaculus.substack.com/p/metaculus-futureeval-ai-forecasting-benchmark
- py-clob-client — https://github.com/Polymarket/py-clob-client

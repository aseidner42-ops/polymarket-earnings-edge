---
date: 2026-11-03
tags: [polymarket, forward-test, honesty, retraction]
---

# 2026-11-03 — "EDGE CONFIRMED" retracted

On **2026-10-30** the daily job fired `EDGE_CONFIRMED.txt`: *+0.394/$1 over 351 bets, 95% CI [0.045, 0.744]*. The app showed the green **"WE'RE GOOD ✅"** banner and `edge_guard` flipped `tradeable_now=True`.

**That verdict was wrong.** It was a scoring artifact. Corrected, the forward test says **no proven edge**.

## What actually happened

| | raw (old method) | corrected |
|---|---|---|
| bets | 382 | 277 |
| mean P&L / $1 | **+0.337** | **+0.003** |
| median | −1.00 | −1.00 |
| CI 95% | [+0.044, +0.689] | [−0.194, +0.228] |
| P(mean ≤ 0) | 0.010 | **0.509** |
| mean excl. top-5 wins | +0.036 | −0.177 |
| beats betting the favorite | "yes" | **no** (+0.003 vs +0.019) |

The whole result lived in the tail: **the top 5 bets carried 89% of all P&L**, top 10 carried 121% (i.e. everything else was net negative). Strip five bets and it's gone.

## Three defects, each independently fatal

1. **Double-counted events.** One earnings print gets logged under several outcome rows (`eps_beat`, `eps_strike`, `eps_surprise_over`). SBUX 2026-10-29 booked its **+28.39** longshot win **twice**; CBRE booked **+7.38** twice. Not independent samples — this inflated the mean *and* faked sample size, which is what made the CI look tight.
2. **Disputed resolutions scored as clean wins.** 8 rows tagged `DISPUTED-RESOLUTION`, mean **+3.11/$1**.
3. **Phantom-price floor too loose.** `0.02 < q < 0.98` admitted fills at **q=0.030 (APP)** and **q=0.034 (SBUX)** paying 32:1. Books that thin have no executable size — today's scan shows `maxUSD=0.0` across every flagged market. A $1 fill at 3¢ is fiction.

Sensitivity: the conclusion is **not** knife-edge on the floor. At q≥0.05 / ≥0.10 / ≥0.15 the mean is +0.011 / +0.007 / −0.026. All breakeven.

## Fixes shipped

- `forward_test._dedup_events()` — one bet per `(ticker, earnings_date)`.
- `DISPUTED` rows excluded from P&L.
- `MIN_Q = 0.05` phantom floor (was 0.02).
- `_robust_pnl()` — **bootstrap** CI (payouts are heavy-tailed and bimodal; the normal approximation understated uncertainty), plus median, tail share and mean-ex-top-5.
- `_verdict()` — confirmation now needs ≥30 independent bets **AND** bootstrap CI-low > 0 **AND** still positive after removing the 5 biggest wins. New "NOT ROBUST" state for a tail-driven mean.
- `daily_improve` inherits the tail leg before it can fire `EDGE_CONFIRMED.txt` again.
- `EDGE_CONFIRMED.txt` → `EDGE_CONFIRMED.retracted_2026-11-03.txt`, so the app stops claiming we're good. It can still re-fire if a real edge appears.
- `backtest._fit_logistic` hardened against non-finite signals + spurious-warning suppression (see below).

## What survived

**Calibration is real.** On the same bets the model's Brier is **0.1576 vs the market's 0.1681** — we predict the outcome better than the crowd does.

**It does not pay.** Betting that calibration is breakeven (+0.003/$1) and *worse than blindly backing the favorite* (+0.019). Being better calibrated on the event ≠ having an edge on the price. The gap is friction, thin books, and the fact that our disagreements cluster where the market is widest.

## Side note — the matmul warnings

The daily log has been spewing `divide by zero / overflow / invalid value encountered in matmul` for weeks. **Not a bug in our data.** numpy 2.0 on Apple Accelerate leaks floating-point status flags: `np.ones((500,9)) @ np.zeros(9)` reproduces all three on clean input. Suppressed at the fit loop via `np.errstate` so a *real* numerical problem would be visible instead of buried. Kept a `nan_to_num` guard + a refusal to return a non-finite model as defense in depth.

## Standing lesson

A mean that dies when you remove five rows was never an edge. Any future profitability claim must clear the tail-robustness leg. See [[honesty-mandate]].

# PUMP — edge/sizing equation

PUMP = a single **mathematical formula** that converts everything the system measures
into one **edge number** (expected value / recommended stake). SCAR answers "how much to
trust this?"; PUMP answers "how much edge is here, and how much to bet?"

## SPLIT (2026-06-26): SMG vs PUMP

The original "PUMP" (the Polymarket edge + ½-Kelly stake equation Ψ=E·w(K)·u(σ̂)·r(V)−f_c)
was RENAMED **SMG — Stake & Market-edge Gauge** (`tools/smg.py`, config `smg:`, result["smg"],
vault smg_psi/smg_stake/smg_live, 💰 badge). It needs a Polymarket price and is NOT
backtestable (no historical PM prices) — validated only on the forward vault.

**PUMP was rebuilt as a PURE-PRICE, no-Polymarket equation** at the user's request: a
pre-earnings move-pressure read off the stock alone, so it IS backtestable.

  PUMP = 100·tanh( z·conv/γ ),  z = abnormal 5-day run-up / the stock's own expected 5d move,
  conv = clip(1+β·volume_buildup, 0.5, 2.0).   `tools/pump.py`, config `pump:`, result["pump"],
  vault pump_score/pump_dir, ⚡ badge, backtest rows carry pump + post_earnings_return.

HONEST BACKTEST (350 events): corr(PUMP, post-earnings move) = **−0.026 (≈zero — does NOT
predict the reaction)**; corr(PUMP, beat) = **+0.16 (mild positive)**; sign match 54%. So PUMP
is an EXACT MEASUREMENT (how stretched the run-up is, normalized per stock) but a WEAK
PREDICTOR. Kept as a DISPLAYED read (like SCAR), deliberately NOT folded into the beat
probability (would double-count the existing pre_earnings_drift signal). The Kelly
follow/fade overlay (big stretched run-up + over-extrapolation → fade) shows only a whisper
(−0.4%) on this sample — not tradeable alone. Research only.

---

## SMG skeleton (was PUMP) — Polymarket edge + stake, design notes below

## Variable registry (symbol · meaning · source · range)

### Raw signals (each normalized to a score in [-1,+1] unless noted) — tools/signals.py
| Sym | Meaning | Source |
|----|----|----|
| s_sue   | SUE / earnings-surprise momentum (standardized) | yfinance/Finnhub surprises |
| s_str   | surprise-beat streak | surprise history |
| s_rev   | analyst estimate-revision drift | yfinance eps_revisions/eps_trend |
| s_drift | pre-earnings price drift [-5,-1] | yfinance prices |
| s_rs    | relative strength vs index+sector | yfinance |
| s_vol   | pre-earnings volume buildup | yfinance |
| s_skew  | options put/call IV skew | yfinance options |
| s_news  | news-sentiment tilt | Finnhub company-news |
| s_peer  | peer-beat rate (sector tailwind) | Finnhub peers |
| s_si    | short-interest squeeze lean | yfinance |
| s_disp  | analyst dispersion (tight=+) | yfinance earnings_estimate |
| s_acc   | accruals quality (Sloan) | yfinance financials |
| s_ftr   | fundamental trend (rev decel + margin) | yfinance financials |
| s_fil   | SEC 8-K event risk (bearish only) | EDGAR item codes |

### Underlying measured quantities (natural units)
| Sym | Meaning | Units |
|----|----|----|
| X_ext | Kelly extrapolated-return measure (ExtRet) | % (2-day BHAR, recency-wtd, 8q) |
| M     | options implied move | % |
| g     | revenue growth (YoY) | fraction |
| Δm    | operating-margin change | fraction |
| R_f   | filing event-risk score | 0..1 |
| V     | VIX regime (1y percentile) | 0..1 |
| n_a   | analyst count | int |
| disp  | forecast dispersion (high−low)/avg | fraction |
| b_s   | sector structural beat rate (Burgstahler) | prob |

### Composite outputs (already computed)
| Sym | Meaning | Where |
|----|----|----|
| P_beat | fundamental beat probability | scoring.score_fundamental |
| Π      | priced-in index (0..1) | scoring.priced_in_index |
| μ_f,σ_f | surprise fingerprint mean / std | surprise_model |
| μ̂, σ̂  | CWSF predicted surprise % / sigma | surprise_model.predict_surprise |
| κ      | CWSF coverage factor (0..1) | surprise_model |
| C      | fingerprint consistency = |μ_f|/σ_f | surprise_model |
| φ      | SCAR coherence (signal agreement, 0..1) | scar |
| α      | SCAR acceleration (2nd deriv revenue growth) | scar |
| K      | SCAR predictability "kills" (0..100) | scar |

### Market / execution layer
| Sym | Meaning | Where |
|----|----|----|
| q      | Polymarket implied prob (YES) | polymarket_client |
| p*     | market-aware probability | scoring.edge_estimate |
| τ      | edge tilt (orthogonal-signal lean) | scoring.edge_estimate |
| E      | edge vs market = p* − q | scoring.edge_estimate |
| Δq     | Polymarket price momentum | polymarket_client |
| L      | book depth ($ near top) | polymarket_client |
| f_c    | friction (½-spread + fee + haircut) | ev |

## PUMP formula skeleton (to fit/tune later)
Goal: output Ψ = expected edge per $1, and a stake fraction.

    Ψ_pump  =  E · w(K) · u(σ̂) · r(V)  −  f_c

where:
  E      = p* − q                      (core mispricing — only nonzero if orthogonal signals fire)
  w(K)   = clip(K/100, 0, 1)           (trust gate — SCAR; ~0 edge if unpredictable)
  u(σ̂)  = 1 / (1 + λ·σ̂/σ̄)            (shrink edge when CWSF surprise band is wide)
  r(V)   = 1 − γ·max(0, V−0.5)         (de-risk in high-vol regimes)
  f_c    = friction (spread/fee/haircut)

Recommended stake (capped by liquidity):
    f*  =  clamp( Ψ_pump / odds , 0 , f_max )  ,  then  size = min(f*·bankroll, L_cap)

Open design questions for later:
  - fit weights/λ/γ on the forward vault once ≥30 resolved bets (don't hand-tune)
  - should PUMP also fold μ̂ directionally (predicted surprise sign) vs just E?
  - per-sector or per-predictability-tier coefficients?
  - PUMP as expected-value (above) vs as a classifier of "tradeable / not"?

Validation plan: once built, score PUMP only on resolved vault bets (realized P&L,
95% CI, vs crowd benchmark) — same honesty bar as everything else. No synthetic data.

---

## Round-2 literature analysis — ML / LLM corpus (added 2026-06-26, still DESIGN-ONLY)

17 ML/DL/LLM papers analyzed (Gu-Kelly-Xiu, Amel-Zadeh, Schnaubelt-Seifert, Ye-Schuller,
Dhar-Chou, Bilinski "Beyond the Street", Chen-Cho-Dou-Lev, Christensen-Timmermann-Veliyev,
Kim-Muhn-Nikolaev, Lopez-Lira-Tang, FinBERT/Araci, Wu et al. press-release, Goldsmith-Pinkham
event-study, + 2 surveys/1 low-credibility paper). The over-arching, slightly ironic verdict:
**the ML literature itself argues for keeping PUMP a transparent equation, not a black box.**
GKX: a well-regularized linear/logit captures MOST of ML's gain; NN only marginally beats
penalized-linear after multiple-testing correction, and the gain is from interactions we can
name. So PUMP stays interpretable; we borrow the *features, weights, and interactions*, not the models.

### What this changes in the equation (proposals to fit later, not hand-tune)

1. **Rebuild E around a MULTI-SURPRISE blend, not EPS alone (Bilinski 2025 — headline).**
   Street EPS surprise ≈ ZERO association with announcement returns after controlling for other
   surprises; non-per-share measures dominate. Prior weight vector from his 1-SD reaction sizes
   (normalize): **NetIncome≈EBITDA≈0.98, Revenue≈0.91, OpProfit≈0.58, GrossMargin≈0.46,
   CFO≈0.28, GAAP-EPS≈0.13, street-EPS≈0**. New symbols to register: s_surpNI, s_surpEBITDA,
   s_surpRev, s_surpOP, s_surpGM, s_surpCFO.
   ⚠ FREE-DATA HONESTY: only EPS + revenue consensus are free (Finnhub). NI/EBITDA/CFO/margin
   *surprises* need a consensus we don't have → must be PROXIED by YoY/own-trend expectation and
   explicitly labeled lower-confidence. Don't pretend we have the consensus.

2. **Quality / earnings-management gate G (Bilinski + Sloan + Kelly).** "Other surprises" matter
   most exactly when street EPS is least trustworthy: small/zero EPS beat, high accruals, growth
   firm. A "beat-EPS-but-miss-CFO/NI" pattern = low-quality, reversal-prone → G shrinks E or flips
   the priced-in penalty. Ties to existing s_acc + Π + Kelly X_ext.

3. **r(V) becomes a COVERAGE/LIQUIDITY edge-LOCATOR, not just a vol de-risker
   (Christensen 2025, Schnaubelt, Lopez-Lira, Dhar-Chou).** Post-2016, drift is arbitraged away in
   liquid mega-caps; edge concentrates in **low-coverage / small-cap / illiquid / post-negative-news**
   names. Proposal: r = base · (1 + ρ·lowCoverage·smallCap) for thin names, shrink toward 0 for
   mega-caps. Uses κ (CWSF coverage) + market cap. This is likely where PUMP's real edge lives.
   (Caveat: most Polymarket earnings markets are big liquid names — so honestly flag that this
   term may rarely fire on the actual tradable universe.)

4. **Downside asymmetry + S-curve in E (Christensen + Amel-Zadeh).** Negative surprises move price
   MORE than positive; small misses punished disproportionately (convex/S-shaped response). Make the
   reaction/EV branch asymmetric and apply a fixed tanh/logistic amplifier to large |surprise| — a
   named shape, not a learned net.

5. **u(σ̂) now empirically grounded (Christensen).** High pre-announcement realized/implied vol ⇒
   smaller, more-priced-in jump ⇒ lower conviction. Keep u(σ̂) shrinking edge as the CWSF band widens
   AND add pre-vol as an input to it.

6. **w(K) confirmed (Dhar-Chou small-disjuncts; Kim-Muhn confidence-accuracy).** Only emit edge in
   high-conviction regions; a model's own stated confidence correlates with accuracy → SCAR K is the
   right gate. No change, now literature-backed.

7. **Sentiment stays SMALL, transparent, and LM-based — do NOT add paid GPT-4/ChatGPT
   (Lopez-Lira, Kim-Muhn, FinBERT, Wu).** HONEST cost verdict: the LLM return-edge is locked behind
   *paid frontier models*; Lopez-Lira explicitly shows cheaper models (BERT/FinBERT) DON'T reproduce
   it. A free tool cannot honestly claim that alpha — documented as out-of-scope, not faked. Our LM
   (Loughran-McDonald) tone already captures the cheap transparent tier. Two free upgrades worth a
   small modifier S: (a) timing gate — text only adds edge PRE-announcement (priced-in by open after,
   Wu et al., consistent with Kelly run-up window); (b) hard-soft AGREEMENT multiplier — weight tone
   up only when it agrees with the surprise sign. Keep weight small (Wu full model ≈ 4% R²; FinBERT
   misreads numbers).

8. **Horizon honesty (Goldsmith-Pinkham, Ye-Schuller, FinCall).** Short-window (announcement/3-day)
   reaction is the reliable estimate; multi-week drift is near-coin-flip, sector/regime-dependent,
   model-misspecification-prone → keep drift terms low-weight + sector-conditioned + flagged. Score
   PUMP by Brier/AUC vs the SECTOR_BEAT_RATE prior, never raw accuracy (class-imbalance illusion).

### Proposed refined skeleton (for later fitting)
    Ψ_pump = [ E(multi-surprise, asymmetric, S-curve) · G(quality) ] · w(K) · u(σ̂, preVol) · r(V, κ, size) − f_c
register later: s_surp{NI,EBITDA,Rev,OP,GM,CFO}, G (quality gate), S (sentiment modifier, small).

### Dropped / out-of-scope (honest)
- GPT-4 / ChatGPT return prediction (Kim-Muhn, Lopez-Lira) — paid API, can't fund in a free tool.
- FinBERT in-process — open weights but heavy (torch/transformers, model download), black-box, and
  its accuracy gain is on sentence-labeling, NOT returns. Documented as a future heavyweight option.
- Event-embedding CNN / multimodal call models (Ding, FinCall) — black-box, not free-in-process.
- "Stock Market Prediction via Multi-Source MIL" (IJESAT) — low-credibility venue, NO empirical
  results; recommend NOT citing it (would weaken the project's credibility).
- 13,881-tag XBRL ML (Chen-Cho-Dou-Lev) — borrow only the top few tags (net income, EPS, tax) as
  transparent inputs; full ML out of scope.

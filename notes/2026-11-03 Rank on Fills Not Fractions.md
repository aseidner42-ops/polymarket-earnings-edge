---
date: 2026-11-03
tags: [polymarket, selection, liquidity, gaap, bug]
---

# Rank on fills, not fractions

Third pass of 2026-11-03. Follows [[2026-11-03 Edge Confirmed — Retracted]] and [[2026-11-03 Road to a Real Edge]].

Selection ranked candidates by **`net_edge`** — a paper fraction measured against the *mid* price. That is what steered the scanner into unfillable books, and those bets are what produced the fake P&L. Fixed at the source.

## 1. Rank on expected dollars

`ev._executable` now walks the book to the largest stake it can actually absorb and returns **`expected_profit_usd` = max_usd × (win_prob − avg_fill − fee)**, priced at the **average fill for the whole stake**, so slippage is paid rather than assumed away.

`scanner.edge_quality` is now `expected_profit_usd × (SCAR/100)` — conviction-weighted **money**, not a percentage. **No paper fallback:** a market we can't fill scores 0 and is tagged `paper_only`. It is still scanned, logged and shown; it just can never outrank something real.

> A 40% edge on a book that fills $50 is worth $20. A 3% edge that fills $5,000 is worth $150. Ranking by fraction gets that backwards every time.

Ladder now starts at **$25** (was $50) — these books are small, and reporting `max_usd=0` for a market that could absorb $30 understates fillability, which is as dishonest as overstating it.

## 2. Say *why* something is unfillable

`fill_reason` distinguishes three very different things:
- **no edge at the touch** — "pays 0.21 for a 0.20 chance (mid flatters it)"
- **real edge but no size** — only ~$X on the top 3 levels
- **edge eaten by slippage** before $25

Plus a phantom guard: a price pinned outside 0.05–0.95 is a resolved book, not a 99% opinion. Scanner now uses the **same** band as the forward scorer, so scanning and scoring finally agree.

## 3. What the live market actually looks like

Every one of the 10 markets scanned came back **unfillable**:

| | mid | model | spread | top-3 $ | why |
|---|---|---|---|---|---|
| UBER | 0.185 | 0.770 | 5¢ | $174 | no edge at the touch |
| EXPE | 0.695 | 0.960 | **23¢** | $49 | no edge at the touch |
| FIS | 0.587 | 0.829 | 17¢ | — | no edge at the touch |

EXPE's "market probability of 0.695" is the midpoint of a **23-cent chasm** between a $9 ask and a $29 bid. You'd pay **0.81**. Our 0.96 model is not 27 points of edge — it's 15 points *before* costs, in a book that holds $9.

**This is the honest state of the opportunity set: not "we can't find the edge," but "there is nothing fillable to bet on right now."** The scanner was never wrong about the gaps; it was wrong to call them money.

## 4. A real bug found along the way: GAAP vs non-GAAP

One candidate *did* come back fillable — UBER strike-gap, `max_usd=$250`, `E[$]=$91`, exec edge **+42%**. It was a **unit error**.

The contract: *"resolves Yes if Uber reports **GAAP EPS** greater than $0.98"* — and **$0.98 is the GAAP consensus**. Our `consensus_eps` was **$0.903**, the *adjusted/non-GAAP* figure, and our whole surprise distribution is built from adjusted history.

So:
- `strike_gap` read as **−8.5%** ("strike far above consensus") when the true gap is **~0**;
- P(GAAP EPS > strike) was computed from an **adjusted-EPS** distribution — wrong distribution entirely, and for a firm whose GAAP line swings on equity-stake marks that gap dwarfs any edge.

Result: **model 0.68 vs market 0.19.** The market was right; we had an apples-to-oranges error wearing the costume of a 49-point edge. Exactly the shape of the thing retracted this morning — and it was about to be the *first* bet in the re-baselined `strike_gap_v1`.

**Fixed.** `parse_eps_strike` returns a three-state **`eps_basis`** (`gaap` / `non_gaap` / `None`) — "not non-GAAP" was never the same as "explicitly GAAP". When a contract resolves on GAAP: no `contract_prob`, no edge, and an explicit message —

> NO EDGE COMPUTED — this contract resolves on GAAP EPS; our consensus and surprise history are non-GAAP/adjusted. Different measures: any apparent gap is our unit error, not a mispricing.

`eps_basis` is now logged per bet, and GAAP rows are excluded from P&L, Brier and hypothesis scoring — they answer a different question.

**Scope: 8 of 36 live markets (~22%) resolve on GAAP.** We have been systematically mispricing every one.

## 5. Also shipped
- Vault records `expected_profit_usd` + `eps_basis` — selection now logs what it ranked on.
- `POTENTIAL_EDGE.txt` tags every line `[FILLABLE]` / `[PAPER-ONLY]` with the reason, spread and E[$].
- Notifications prefer a fillable candidate; a paper one pings as **"PAPER ONLY, can't fill"** with the reason inline. Nothing is hidden — you still see every potential — it just can't masquerade as money.
- App scan table: new **E[$]** and **FILL?** columns, default sort = fillable first then expected dollars, hover PAPER for why.

## The through-line
This morning's fake edge wasn't one bad number — it was a pipeline that **selected** on fiction (paper fractions off mids in empty books), **priced** at a mid you can't trade, and **scored** without asking whether the bet could be filled or whether the contract even measured the same thing we modelled. All four are now closed.

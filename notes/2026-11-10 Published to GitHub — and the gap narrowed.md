# 2026-11-10 — Published to GitHub, and the gap narrowed

**Repo:** https://github.com/aseidner42-ops/polymarket-earnings-edge · public · `main` · 103 files, 2.6 MB

## What shipped

`gh` CLI installed, global git identity set, repo created public and pushed. Three
pre-existing commits landed intact, plus two new ones today.

Verified before and after push: no `.env`, no `credentials.json`, no `token.json`,
no key-shaped strings in any tracked file, 0 PDFs. The 184 MB of papers stay local;
`literature/README.md` indexes all 129 of them instead. Both calibration SVGs present
and resolving.

Also stopped tracking `.obsidian/graph.json` and the five launchd notify flags — they
are rewritten on every automated run, so `git status` was permanently dirty with no
information in it. `output/EDGE_CONFIRMED.retracted_2026-11-03.txt` stays tracked
deliberately: it is the record of the retraction, not a live trigger.

## The thing that actually matters today

The README went public claiming **Brier 0.1544 vs market 0.1679 at n = 298**. The vault
had already grown past that. Recomputed:

| | at n = 298 | at n = 322 |
|---|---|---|
| model Brier | 0.1544 | **0.1581** |
| market Brier | 0.1679 | **0.1677** |
| **gap** | **0.0135** | **0.0096** |
| realised P&L | −0.023/$1 | **−0.029/$1**, CI [−0.21, +0.17] |
| bets needed to resolve 2.6% | ~17,500 | **~16,909** (SD 1.725) |

The model still beats the market. But **the gap is shrinking as the sample grows** —
it has lost 29% of its width over 24 additional bets. The honest reading is that part
of the original margin was sample noise, and the remaining margin may keep eroding.

That is the one claim this project has been willing to stand behind, and it got
smaller the moment it was published. Both README and case study now say so in the
headline row rather than in a footnote, and the figure was regenerated from the same
vault so the chart and the prose cannot drift apart.

## Why it's written this way

A published number that only ever moves in its own favour is precisely the failure
retracted on 2026-11-03. The defense is not intent — it is that the figure is generated
from `output/edge_log.csv` by `tools/make_figures`, so it cannot be left at a flattering
stale value without someone actively choosing to.

Standing rule recorded: when the vault grows, re-run the figure generator and resync
both documents, **including when the new number is worse.**

## Still true, unchanged

- Money edge: **absent and underpowered.** 322 bets against ~16,909 needed.
- Median bet still loses its entire stake.
- Nothing places an order.

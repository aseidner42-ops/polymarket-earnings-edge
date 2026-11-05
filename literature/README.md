# Literature

129 papers underpin this project. This index exists because a folder of PDFs proves
nothing — what matters is which finding drove which line of code, and which papers were
read, considered, and *not* used.

The PDFs themselves are not committed (184 MB, and they are copyrighted). Each entry below
names the paper so it can be found; the filename is given where it isn't obvious.

---

## Core — the paper the project is built on

**Kelly (2016), "Pre-Earnings Announcement Over-Extrapolation"**
`2016_spring_finance_seminar_series_peter_kelly_paper_updated_3_22_2016.pdf`

Individual investors over-extrapolate from a firm's past earnings-announcement returns:
after a run of good announcement returns they grow optimistic, over-buy in the ~5 days
before the next print, and the stock reverses afterwards. The predictor is **ExtRet**, a
recency-weighted average of the 2-day [0,+1] announcement-window returns over the last 8
quarters.

→ Implemented in `tools/snapshot.py` (ExtRet) and `tools/pump.py` (the PUMP pressure score).

**Honest status:** we could not reproduce the return-predictability on our data. Across 480
events, PUMP's correlation with the post-earnings return is **+0.03**, and a quintile sort
gives top-minus-bottom **+0.003, t = 0.33** — no effect, and the wrong sign versus Kelly.
This may be our implementation rather than the finding (we use a 2-day window on free data,
not the paper's portfolio-sort methodology over 1983–2014). Recorded rather than buried:
see `notes/2026-11-05 Pivot Options.md`.

---

## Papers that drive specific mechanics

| Paper | What it contributes | Where |
|---|---|---|
| **Bernard & Thomas (1989)**, post-earnings-announcement drift | Standardised surprise, drift structure | `signals.sue_momentum` |
| **Livnat & Mendenhall (2006)**, comparing PEAD across surprise definitions | Surprise ÷ own surprise-volatility, not raw surprise | `signals.sue_momentum` |
| **Foster, Olsen & Shevlin (1984)**, earnings releases and anomalies | Standardisation of the surprise measure | `signals.sue_momentum` |
| **Stickel (1991)**, common stock returns around revisions | Analyst revisions carry information | `signals.analyst_revision` |
| **Chan, Jegadeesh & Lakonishok (1996)**, momentum strategies | Revision momentum | `signals.analyst_revision` |
| **Diether, Malloy & Scherbina (2002)**, differences of opinion | Wide analyst dispersion ⇒ noisier, weaker prediction | `signals.dispersion` |
| **Johnson (2004)**, forecast dispersion | Dispersion as a risk proxy | `signals.dispersion` |
| **Burgstahler & Dichev (1997)**, earnings management to avoid decreases | Firms manage to just beat ⇒ structural beat-rate prior | `scoring.SECTOR_BEAT_RATE` |
| **Frazzini & Lamont (2007)**, "Dumb money" | Small earnings-announcement premium | `reaction.announcement_premium` |
| **DellaVigna & Pollet (2009)**, investor inattention and Friday announcements | Friday / after-close prints amplify drift | `reaction.inattention_amp` |
| **Hirshleifer, Lim & Teoh (2009)**, driven to distraction | Attention-constrained underreaction | `reaction.inattention_amp` |
| **Sloan (1996)**, do stock prices fully reflect accruals | Earnings quality = (NI − CFO)/assets | `signals.accruals` |
| **Piotroski (2000)**, value investing with financial statements | F-score as a fundamental-health signal | `signals.piotroski` |
| **Loughran & McDonald (2020)**, textual analysis in finance | Finance-specific sentiment dictionary | `tools/lm_dict.json` |
| **Barberis, Shleifer & Vishny (1998)**, investor sentiment | Under/overreaction model behind Kelly | informs tuning |

---

## Papers that shaped the method rather than a signal

**Bürgi, Deng & Whelan (2026), "Makers and Takers: The Economics of the Kalshi Prediction
Market"** — 46,282 contracts. Buyers of contracts under 10¢ lose **over 60%**; contracts
above 50¢ earn a small significant positive return; **makers** on ≥50¢ earn +2.6% after
commission, and the favourite-longshot bias is **worse on the taker side**.

This paper changed the project more than any other. It explained, from outside our own
data, why our fades lost and why our one "confirmed" edge was built on 3¢ longshot fills.
→ `ev.evaluate` longshot gate, `validation.FAVORITE_COST`, `tools/maker.py`, and the
`favorite_zone_v1` pre-registration.

**Bailey, Borwein, López de Prado & Zhu, "The Probability of Backtest Overfitting"** and
**Bailey & López de Prado, "The Deflated Sharpe Ratio"** — a single significant result is
not evidence when it was selected from many trials.
→ `validation.Registry` trial counter and the pre-registration rule.

**Fama (1970), efficient capital markets**; **Fama, Fisher, Jensen & Roll (1969)**;
**Ball & Brown (1968)**; **Beaver (1968)** — the baseline case that this is hard, and the
event-study methodology the backtest follows.

**MacKinlay (1996)**, **Brown & Warner (1985)**, **Kothari & Warner (1997)** — event-study
mechanics: abnormal return windows, test specification.

---

## Read and deliberately not used

Recording these matters as much as the ones above — most of a research process is
rejection.

- **Options-based**: Black-Scholes (1973), Merton (1973), Cremers & Weinbaum (2010) on
  put-call parity deviations. We use options only for the implied move; no options
  strategy was built, because our signals do not predict returns.
- **LLM / NLP on filings**: Alex Kim (2024) on financial statement analysis with LLMs,
  Lopez Lira (2024) on ChatGPT and returns, FinBERT, "More Than Words" (Tetlock),
  "The Power of Voice". Interesting, and all require data or compute we do not have
  point-in-time. The one piece adopted is the Loughran-McDonald dictionary, which is
  cheap and well-validated.
- **Management-forecast credibility**: Rogers & Stocken (2005), Gu & Li (2007),
  Kothari, Shu & Wysocki (2009) on withholding bad news. Guidance is the thing that
  actually moves stocks on earnings day — and is exactly what free data does not give us
  in structured form. This is a known, documented gap, not an oversight.
- **Multi-dimensional surprise**: "Beyond the street EPS surprise", Bilinski-style
  multi-surprise. Tested on 480 events: base miss rate 14% → pattern 17.5%. Mild lift,
  largely redundant with the existing fundamental-trend confirmer. **Documented, not
  shipped** — see the low-quality-beat note.

---

## What this list is honest about

Most of these papers establish effects in large, clean, point-in-time datasets over
decades. We work with free data, a few hundred events, and no historical Polymarket
prices. Citing a paper is not evidence that the effect is present in our implementation —
the Kelly result above is the clearest demonstration of that, and the reason the project
now leans on forward testing rather than on literature-backed assertion.

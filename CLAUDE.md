# Agent Instructions

You're working inside the **WAT framework** (Workflows, Agents, Tools). This architecture separates concerns so that probabilistic AI handles reasoning while deterministic code handles execution. That separation is what makes this system reliable.

## The WAT Architecture

**Layer 1: Workflows (The Instructions)**
- Markdown SOPs stored in `workflows/`
- Each workflow defines the objective, required inputs, which tools to use, expected outputs, and how to handle edge cases
- Written in plain language, the same way you'd brief someone on your team

**Layer 2: Agents (The Decision-Maker)**
- This is your role. You're responsible for intelligent coordination.
- Read the relevant workflow, run tools in the correct sequence, handle failures gracefully, and ask clarifying questions when needed
- You connect intent to execution without trying to do everything yourself
- Example: If you need to pull data from a website, don't attempt it directly. Read `workflows/scrape_website.md`, figure out the required inputs, then execute `tools/scrape_single_site.py`

**Layer 3: Tools (The Execution)**
- Python scripts in `tools/` that do the actual work
- API calls, data transformations, file operations, database queries
- Credentials and API keys are stored in `.env`
- These scripts are consistent, testable, and fast

**Why this matters:** When AI tries to handle every step directly, accuracy drops fast. If each step is 90% accurate, you're down to 59% success after just five steps. By offloading execution to deterministic scripts, you stay focused on orchestration and decision-making where you excel.

## How to Operate

**1. Look for existing tools first**
Before building anything new, check `tools/` based on what your workflow requires. Only create new scripts when nothing exists for that task.

**2. Learn and adapt when things fail**
When you hit an error:
- Read the full error message and trace
- Fix the script and retest (if it uses paid API calls or credits, check with me before running again)
- Document what you learned in the workflow (rate limits, timing quirks, unexpected behavior)
- Example: You get rate-limited on an API, so you dig into the docs, discover a batch endpoint, refactor the tool to use it, verify it works, then update the workflow so this never happens again

**3. Keep workflows current**
Workflows should evolve as you learn. When you find better methods, discover constraints, or encounter recurring issues, update the workflow. That said, don't create or overwrite workflows without asking unless I explicitly tell you to. These are your instructions and need to be preserved and refined, not tossed after one use.

## The Self-Improvement Loop

Every failure is a chance to make the system stronger:
1. Identify what broke
2. Fix the tool
3. Verify the fix works
4. Update the workflow with the new approach
5. Move on with a more robust system

This loop is how the framework improves over time.

## File Structure

**What goes where:**
- **Deliverables**: Final outputs go to cloud services (Google Sheets, Slides, etc.) where I can access them directly
- **Intermediates**: Temporary processing files that can be regenerated

**Directory layout:**
```
.tmp/           # Temporary files (scraped data, intermediate exports). Regenerated as needed.
tools/          # Python scripts for deterministic execution
workflows/      # Markdown SOPs defining what to do and how
.env            # API keys and environment variables (NEVER store secrets anywhere else)
credentials.json, token.json  # Google OAuth (gitignored)
```

**Core principle:** Local files are just for processing. Anything I need to see or use lives in cloud services. Everything in `.tmp/` is disposable.

## Bottom Line

You sit between what I want (workflows) and what actually gets done (tools). Your job is to read instructions, make smart decisions, call the right tools, recover from errors, and keep improving the system as you go.

Stay pragmatic. Stay reliable. Keep learning.

---

# Project: Polymarket Earnings Edge Tool

This project applies the WAT framework to a specific domain: **finding mispriced Polymarket earnings markets**.

## What it does
Estimate the true probability that a company beats EPS / revenue / a custom threshold, then compare that estimate to **Polymarket's implied probability** to surface positive-expected-value trades. Grounded in earnings-announcement academic literature (see `information/` and the PDFs in the repo root).

## Central theory — Kelly et al. (2016), "Pre-Earnings Announcement Over-Extrapolation"
The project's anchor paper (`2016_spring_finance_seminar_series_peter_kelly_paper_updated_3_22_2016.pdf`). Finding: individual investors **over-extrapolate from a firm's past earnings-announcement returns** — when recent announcement returns are high they grow overly optimistic, **over-buy in the ~5 days before** the next print (run-up), and the stock **reverses after** the announcement. Their predictor is the **extrapolated-return measure (ExtRet)**: a recency-weighted average of the 2-day ([0,+1]) announcement-window returns over the last 8 quarters. We implement ExtRet in `tools/snapshot.py` from a user-supplied TradingView price snapshot; high ExtRet ⇒ beat likely already priced in (Q2) and downside reversal risk after the print (Q3). Other papers (Bernard-Thomas, Ball-Brown, Stickel, Chan, Jegadeesh, Beaver, Sloan, Fama) inform supporting signals — use them at your discretion from `information/`.

## Papers incorporated (accuracy upgrades)
Beyond Kelly 2016 (core), these now drive specific mechanics — all in `information/`:
- **Bernard-Thomas 1989 / Livnat-Mendenhall 2006 / Foster-Olsen-Shevlin 1984** → `sue_momentum` is standardized SUE (surprise ÷ own surprise-volatility).
- **Stickel 1991 / Chan-Jegadeesh-Lakonishok 1996** → `analyst_revision` uses real estimate revisions (yfinance up/down counts + estimate drift).
- **Diether-Malloy-Scherbina 2002 / Johnson 2004** → `dispersion` signal (tight consensus = predictable; wide = noisy/weak).
- **Burgstahler-Dichev 1997** → per-sector structural beat-rate prior sets the logit intercept (`scoring.SECTOR_BEAT_RATE`).
- **Frazzini-Lamont 2007** → small earnings-announcement premium in the `stock_up` reaction (`reaction.announcement_premium`).
- **DellaVigna-Pollet 2009 / Hirshleifer-Lim-Teoh 2009** → Friday / after-close prints amplify post-earnings drift (`reaction.inattention_amp`).
- **Sloan 1996** → `accruals` signal: earnings quality = (Net Income − Operating Cash Flow) / Total Assets; low/negative = cash-backed & persistent, high = low-quality/mean-reverting.
- **Barberis-Shleifer-Vishny 1998** (under/overreaction model behind Kelly) — documented; informs tuning.
Calibration: isotonic + reliability diagram and walk-forward OOS validation live in `backtest.py`.

## On validation (read before adding more backtests)
Kelly already established the *effect* in-sample. Our backtest (v1.1) only calibrates *our implementation* on shallow free history — do not over-invest in re-proving the theory. The primary ongoing validation is **forward-testing**: `--log` each live prediction, then `tools/forward_test.py` scores them after earnings resolve (model vs realized, model Brier vs market Brier). The effect is concentrated in the week before the print, so log predictions in that window.

## Three questions it keeps separate
1. **Fundamentals** — will the company beat official analyst consensus (EPS, revenue)?
2. **Priced-in** — has the stock already priced the beat in? (pre-earnings over-extrapolation, Kelly 2016)
3. **Edge** — is Polymarket over/under-pricing the outcome vs our model probability?

## Hard rules (do NOT violate)
- Never output a guaranteed yes/no. Always a probability + confidence + evidence.
- Never auto-trade.
- Public data only. No insider / non-public information.
- Never rely on a single signal.
- Always account for Polymarket liquidity, spread, fees, and resolution rules before flagging an edge.

## Tools map (`tools/`)
- `config.py` — env + path loading, shared constants.
- `finnhub_client.py` — consensus EPS/revenue estimates, historical surprises, earnings dates.
- `market_data.py` — yfinance: prices, volume, options-implied move/skew, relative strength.
- `polymarket_client.py` — parse a pasted market URL/slug; read price + resolution rules (Gamma/CLOB).
- `signals.py` — compute each academic signal, normalized to a score.
- `scoring.py` — combine signals → probability + confidence (transparent logistic blend).
- `ev.py` — net-of-cost expected value vs Polymarket price.
- `snapshot.py` — parse a snapshot timeframe (d/w/y), load a TradingView OHLCV CSV, compute the Kelly ExtRet over-extrapolation measure (CENTRAL signal).
- `predict.py` — orchestrate one company/outcome → result dict.
- `report.py` — render terminal table + save CSV/markdown.
- `backtest.py` — validate + calibrate the beat model on historical surprises (calibration metrics, weight recommendation).
- `edge_log.py` — append every live model-vs-Polymarket gap for forward evaluation.
- `forward_test.py` — resolve logged predictions after earnings; model vs market skill.
- `webapp.py` — local Flask web platform (serves `web/index.html`; `/api/predict|backtest|forward|upload`). Thin UI layer only — no logic; reuses `report.ROWS` so web + CLI match.
- `cli.py` — entrypoint (`check` / `predict` / `backtest` / `forward` / `serve`).

## Data source reachability (important)
- **Pipeline (Python-reachable):** Finnhub (consensus, surprises, recommendations, free company-news) + yfinance (price, volume, options). These feed `signals.compute_all` automatically.
- **Agent-driven only (NOT reachable from Python):** TradingView MCP tools and WebSearch/WebFetch run in the agent harness, not the Python process. Use them for live confirmation / filling gaps by hand — see `workflows/tradingview_confirm.md`. Do not try to import or call them from `tools/*.py`.

## Config
- `config/weights.yaml` — signal weights, default thresholds, friction params (spread/fee/haircut). Tune here, not in code.

## Setup
- `.venv/` Python env. `pip install -r requirements.txt`.
- Secrets in `.env` (`FINNHUB_API_KEY`). Polymarket APIs need no key.

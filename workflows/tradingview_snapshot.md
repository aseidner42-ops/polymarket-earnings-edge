# Workflow: Capture a TradingView market snapshot for the Kelly signal

## Why
The project's central signal is Kelly et al. (2016) over-extrapolation: a
recency-weighted average of the firm's last 8 earnings-announcement returns
(ExtRet). Computing it needs a price history covering those announcements. The
user supplies a **market snapshot** and indicates its timeframe (days / weeks /
years, e.g. `5y`, `180d`, `52w`). ~2 years of DAILY bars is enough for the 8
windows; longer adds context.

## Inputs
- Ticker
- Snapshot timeframe (days / weeks / years) — the user states how far back

## Two ways to get the snapshot CSV
**A. Agent pulls via TradingView MCP (no manual export):**
1. `parse_timeframe(<tf>)` (tools/snapshot.py) → resolution + bar count.
2. `mcp__tradingview__chart_set_symbol` <ticker>; `mcp__tradingview__chart_set_timeframe` <resolution>.
3. `mcp__tradingview__data_get_ohlcv` with `summary=false`, `count` = bars
   (max 500/pull; for >500 daily bars, `chart_scroll_to_date` + repeat).
4. Save the returned bars to `.tmp/<ticker>_snapshot.csv` with header
   `time,open,high,low,close,volume` (time = unix seconds — the loader handles it).

**B. User exports from TradingView:** chart → Export chart data → CSV → pass its path.

## Use it
`python -m tools.cli predict <ticker> --outcome stock_up \
   --snapshot .tmp/<ticker>_snapshot.csv --snapshot-timeframe <tf>`

## Reminders / learnings
- MCP tools run in the agent, NOT in Python — that's why we bridge via a saved CSV.
- Kelly's effect is concentrated in the **~5 trading days before** the print (run-up)
  and reverses after — so run this in the week ahead of earnings for live use.
- If announcement windows can't be aligned (snapshot too short / wrong dates), the
  Kelly signal falls back to the weaker recent-run-up band and the report flags it.
- Daily resolution is preferred for the [0,+1] window accuracy; weekly/monthly is
  coarser but works for long-horizon context.

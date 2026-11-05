# Workflow: TradingView live confirmation (agent-driven)

## Why this is separate from the Python pipeline
The TradingView MCP tools run inside the Claude agent harness, **not** inside the
Python process. `tools/*.py` cannot call them. So TradingView is a confirmation /
deep-dive layer the AGENT drives by hand around a prediction — it does not feed
`signals.compute_all` automatically. (yfinance + Finnhub already cover automated
price/volume/options in the pipeline.)

## When to use
After `predict` produces a probability, to sanity-check the price signals against
a live chart before trusting an edge — especially for `stock_up` and priced-in (Q2).

## Steps (agent)
1. `mcp__tradingview__tv_health_check` — confirm desktop connected.
2. `mcp__tradingview__chart_set_symbol` to the ticker; set a daily timeframe.
3. `mcp__tradingview__quote_get` — live price vs the prediction's assumptions.
4. `mcp__tradingview__chart_manage_indicator` to add RSI / volume; read with
   `mcp__tradingview__data_get_study_values`.
5. `mcp__tradingview__data_get_ohlcv` (summary=true) — confirm the pre-earnings
   run-up / volume buildup the model saw.
6. Optionally `mcp__tradingview__capture_screenshot` for the record.

## Output
A short note appended to the prediction: does the live chart confirm or contradict
the pre-earnings drift / over-extrapolation / volume signals? Contradiction → lower
confidence or widen the uncertainty haircut before acting.

## Web fallback
For news/sentiment or a missing earnings date, the agent can also use WebSearch /
WebFetch (free) — likewise agent-driven, not in the Python pipeline. The automated
news-sentiment signal uses Finnhub `/company-news` (free) instead.

# Workflow: Predict an earnings/revenue outcome and compare to Polymarket

## Objective
Estimate the probability that a company achieves a chosen earnings outcome, then
compare that estimate to the Polymarket implied probability to surface a possible
edge. Always probabilistic, evidence-backed — never a guaranteed yes/no.

## Inputs
- Ticker or company name (required)
- Outcome (default `eps_beat`). One of:
  - `eps_beat` — beat consensus EPS (threshold = beat %, default 0)
  - `eps_surprise_over` — EPS surprise ≥ threshold %
  - `revenue_beat` — beat consensus revenue (threshold = beat %)
  - `revenue_over` — revenue ≥ threshold dollars (needs consensus revenue)
  - `stock_up` — stock rises after earnings (least reliable; heuristic)
- Threshold (optional; units depend on outcome)
- Polymarket market URL or slug (optional but required for an edge)

## Tools to use
1. `tools/cli.py predict <ticker> --outcome <outcome> [--threshold N] [-p <url>] [--save] [--log]`
   which internally runs:
   - `finnhub_client.resolve_symbol`, `get_next_earnings`, `get_earnings_surprises`,
     `get_recommendation_trends`
   - `market_data.*` (price/volume/relative-strength/options)
   - `signals.compute_all` → `scoring.*` → `predict.predict`
   - `polymarket_client.get_market` → `ev.evaluate`
   - `report.render` / `report.save`, `edge_log.log_edge`

## Expected output
A table with one column per ticker showing: earnings date, consensus EPS/revenue,
predicted surprises, Q1 fundamental beat prob, Q2 priced-in index, model
probability, Polymarket implied prob, Q3 net edge, confidence, recommendation,
main evidence, and risks/missing data.

## Edge cases & learnings
- Finnhub free tier: `revenueEstimate`, eps-estimate detail, and price targets may
  be premium-gated (HTTP 403) → those fields come back None; the report flags them.
- If no Polymarket URL is given, the edge columns are blank — only the model runs.
- `revenue_over` needs consensus revenue AND a dollar threshold or it returns
  "uncertain".
- Always read the market `resolution_text` before acting — wording (GAAP vs
  adjusted, fiscal quarter) changes what "beat" means.
- Rate limit ~60 req/min on Finnhub; one prediction is well under that.

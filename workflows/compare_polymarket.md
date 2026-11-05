# Workflow: Resolve a Polymarket market and compute net-of-cost edge

## Objective
Turn a pasted Polymarket URL/slug into an implied probability + resolution rules,
then compare to the model probability and report a friction-adjusted edge.

## Inputs
- Polymarket market or event URL, or a bare slug
- A model probability for the SAME outcome the market resolves on

## Tools to use
- `polymarket_client.parse_slug` → `polymarket_client.get_market`
  (Gamma `/markets` then `/events`; CLOB `/midpoint` for live YES price)
- `ev.evaluate(model_prob, market)`

## Expected output
`yes_price` (implied prob), spread, liquidity, volume, resolution_text, and from
`ev`: recommended side, gross edge, net edge after frictions, EV per dollar, and a
recommendation string (or "no actionable edge").

## Edge cases & learnings
- Match the market's exact resolution: which fiscal quarter, GAAP vs adjusted EPS,
  revenue definition. A mismatch invalidates the comparison — surface it as a risk.
- Frictions (config/weights.yaml `frictions`): half-spread + fee +
  uncertainty_haircut subtracted before flagging; net edge must clear
  `min_edge_to_flag`.
- Low liquidity (< $5k) → recommendation is tagged CAUTION; size down or skip.
- Never auto-trade. Output is decision support only.

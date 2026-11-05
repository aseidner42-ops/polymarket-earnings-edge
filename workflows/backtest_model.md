# Workflow: Backtest the fundamental beat model

## Objective
Measure whether the rules-based beat model predicts actual EPS beats better than
the naive "companies usually beat" baseline, and check probability calibration.

## Inputs
- One or more tickers (a basket gives more quarters → more reliable metrics)
- `--quarters` per ticker (default 8)

## Tools to use
- `tools/cli.py backtest <tickers...> --quarters N` → `backtest.backtest`

## Expected output
JSON metrics: accuracy, precision, recall, F1, confusion matrix, baseline
(always-beat) accuracy, `beats_baseline` flag, and a calibration table
(predicted-prob bucket → realized beat rate).

## Method & limitations
- For each past quarter we rebuild only signals knowable beforehand: prior
  surprises + price/volume/relative-strength as-of the announcement.
- Finnhub gives fiscal-period-end, not announcement date → announcement is
  approximated as period-end + 40 days for the price-window as-of.
- Analyst-revision and options signals have no free history → absent in backtest.
- Interpretation: a model that only ties the always-beat baseline adds no edge;
  look for higher accuracy AND calibration where high-prob buckets really do beat
  more often. Tune `config/weights.yaml` (intercept/gain/weights), re-run.

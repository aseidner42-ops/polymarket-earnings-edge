#!/bin/bash
# Daily near-autonomous self-improvement (launchd, ~7am — see com.earningsedge.dailyimprove.plist).
# Backtests 50 rotating tickers, recalibrates the SCAR threshold (guardrailed), forward-resolves
# the vault, and notifies when forward tests finish. Does NOT rewrite model logic (see the
# docstring in tools/daily_improve.py). Needs the machine awake (runs on next wake if asleep).
cd "$(dirname "$0")/.." || exit 1
source .venv/bin/activate 2>/dev/null || exit 1
python -m tools.daily_improve >> output/daily_improve.log 2>&1

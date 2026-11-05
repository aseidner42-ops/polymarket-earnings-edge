#!/bin/bash
# Weekly automated job (launchd, Mon 8am — see com.earningsedge.weeklyscan.plist).
# 1) SCAN live Polymarket earnings/revenue markets -> auto-log every one to the vault,
#    append a landscape snapshot, and raise output/EDGE_FOUND.txt if anything is tradeable.
# 2) FORWARD-resolve: score logged predictions whose earnings have now passed (model vs
#    market, realized outcome) so the vault fills AND grades itself with no manual step.
# Needs the machine awake (launchd runs it on next wake if asleep).
cd "$(dirname "$0")/.." || exit 1
source .venv/bin/activate 2>/dev/null || exit 1
LOG=output/auto_scan.log
echo "===== $(date) weekly job start =====" >> "$LOG"
echo "--- scan ---" >> "$LOG"
python -m tools.cli scan --limit 20 >> "$LOG" 2>&1
echo "--- forward resolve/score ---" >> "$LOG"
python -m tools.cli forward >> "$LOG" 2>&1
echo "--- autofit (walk-forward, OOS-gated weight fitter) ---" >> "$LOG"
python -m tools.autofit >> "$LOG" 2>&1
echo "===== $(date) weekly job done =====" >> "$LOG"

#!/bin/bash
# Double-click this file in Finder to launch Earnings Edge.
# It sets up the environment if needed, starts the local server, and opens the browser.

cd "$(dirname "$0")" || exit 1
echo "=== Earnings Edge launcher ==="

# Create the virtual environment + install deps on first run.
if [ ! -d ".venv" ]; then
  echo "First run: creating environment (one-time, ~1 min)..."
  python3 -m venv .venv || { echo "ERROR: python3 not found. Install Python 3."; read -r; exit 1; }
  source .venv/bin/activate
  pip install --quiet --upgrade pip
  pip install --quiet -r requirements.txt
else
  source .venv/bin/activate
fi

# Open the browser shortly after the server starts.
( sleep 2 && open "http://127.0.0.1:8000" ) &

echo "Starting server — keep this window open. Close it (or press Ctrl+C) to stop."
python -m tools.cli serve

echo ""
echo "Server stopped. You can close this window."
read -r

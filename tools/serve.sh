#!/bin/bash
# Keep the local web app always up (launchd KeepAlive). Loopback-only.
cd "$(dirname "$0")/.." || exit 1
source .venv/bin/activate 2>/dev/null || exit 1
exec python -m tools.cli serve
